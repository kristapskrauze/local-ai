"""Atomic MSI Center lighting-profile control for the MS-7B43 motherboard.

MSI's public Mystic Light SDK exposes the board profile records as separate
areas, but each ordinary setter behaves like a device-wide update.  A loop over
those areas therefore produces a visible chase and ultimately leaves only the
final subset lit.  MSI Center itself does not use that route for profile
changes: LEDKeeper2 watches the 32-bit ``SettingData`` registry value and
applies the complete profile in one transaction.

This module changes the eight ``MB Mystic Light1..8`` records, ``JRGB1``, and
the JCORSAIR profile records in one snapshot. Every unrelated profile is
preserved byte-for-byte. Ordinary identical scenes perform no registry write;
a deliberate forced reconciliation still performs one acknowledged
transaction so hardware that reset after boot cannot be mistaken for lit. A
forced identical snapshot first changes only a colour byte on the disabled
``MB Function LED`` record; Windows does not emit LEDKeeper2's
``RegistryValueChangeEvent`` when identical ``SettingData`` bytes are written.
"""
from __future__ import annotations

import re
import threading
import time

try:
    import winreg
except ImportError:  # pragma: no cover - useful when pure helpers run off Windows
    winreg = None


REG_PATH = r"SOFTWARE\MSI\MSI Center\Component\Mystic Light\LED"
SETTING_VALUE = "SettingData"
APPLY_VALUE = "ML_UI_Apply"
WAIT_VALUE = "ML_UI_WaitSync"
SYNC_STATUS_VALUE = "SyncStatus"

# MSI keeps a SECOND source of truth: the "All Sync" master colour. Writing
# only SettingData leaves this at whatever MSI Center last used (255,0,0 here),
# and something re-asserts it about twice a second -- the zones visibly flicker
# between our colour and red. Both have to agree or they fight.
ALLSYNC_STYLE_VALUE = "CurrentAllSyncStyleIndex"
ALLSYNC_RGB_VALUES = ("CurrentAllSyncR", "CurrentAllSyncG", "CurrentAllSyncB")
ALLSYNC_STATIC_STYLE = "8"      # Steady, same index as the per-record styles

BOARD_NAME = re.compile(r"^MB Mystic Light([1-8])$")
JRGB_NAME = "JRGB1"
FUNCTION_LED_NAME = "MB Function LED"
AIO_PREFIX = "JCORSAIR-"
VGA_PROFILE_TYPE = "V01"
SYNC_GROUP = re.compile(r"^([A-Z]\d{2})([01])(\d+)$")
STATIC = {
    "board": ("8", "F"),
    "jrgb": ("8", "F"),
    "aio": ("1", "T"),
}
BOARD_LIT_STYLE = "8"

# Field 10 IS the persisted brightness level.  The public SDK cannot write it
# on this board (`SetLedBright` returns -103), so profile transactions must keep
# enabled routes at the user's requested maximum instead of preserving a stale
# zero left by a previous failed/off transaction.
BRIGHT_INDEX = 10
BRIGHT_LEVEL = "100"
STATIC_STYLE, STATIC_FLAG = STATIC["board"]     # kept for the existing tests
_LOCK = threading.Lock()


class MsiCenterError(RuntimeError):
    pass


def _clamp_colour(rgb):
    values = tuple(max(0, min(255, int(value))) for value in rgb)
    if len(values) != 3:
        raise ValueError("RGB colour must contain exactly three channels")
    return values


def split_profile(value):
    """Return mutable profile records while preserving their original order."""
    if not isinstance(value, str) or not value:
        raise MsiCenterError("MSI Center SettingData is empty")
    return [record.split(",") for record in value.split("|")]


def join_profile(records):
    return "|".join(",".join(fields) for fields in records)


def detach_vga_from_sync_status(sync_value, profile_value):
    """Return MSI's native All Sync state with VGA ownership disabled.

    ``SyncStatus`` stores selected SettingData indexes before ``|`` and main
    device groups after it.  A group token is ``<type><mode><start index>``;
    for example the live RTX entry ``V01114`` is VGA type ``V01``, enabled
    mode ``1``, starting at SettingData index 14.  MSI's own unlink operation
    keeps the group and changes its mode to zero, while removing its record
    indexes from the selected prefix.

    The VGA SettingData record itself is deliberately preserved.  OpenRGB can
    then own the GPU without MSI's All Sync transaction repainting it.
    """
    if not isinstance(sync_value, str) or sync_value.count("|") != 1:
        raise MsiCenterError("MSI Center SyncStatus has an invalid shape")

    selected_text, groups_text = sync_value.split("|", 1)
    selected = [] if not selected_text else selected_text.split(",")
    if any(not token.isdigit() for token in selected):
        raise MsiCenterError("MSI Center SyncStatus contains an invalid index")

    records = split_profile(profile_value)
    vga_indexes = {
        index for index, fields in enumerate(records)
        if fields and fields[0] == VGA_PROFILE_TYPE
    }
    if not vga_indexes:
        return sync_value, {"vga_indexes": [], "vga_groups": 0}

    groups = [] if not groups_text else groups_text.split(",")
    rewritten_groups = []
    vga_groups = 0
    for token in groups:
        match = SYNC_GROUP.fullmatch(token)
        if not match:
            raise MsiCenterError("MSI Center SyncStatus contains an invalid group")
        profile_type, mode, start_text = match.groups()
        if profile_type == VGA_PROFILE_TYPE:
            if int(start_text) not in vga_indexes:
                raise MsiCenterError(
                    "MSI Center VGA SyncStatus does not match SettingData")
            vga_groups += 1
            token = "%s0%s" % (profile_type, start_text)
        rewritten_groups.append(token)

    if vga_groups == 0:
        raise MsiCenterError("MSI Center VGA profile has no SyncStatus group")

    selected = [token for token in selected if int(token) not in vga_indexes]
    updated = "%s|%s" % (",".join(selected), ",".join(rewritten_groups))
    return updated, {
        "vga_indexes": sorted(vga_indexes),
        "vga_groups": vga_groups,
    }


def normalize_sync_status(sync_value, profile_value):
    """Select every MSI motherboard record while keeping VGA out of All Sync.

    MSI stores the records and their selection separately.  A perfectly
    coloured ``SettingData`` snapshot with an empty selection prefix is still
    an off profile.  Rebuild the prefix from the live M01 records, enable the
    M01 group, and use MSI's native disabled form for each V01 group.
    """
    detached, detail = detach_vga_from_sync_status(sync_value, profile_value)
    selected_text, groups_text = detached.split("|", 1)
    records = split_profile(profile_value)
    msi_indexes = [
        index for index, fields in enumerate(records)
        if fields and fields[0] == "M01"
    ]
    if not msi_indexes:
        raise MsiCenterError("MSI Center profile has no M01 records")

    selected = [] if not selected_text else selected_text.split(",")
    if any(not token.isdigit() or int(token) >= len(records)
           for token in selected):
        raise MsiCenterError("MSI Center SyncStatus contains an invalid index")
    # Preserve selected records owned by unrelated groups, but make M01 and
    # V01 authoritative: every MSI board/AIO record is selected; no VGA is.
    wanted = set(msi_indexes)
    for token in selected:
        index = int(token)
        if records[index][0] not in ("M01", VGA_PROFILE_TYPE):
            wanted.add(index)

    rewritten_groups = []
    msi_groups = 0
    for token in ([] if not groups_text else groups_text.split(",")):
        match = SYNC_GROUP.fullmatch(token)
        if not match:
            raise MsiCenterError("MSI Center SyncStatus contains an invalid group")
        profile_type, mode, start_text = match.groups()
        if profile_type == "M01":
            if int(start_text) not in msi_indexes:
                raise MsiCenterError(
                    "MSI Center M01 SyncStatus does not match SettingData")
            msi_groups += 1
            token = "%s1%s" % (profile_type, start_text)
        rewritten_groups.append(token)
    if msi_groups == 0:
        raise MsiCenterError("MSI Center M01 profile has no SyncStatus group")

    updated = "%s|%s" % (
        ",".join(str(index) for index in range(len(records)) if index in wanted),
        ",".join(rewritten_groups),
    )
    detail.update({
        "msi_indexes": msi_indexes,
        "msi_groups": msi_groups,
        "msi_sync_selected": updated == sync_value,
    })
    return updated, detail


def _record_kind(fields):
    if len(fields) < 11:
        return None
    name = fields[2]
    if BOARD_NAME.fullmatch(name):
        return "board"
    if name == JRGB_NAME:
        return "jrgb"
    if name.startswith(AIO_PREFIX):
        return "aio"
    return None


def update_profile(value, board_rgb, aio_rgb, board_enabled=True,
                   aio_enabled=True):
    """Build one MSI profile snapshot and describe exactly what was touched.

    MSI Center normalises colours to 5-bit-ish values, so callers normally
    quantise before this function.  Keeping the helper exact makes it easy to
    test and prevents hidden changes to persisted profile data.
    """
    board_rgb = _clamp_colour(board_rgb if board_enabled else (0, 0, 0))
    aio_rgb = _clamp_colour(aio_rgb if aio_enabled else (0, 0, 0))
    records = split_profile(value)
    counts = {"board": 0, "jrgb": 0, "aio": 0}
    wanted = {"board": board_rgb, "jrgb": board_rgb, "aio": aio_rgb}

    for fields in records:
        # The function/status LED record is not an RGB route and enabling it
        # can light diagnostic indicators or make LEDKeeper2 re-enumerate the
        # board.  Keep it disabled in every profile transaction.
        if len(fields) >= 4 and fields[2] == FUNCTION_LED_NAME:
            fields[3] = "F"
            continue
        kind = _record_kind(fields)
        if not kind:
            continue
        counts[kind] += 1
        # Per GROUP -- they do not share a style table. Straight from MSI
        # Center's own SELECT ALL + Steady apply:
        #   board  T,8,r,g,b,F      aio  T,1,r,g,b,T
        # Using the board pair for the AIO records is what broke the fans and
        # dragon immediately after a manual write had them right.
        style, flag = STATIC[kind]
        if kind in ("board", "jrgb") and board_enabled:
            # Use MSI Center's captured Steady index for the board and JRGB1.
            # A disabled route stays black on the same static style.
            style = BOARD_LIT_STYLE
        # Field 3 is the route's actual enable bit. MSI Center can leave all
        # colours/styles intact while setting it to F; treating that snapshot
        # as latched is what allowed the hardware's old rainbow to keep running.
        fields[3] = "T" if ((kind in ("board", "jrgb") and board_enabled) or
                              (kind == "aio" and aio_enabled)) else "F"
        fields[4] = style
        if len(fields) > 8:
            fields[8] = flag
        fields[5:8] = [str(channel) for channel in wanted[kind]]
        if len(fields) > BRIGHT_INDEX:
            fields[BRIGHT_INDEX] = BRIGHT_LEVEL if (
                (kind in ("board", "jrgb") and board_enabled) or
                (kind == "aio" and aio_enabled)) else "0"

    if counts["board"] != 8:
        raise MsiCenterError(
            "expected 8 board Mystic records, found %d" % counts["board"])
    if counts["jrgb"] != 1:
        raise MsiCenterError(
            "expected one JRGB1 profile record, found %d" % counts["jrgb"])
    if counts["aio"] < 1:
        raise MsiCenterError("no JCORSAIR profile records found")

    updated = join_profile(records)
    return updated, {
        "board_zones": counts["board"],
        "aio_profiles": counts["aio"],
        "board_color": board_rgb,
        "aio_color": aio_rgb,
        "changed": updated != value,
    }


def pulse_profile(value):
    """Make a harmless, real ``SettingData`` change for a forced apply."""
    records = split_profile(value)
    for fields in records:
        if (len(fields) >= 8 and fields[2] == FUNCTION_LED_NAME and
                fields[3] == "F"):
            fields[5] = "8" if fields[5] == "0" else "0"
            return join_profile(records)
    raise MsiCenterError(
        "no disabled MB Function LED record available for apply pulse")


def inspect_value(value):
    """Summarise the controlled records in an MSI ``SettingData`` string."""
    colours = {"board": [], "jrgb": [], "aio": []}
    styles = {"board": [], "jrgb": [], "aio": []}
    enabled = {"board": [], "jrgb": [], "aio": []}
    for fields in split_profile(value):
        kind = _record_kind(fields)
        if not kind:
            continue
        try:
            colours[kind].append(tuple(int(channel) for channel in fields[5:8]))
        except (TypeError, ValueError):
            colours[kind].append(None)
        styles[kind].append(fields[4])
        enabled[kind].append(fields[3] == "T")

    def _group(kind):
        values = colours[kind]
        uniform = bool(values) and all(value == values[0] for value in values)
        return {
            ("zones" if kind == "board" else
             "headers" if kind == "jrgb" else "profiles"): len(values),
            "uniform": uniform,
            "color": values[0] if uniform else None,
            "styles": sorted(set(styles[kind])),
            "enabled": bool(enabled[kind]) and all(enabled[kind]),
            "enabled_records": sum(1 for value in enabled[kind] if value),
        }

    return {"board": _group("board"), "jrgb": _group("jrgb"),
            "aio": _group("aio")}


def _open(access):
    if winreg is None:
        raise MsiCenterError("MSI Center profile control requires Windows")
    flags = access | getattr(winreg, "KEY_WOW64_32KEY", 0)
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_PATH, 0, flags)


def _read(key, name):
    return winreg.QueryValueEx(key, name)[0]


def inspect_profile():
    """Read-only health check for the live MSI Center profile."""
    try:
        with _open(winreg.KEY_READ) as key:
            value = _read(key, SETTING_VALUE)
            apply_pending = str(_read(key, APPLY_VALUE)).lower() == "true"
            wait_pending = str(_read(key, WAIT_VALUE)).lower() == "true"
            try:
                allsync_style = str(_read(key, ALLSYNC_STYLE_VALUE))
                allsync_color = tuple(
                    int(_read(key, name)) for name in ALLSYNC_RGB_VALUES)
                allsync_error = None
            except Exception as exc:
                allsync_style = None
                allsync_color = None
                allsync_error = str(exc)[:240]
            try:
                sync_status = str(_read(key, SYNC_STATUS_VALUE))
                gpu_sync_error = None
            except Exception as exc:
                sync_status = None
                gpu_sync_error = str(exc)[:240]
        result = inspect_value(value)
    except Exception as exc:
        return {
            "available": False,
            "backend": "MSI Center atomic profile",
            "board": {"zones": 0, "uniform": False, "color": None, "styles": []},
            "jrgb": {"headers": 0, "uniform": False, "color": None, "styles": []},
            "aio": {"profiles": 0, "uniform": False, "color": None, "styles": []},
            "apply_pending": False,
            "wait_pending": False,
            "sync_status": None,
            "gpu_allsync_detached": False,
            "gpu_sync_error": None,
            "allsync_style": None,
            "allsync_color": None,
            "allsync_error": None,
            "vga_indexes": [],
            "error": str(exc)[:240],
        }

    vga_indexes = []
    gpu_allsync_detached = False
    msi_sync_selected = False
    msi_indexes = []
    if sync_status is not None:
        try:
            detached_status, detached_detail = detach_vga_from_sync_status(
                sync_status, value)
            gpu_allsync_detached = detached_status == sync_status
            normalized_status, sync_detail = normalize_sync_status(
                sync_status, value)
            vga_indexes = sync_detail["vga_indexes"]
            msi_indexes = sync_detail["msi_indexes"]
            msi_sync_selected = normalized_status == sync_status
        except Exception as exc:
            gpu_sync_error = str(exc)[:240]

    result.update({
        # Profile availability and AIO health do not disappear merely because
        # an MSI update changed the separate SyncStatus encoding.
        "available": result["board"]["zones"] == 8 and
                     result["jrgb"]["headers"] == 1 and
                     result["aio"]["profiles"] >= 1,
        "apply_pending": apply_pending,
        "wait_pending": wait_pending,
        "sync_status": sync_status,
        "gpu_allsync_detached": gpu_allsync_detached,
        "msi_sync_selected": msi_sync_selected,
        "gpu_sync_error": gpu_sync_error,
        "allsync_style": allsync_style,
        "allsync_color": allsync_color,
        "allsync_error": allsync_error,
        "vga_indexes": vga_indexes,
        "msi_indexes": msi_indexes,
        "backend": "MSI Center atomic profile",
        "error": None,
    })
    return result


def _same_color(value, expected):
    try:
        return tuple(int(channel) for channel in value) == tuple(expected)
    except Exception:
        return False


def profile_current_errors(profile, board_rgb, aio_rgb, board_enabled=True,
                           aio_enabled=True):
    """Return live-profile mismatches for the complete Dobby-owned MSI route."""
    board_enabled = bool(board_enabled)
    aio_enabled = bool(aio_enabled)
    board_rgb = _clamp_colour(board_rgb if board_enabled else (0, 0, 0))
    aio_rgb = _clamp_colour(aio_rgb if aio_enabled else (0, 0, 0))
    sync_rgb = board_rgb if board_enabled else (
        aio_rgb if aio_enabled else (0, 0, 0))
    errors = []
    if not profile or not profile.get("available"):
        errors.append((profile or {}).get("error") or "profile unavailable")
        return errors
    if profile.get("apply_pending"):
        errors.append("apply flag still pending")
    if not profile.get("gpu_allsync_detached"):
        errors.append(profile.get("gpu_sync_error") or "VGA still in All Sync")
    if not profile.get("msi_sync_selected"):
        errors.append(profile.get("gpu_sync_error") or
                      "MSI records missing from SyncStatus")

    checks = (
        ("board", "board", board_rgb, STATIC["board"][0], board_enabled),
        ("JRGB1", "jrgb", board_rgb, STATIC["jrgb"][0], board_enabled),
        ("AIO", "aio", aio_rgb, STATIC["aio"][0], aio_enabled),
    )
    for label, key, expected_rgb, expected_style, expected_enabled in checks:
        state = profile.get(key) or {}
        if bool(state.get("enabled")) != expected_enabled:
            errors.append("%s enable mismatch" % label)
        if not state.get("uniform") or not _same_color(
                state.get("color"), expected_rgb):
            errors.append("%s colour mismatch" % label)
        if sorted(str(style) for style in (state.get("styles") or [])) != [
                expected_style]:
            errors.append("%s style mismatch" % label)
    if str(profile.get("allsync_style")) != ALLSYNC_STATIC_STYLE:
        errors.append("All Sync style mismatch")
    if not _same_color(profile.get("allsync_color"), sync_rgb):
        errors.append("All Sync colour mismatch")
    if profile.get("allsync_error"):
        errors.append("All Sync read failed: %s" % profile.get("allsync_error"))
    return errors


def profile_is_current(profile, board_rgb, aio_rgb, board_enabled=True,
                       aio_enabled=True):
    return not profile_current_errors(
        profile, board_rgb, aio_rgb,
        board_enabled=board_enabled, aio_enabled=aio_enabled)


def apply_profile(board_rgb, aio_rgb, board_enabled=True, aio_enabled=True,
                  timeout=12.0, force=False):
    """Commit both MSI lighting routes and wait for LEDKeeper2's acknowledgement.

    An ordinary call whose complete registry state already matches is a no-op.
    ``force=True`` deliberately repeats the one atomic transaction and waits
    for a fresh acknowledgement.  Dobby uses that once at startup because the
    controller can reset while MSI's persisted registry snapshot stays intact.
    """
    with _LOCK:
        with _open(winreg.KEY_READ | winreg.KEY_WRITE) as key:
            if str(_read(key, APPLY_VALUE)).lower() == "true":
                # Wait before writing anything. If LEDKeeper2 died with the
                # flag stuck, clear only the stale flag and let the worker's
                # next retry build a fresh profile from stable registry data.
                pending_deadline = time.monotonic() + max(0.5, float(timeout))
                while time.monotonic() < pending_deadline:
                    time.sleep(0.05)
                    if str(_read(key, APPLY_VALUE)).lower() == "false":
                        break
                else:
                    winreg.SetValueEx(key, APPLY_VALUE, 0, winreg.REG_SZ,
                                      "False")
                    raise MsiCenterError(
                        "stale MSI Center apply flag was cleared; retry profile apply")

            current = _read(key, SETTING_VALUE)
            updated, detail = update_profile(
                current, board_rgb, aio_rgb,
                board_enabled=board_enabled, aio_enabled=aio_enabled)
            setting_changed = bool(detail["changed"])
            detail.update({
                "forced": bool(force),
                "committed": False,
                "already_current": False,
                "edge_pulsed": False,
            })

            current_sync_status = str(_read(key, SYNC_STATUS_VALUE))
            updated_sync_status, sync_detail = normalize_sync_status(
                current_sync_status, current)
            sync_status_changed = updated_sync_status != current_sync_status
            if sync_status_changed:
                winreg.SetValueEx(key, SYNC_STATUS_VALUE, 0, winreg.REG_SZ,
                                  updated_sync_status)
                detail["changed"] = True
            detail.update({
                "requested_sync_status": updated_sync_status,
                "sync_status_changed": sync_status_changed,
                "target_vga_indexes": sync_detail["vga_indexes"],
                "target_msi_indexes": sync_detail["msi_indexes"],
            })

            # Keep the All Sync master in step with the scene. Whichever group
            # is actually being driven wins. If both are disabled, black is
            # authoritative; leaving the old master colour behind can repaint
            # records that were just committed off.
            sync_rgb = (board_rgb if board_enabled else
                        (aio_rgb if aio_enabled else (0, 0, 0)))
            allsync_stale = False
            # THE one that mattered. All Sync owned the GPU plus every MSI
            # record (indexes 0..14), and its style index was 1 = Rainbow.
            # That is what re-painted the whole rig rainbow, survived reboots,
            # ignored Mystic_Light_Service being stopped, and stomped every
            # per-record Steady write moments after it landed. 8 = Steady.
            try:
                if str(_read(key, ALLSYNC_STYLE_VALUE)) != ALLSYNC_STATIC_STYLE:
                    winreg.SetValueEx(key, ALLSYNC_STYLE_VALUE, 0, winreg.REG_SZ,
                                      ALLSYNC_STATIC_STYLE)
                    allsync_stale = True
            except OSError:
                pass
            for name, channel in zip(ALLSYNC_RGB_VALUES, sync_rgb):
                try:
                    if int(_read(key, name)) != int(channel):
                        allsync_stale = True
                except Exception:
                    allsync_stale = True
            if allsync_stale:
                for name, channel in zip(ALLSYNC_RGB_VALUES, sync_rgb):
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ,
                                      str(int(channel)))
            detail["allsync_synced"] = allsync_stale
            if allsync_stale:
                detail["changed"] = True

            if not detail["changed"] and not force:
                snapshot = inspect_value(current)
                return {
                    **snapshot,
                    **detail,
                    "available": True,
                    "backend": "MSI Center atomic profile",
                    # No watcher transaction happened, so these must not be
                    # presented as physical proof. A forced startup call is
                    # what turns an identical persisted state into proof.
                    "acknowledged": False,
                    "latched": False,
                    "already_current": True,
                    "apply_pending": False,
                    "sync_status": updated_sync_status,
                    "gpu_allsync_detached": True,
                    "msi_sync_selected": True,
                    "gpu_sync_error": None,
                    "vga_indexes": sync_detail["vga_indexes"],
                    "msi_indexes": sync_detail["msi_indexes"],
                    "allsync_style": ALLSYNC_STATIC_STYLE,
                    "allsync_color": tuple(sync_rgb),
                    "allsync_error": None,
                    "error": None,
                }

            # LEDKeeper2's only apply trigger is a WMI change event on
            # SettingData. SetValueEx with identical bytes does not produce
            # that event, even if ML_UI_Apply was toggled first. Whenever the
            # requested transaction changes only companion values (or is a
            # forced boot reconciliation), pulse the disabled function LED
            # and wait for its acknowledgement. Writing the real snapshot
            # afterward is then guaranteed to be another actual change/event.
            if not setting_changed:
                pulse = pulse_profile(updated)
                winreg.SetValueEx(key, APPLY_VALUE, 0, winreg.REG_SZ, "True")
                winreg.SetValueEx(key, SETTING_VALUE, 0, winreg.REG_SZ, pulse)
                try:
                    winreg.FlushKey(key)
                except OSError:
                    pass
                pulse_deadline = time.monotonic() + max(0.5, float(timeout))
                while time.monotonic() < pulse_deadline:
                    time.sleep(0.05)
                    if str(_read(key, APPLY_VALUE)).lower() == "false":
                        detail["edge_pulsed"] = True
                        break
                else:
                    winreg.SetValueEx(key, APPLY_VALUE, 0, winreg.REG_SZ,
                                      "False")
                    raise MsiCenterError(
                        "LEDKeeper2 did not acknowledge the profile edge pulse "
                        "within %.1fs" % timeout)

            winreg.SetValueEx(key, APPLY_VALUE, 0, winreg.REG_SZ, "True")
            winreg.SetValueEx(key, SETTING_VALUE, 0, winreg.REG_SZ, updated)
            try:
                winreg.FlushKey(key)
            except OSError:
                pass

        deadline = time.monotonic() + max(0.5, float(timeout))
        acknowledged = False
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with _open(winreg.KEY_READ) as key:
                if str(_read(key, APPLY_VALUE)).lower() == "false":
                    acknowledged = True
                    break

        if not acknowledged:
            # Do not leave MSI Center presenting a permanently pending apply.
            try:
                with _open(winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, APPLY_VALUE, 0, winreg.REG_SZ, "False")
            except OSError:
                pass
            raise MsiCenterError(
                "LEDKeeper2 did not acknowledge the atomic profile within %.1fs" % timeout)

        result = inspect_profile()
        if not result.get("available"):
            raise MsiCenterError(
                result.get("error") or "MSI Center profile unavailable after apply")
        if not result.get("gpu_allsync_detached"):
            raise MsiCenterError(
                result.get("gpu_sync_error") or
                "MSI Center reattached the VGA to All Sync after apply")
        if not result.get("msi_sync_selected"):
            raise MsiCenterError(
                result.get("gpu_sync_error") or
                "MSI Center cleared the motherboard/AIO SyncStatus selection")
        board_state = result.get("board") or {}
        jrgb_state = result.get("jrgb") or {}
        aio_state = result.get("aio") or {}
        if bool(board_state.get("enabled")) != bool(board_enabled):
            raise MsiCenterError("MSI Center did not preserve the board enable state")
        if bool(jrgb_state.get("enabled")) != bool(board_enabled):
            raise MsiCenterError("MSI Center did not preserve the JRGB1 enable state")
        if bool(aio_state.get("enabled")) != bool(aio_enabled):
            raise MsiCenterError("MSI Center did not preserve the AIO enable state")
        errors = profile_current_errors(
            result, board_rgb, aio_rgb,
            board_enabled=board_enabled, aio_enabled=aio_enabled)
        if errors:
            raise MsiCenterError(
                "MSI Center profile drifted after apply: %s" %
                "; ".join(errors[:4]))

        detail["committed"] = True
        result.update(detail)
        result.update({
            "acknowledged": True,
            "latched": True,
            "already_current": False,
            "error": None,
        })
        return result
