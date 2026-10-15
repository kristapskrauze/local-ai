"""GMMK / EVision 0C45:652F keyboard, driven natively over Windows HID.

OpenRGB detects it (126 LEDs) but cannot hold a colour: its writes reach the
hardware and are then lost to the firmware's stored profile, because it never
sends the trailing END that commits the transaction a START opened. Upstream
this model is an open request, not supported hardware (OpenRGB issue #3453).

Ported from dokutan/rgb_keyboard (libusb interrupt to endpoint 0x03, Linux
only). It maps exactly onto Windows HID: the vendor collection (usage page
0xFF1C, usage 0x0092) reports OutputReportByteLength = 64 and
FeatureReportByteLength = 0, so a plain 64-byte output report with the report id
(0x04) inline is the same bytes on the wire. Every setting is START, settings
packet(s), END.

Byte[1] of a settings packet is a checksum over the meaningful payload bytes
(0x06 + 0x01 from the base packet, plus whatever the write sets). The colour
packet is the exception -- upstream leaves it zero and the firmware accepts it,
so we send it byte-for-byte identical.

No direct/streaming path exists: every transaction commits to the stored
profile, and per-key costs one packet PER KEY (126 per frame). So this drives
colour plus the board's own hardware effects, on change only (MIN_INTERVAL).

Needs hidapi -- the one pip dependency in the service. The import is lazy and
every entry point fails soft, so a missing wheel costs the keyboard, not the
service.
"""
import threading
import time

VID, PID = 0x0C45, 0x652F
USAGE_PAGE, USAGE = 0xFF1C, 0x0092

# include/data.cpp -- the transaction framing. END is the commit.
DATA_START = [0x04, 0x01, 0x00, 0x01] + [0x00] * 60
DATA_END = [0x04, 0x02, 0x00, 0x02] + [0x00] * 60
DATA_SETTINGS = [0x04, 0x00, 0x00, 0x06, 0x01] + [0x00] * 59
# include/readers.cpp read_led_settings(), profile 1 slice
DATA_READ = [0x04, 0x3D, 0x00, 0x05, 0x38] + [0x00] * 59

# include/writers.cpp write_mode(), profile 1
HW_MODES = {
    "horizontal_wave": 0x01, "pulse": 0x02, "hurricane": 0x03,
    "breathing_color": 0x04, "breathing": 0x05, "fixed": 0x06,
    "reactive_single": 0x07, "reactive_ripple": 0x08,
    "reactive_horizontal": 0x09, "waterfall": 0x0A, "swirl": 0x0B,
    "vertical_wave": 0x0C, "sine": 0x0D, "vortex": 0x0E, "rain": 0x0F,
    "diagonal_wave": 0x10, "reactive_color": 0x11, "ripple": 0x12,
    "off": 0x13, "custom": 0x14,
}
MODE_NAMES = {v: k for k, v in HW_MODES.items()}

# Dashboard mode -> what the keyboard should do. The board animates its own
# effects, which is the whole point: we set it once instead of pushing frames.
# Every state-derived mode (temp/load/ai/goals) is a solid colour the engine
# computes, so they all land on "fixed".
MODE_MAP = {
    "off": ("off", False),
    "static": ("fixed", False),
    "breathe": ("breathing_color", False),
    "rainbow": ("horizontal_wave", True),   # True = let the board cycle hue
    "temp": ("fixed", False),
    "load": ("fixed", False),
    "ai": ("fixed", False),
    "goals": ("fixed", False),
}

# Each write commits to the keyboard's stored profile, so the engine's 120 ms
# tick must not reach it. Modes like temp/ai slide their colour continuously;
# without these two guards the keyboard would be rewritten ~8x a second.
MIN_INTERVAL = 2.0
COLOR_DELTA = 12          # per-channel change worth a write
DYNAMIC_SCENE_MODES = {"temp", "load", "ai", "goals"}

VERIFY_SECONDS = 10.0

_LOCK = threading.RLock()
_DEV = None
_STATE = {
    "available": None,     # None = not probed yet
    "error": None,
    "last": None,          # (mode, rainbow, rgb) actually written
    "last_write": 0.0,
    "verified": 0.0,
    "writes": 0,
    "path": None,
}


def _hid():
    """Import hidapi lazily so a missing wheel never stops the service."""
    try:
        import hid
        return hid
    except Exception as exc:
        _STATE["error"] = "hidapi unavailable: %s" % (exc,)
        _STATE["available"] = False
        return None


def _open():
    """The vendor collection, opened and cached. None if it is not there."""
    global _DEV
    if _DEV is not None:
        return _DEV
    hid = _hid()
    if hid is None:
        return None
    try:
        for d in hid.enumerate(VID, PID):
            if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE:
                dev = hid.device()
                dev.open_path(d["path"])
                _DEV = dev
                _STATE["available"] = True
                _STATE["error"] = None
                _STATE["path"] = d["path"].decode(errors="replace")
                return _DEV
        _STATE["available"] = False
        _STATE["error"] = "keyboard not present"
    except Exception as exc:
        _STATE["available"] = False
        _STATE["error"] = "open failed: %s" % (exc,)
        _DEV = None
    return None


def _close():
    global _DEV
    if _DEV is not None:
        try:
            _DEV.close()
        except Exception:
            pass
    _DEV = None


def _send(dev, packet):
    """One 64-byte output report. The firmware echoes each packet back; we drain
    that reply so it cannot pile up and be mistaken for a later readback."""
    dev.write(bytes(packet))
    time.sleep(0.002)
    try:
        dev.read(64, 40)
    except Exception:
        pass


def _settings(**over):
    pkt = list(DATA_SETTINGS)
    for name, val in over.items():
        pkt[int(name[1:])] = val & 0xFF
    return pkt


def _txn(dev, packets):
    """START, the payload packets, then the END that commits them."""
    _send(dev, DATA_START)
    for p in packets:
        _send(dev, p)
    _send(dev, DATA_END)


def _write_brightness(dev, level):
    level = max(0, min(4, int(level)))
    _txn(dev, [_settings(b1=0x08 + level, b5=0x01, b8=level)])


def _write_mode(dev, mode):
    code = HW_MODES[mode]
    _txn(dev, [_settings(b1=0x07 + code, b8=code)])


def _write_color(dev, r, g, b, rainbow=False):
    if rainbow:
        ds1 = _settings(b1=0x0C, b5=0x04, b8=0x01)
    else:
        ds1 = _settings(b1=0x0B, b5=0x04)
    # NB byte[1] deliberately left 0 here -- upstream sends no checksum on the
    # colour packet and the firmware accepts it. Do not "fix" this.
    ds2 = _settings(b2=0x02, b4=0x03, b5=0x05, b8=r, b9=g, b10=b)
    _txn(dev, [ds1, ds2])


def available():
    with _LOCK:
        if _STATE["available"] is None:
            _open()
        return bool(_STATE["available"])


def push(rgb, mode="static", force=False):
    """Follow the engine's colour, but only when it actually changed.

    Returns True if a write went out. The engine's brightness is already baked
    into rgb by _compute_color(), so hardware brightness stays at max -- setting
    both would dim the keyboard twice.
    """
    scene_mode = str(mode).lower()
    hw_mode, rainbow = MODE_MAP.get(scene_mode, ("fixed", False))
    rgb = tuple(max(0, min(255, int(c))) for c in rgb)
    now = time.time()
    with _LOCK:
        # This keyboard has no direct/live colour channel: every colour packet
        # visibly commits the onboard profile. For continuously changing scene
        # modes, keep the last fixed colour instead of replaying the write
        # animation every second. A forced write still works for an explicit
        # user colour or recovery action.
        if (not force and scene_mode in DYNAMIC_SCENE_MODES and
                _STATE["last"] is not None and _STATE["last"][0] == "fixed"):
            return False

        if (not force and _STATE["last"] is not None
                and _STATE["last"][0] == "fixed" and not _STATE["last"][1]
                and now - _STATE.get("verified", 0.0) >= VERIFY_SECONDS):
            _STATE["verified"] = now
            actual = read_settings()
            want = "#%02x%02x%02x" % _STATE["last"][2]
            if actual is None or actual.get("color") != want:
                _STATE["last"] = None      # drifted -> full repaint below

        last = _STATE["last"]
        if not force and last is not None:
            same_mode = (last[0] == hw_mode and last[1] == rainbow)
            drift = max(abs(a - b) for a, b in zip(last[2], rgb))
            if same_mode and drift < COLOR_DELTA:
                return False
            if now - _STATE["last_write"] < MIN_INTERVAL:
                return False
        dev = _open()
        if dev is None:
            return False
        try:
            if last is None or last[0] != hw_mode or last[1] != rainbow:
                _write_brightness(dev, 4)
                _write_mode(dev, hw_mode)
            if hw_mode != "off":
                _write_color(dev, rgb[0], rgb[1], rgb[2], rainbow=rainbow)
            _STATE["last"] = (hw_mode, rainbow, rgb)
            _STATE["last_write"] = now
            _STATE["writes"] += 1
            _STATE["error"] = None
            return True
        except Exception as exc:
            # A yanked cable leaves a dead handle behind; drop it so the next
            # push re-enumerates instead of failing forever.
            _STATE["error"] = "write failed: %s" % (exc,)
            _close()
            return False


def off():
    with _LOCK:
        dev = _open()
        if dev is None:
            return False
        try:
            _write_mode(dev, "off")
            _STATE["last"] = ("off", False, (0, 0, 0))
            _STATE["last_write"] = time.time()
            return True
        except Exception as exc:
            _STATE["error"] = "off failed: %s" % (exc,)
            _close()
            return False


def read_settings():
    """What the keyboard says it is set to. This is a real readback, not an
    echo -- it is how the port was verified in the first place."""
    with _LOCK:
        dev = _open()
        if dev is None:
            return None
        try:
            dev.write(bytes(DATA_READ))
            time.sleep(0.005)
            buf = list(dev.read(64, 500) or [])
        except Exception as exc:
            _STATE["error"] = "read failed: %s" % (exc,)
            _close()
            return None
    if len(buf) < 16:
        return None
    return {
        "mode": MODE_NAMES.get(buf[8], "0x%02x" % buf[8]),
        "brightness": buf[9],
        "rainbow": bool(buf[12]),
        "color": "#%02x%02x%02x" % (buf[13], buf[14], buf[15]),
    }


def status():
    with _LOCK:
        last = _STATE["last"]
        return {
            "available": bool(_STATE["available"]),
            "error": _STATE["error"],
            "writes": _STATE["writes"],
            "path": _STATE["path"],
            "mode": last[0] if last else None,
            "last_color": ("#%02x%02x%02x" % last[2]) if last else None,
            "note": "native HID driver; OpenRGB cannot hold a colour on this model",
        }
