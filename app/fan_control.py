"""Multi-channel fan control + read-back via LibreHardwareMonitorLib.

Discovers all (Fan-RPM, Control-PWM) pairs on the Super I/O chip. Each
channel is independently togglable from the dashboard.

Thread-safe singleton. Lazy init - import is cheap; Computer.Open() only
runs on first call. Any failure leaves available=False and the rest of
the dashboard keeps working.

LocalSystem (or admin) required for Nuvoton Super I/O access.
"""
import concurrent.futures
import logging
import os
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LHM_DIR = os.path.normpath(os.path.join(_HERE, "..", "_tools", "LibreHardwareMonitor"))
_LHM_DLL = os.path.join(_LHM_DIR, "LibreHardwareMonitorLib.dll")

# Mode -> PWM percent (None = release to BIOS curve)
MODES = {
    "silent": 20,
    "quiet": 40,
    "balanced": 60,
    "performance": 100,
    "auto": None,
}
SAFETY_FLOOR = 20          # never let software PWM drop below this
HOT_TEMP_C = 85            # if CPU package > this, force >= HOT_FLOOR
HOT_FLOOR = 70

# Thermal watchdog: independent of mode, a background thread forces all
# channels (CPU + GPU) to 100% when CPU temp crosses CRITICAL_TEMP_C, and
# releases back to the last user-set mode when it drops below RECOVERY_TEMP_C.
CRITICAL_TEMP_C = 95
RECOVERY_TEMP_C = 85
WATCHDOG_POLL_S = 3.0

_lock = threading.Lock()
# All native LHM calls run on this dedicated worker thread so we can bound
# execution time. If a .NET call wedges (chip IOCTL hang), one timeout
# returns an error to the API instead of locking up the whole dashboard.
_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fanctl")
READ_TIMEOUT_S = 2.0
WRITE_TIMEOUT_S = 3.0
INIT_TIMEOUT_S = 8.0

_logger = logging.getLogger("sysmonitor.fan_control")

_state = {
    "available": False,
    "error": None,
    "computer": None,
    "cpu_package_sensor": None,
    "channels": {},         # CPU/board fan channels: idx -> dict
    "gpu_channels": {},     # NVIDIA GPU fan channels: idx -> dict
    "last_update": 0.0,
    "wedged": False,
    "thermal_emergency": False,
    "peak_temp_c": None,
    "saved_modes_pre_emergency": None,
    "monitor_thread": None,
    "cpu_core_temp_sensors": {},
    "cpu_thread_load_sensors": {},
    "cpu_core_clock_sensors": {},
    "cpu_bus_clock_sensor": None,
}
_stop_event = threading.Event()
_wedged_event = threading.Event()


def is_elevated():
    """True when this process has an admin token (Super I/O needs one)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _make_channel(idx, name, fan_sensor, control_sensor, control):
    return {
        "index": int(idx),
        "name": str(name),
        "fan_sensor": fan_sensor,
        "control_sensor": control_sensor,
        "control": control,
        "mode": "auto",
        "pwm_set": None,
    }


def _is_wedged():
    return _wedged_event.is_set() or bool(_state["wedged"])


def _mark_wedged(reason):
    _wedged_event.set()
    acquired = _lock.acquire(timeout=0.2)
    if acquired:
        try:
            _state["wedged"] = True
            _state["error"] = reason
        finally:
            _lock.release()
    else:
        _state["wedged"] = True
        _state["error"] = reason
    _logger.warning(reason)


def _set_runtime_error(reason):
    with _lock:
        _state["available"] = False
        _state["error"] = reason
    _logger.warning(reason)


def _init_locked():
    if _state["available"] or _state["error"]:
        return
    try:
        if not os.path.isfile(_LHM_DLL):
            _state["error"] = "LibreHardwareMonitorLib.dll not found at " + _LHM_DLL
            return

        import sys
        if _LHM_DIR not in sys.path:
            sys.path.append(_LHM_DIR)
        import clr  # type: ignore
        clr.AddReference(_LHM_DLL)
        from LibreHardwareMonitor.Hardware import (  # type: ignore
            Computer, HardwareType, SensorType,
        )
        from System.Reflection import BindingFlags  # type: ignore

        c = Computer()
        c.IsMotherboardEnabled = True
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsControllerEnabled = True
        c.Open()

        for _ in range(2):
            for hw in c.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()
            time.sleep(0.15)

        superio = None
        for hw in c.Hardware:
            if hw.HardwareType == HardwareType.Motherboard:
                for sub in hw.SubHardware:
                    if sub.HardwareType == HardwareType.SuperIO:
                        superio = sub
                        break
            if superio is not None:
                break
        if superio is None:
            if not is_elevated():
                _state["error"] = (
                    "not elevated: this dashboard is running as a normal user "
                    "(dev instance), so the Super I/O chip is unreachable. "
                    "Fan control works when the SysMonitor service serves the page."
                )
            else:
                _state["error"] = "Super I/O not detected"
            return

        # Private List<Sensor> of control sensors (each .Control = IControl)
        flags = BindingFlags.NonPublic | BindingFlags.Instance
        cf = superio.GetType().GetField("_controls", flags)
        if cf is None:
            _state["error"] = "SuperIOHardware._controls field not found"
            return
        controls_list = cf.GetValue(superio)
        if controls_list is None or controls_list.Count == 0:
            _state["error"] = "No control sensors on SuperIOHardware"
            return

        # Group by channel index
        by_idx = {}
        for s in superio.Sensors:
            if s.SensorType == SensorType.Fan:
                by_idx.setdefault(s.Index, {})["fan"] = s
        for i in range(controls_list.Count):
            cs = controls_list[i]
            if cs.SensorType == SensorType.Control:
                by_idx.setdefault(cs.Index, {})["ctrl"] = cs

        # Build channel objects for pairs that have BOTH a fan and a control
        # (so we can read RPM and write PWM)
        ch_dict = {}
        for idx in sorted(by_idx.keys()):
            entry = by_idx[idx]
            fan_sensor = entry.get("fan")
            control_sensor = entry.get("ctrl")
            if fan_sensor is None or control_sensor is None:
                continue
            control = control_sensor.Control
            if control is None:
                continue
            # Prefer the fan sensor's name (e.g. "Fan #1"); fall back to control
            name = fan_sensor.Name or control_sensor.Name or ("Channel " + str(idx))
            ch_dict[int(idx)] = _make_channel(idx, name, fan_sensor, control_sensor, control)

        if not ch_dict:
            _state["error"] = "No usable fan+control channels discovered"
            return

        # CPU package temp sensor (for safety floor)
        pkg = None
        for hw in c.Hardware:
            if hw.HardwareType == HardwareType.Cpu:
                for s in hw.Sensors:
                    if s.SensorType == SensorType.Temperature and "Package" in s.Name:
                        pkg = s
                        break
            if pkg is not None:
                break

        # Per-physical-core temperature sensors + per-logical-thread load
        # sensors + current clock sensors. Lets us drive the dashboard
        # without Core Temp running, and avoids psutil's Windows base-clock
        # reading that sticks at 3600 MHz on this rig.
        core_temps = {}
        thread_loads = {}
        core_clocks = {}
        bus_clock = None
        for hw in c.Hardware:
            if hw.HardwareType != HardwareType.Cpu:
                continue
            for s in hw.Sensors:
                nm = s.Name or ""
                if s.SensorType == SensorType.Temperature:
                    if "TjMax" in nm or "Distance" in nm or "Average" in nm or "Max" in nm:
                        continue
                    if "CPU Core" in nm:
                        core_temps[int(s.Index)] = s
                elif s.SensorType == SensorType.Load:
                    # "CPU Core #N Thread #M" - 16 per-thread sensors on a 9900KF
                    if "Thread" in nm and "Core" in nm:
                        thread_loads[int(s.Index)] = s
                elif s.SensorType == SensorType.Clock:
                    if "Bus" in nm:
                        bus_clock = s
                    elif "Core" in nm:
                        core_clocks[int(s.Index)] = s

        # NVIDIA GPU fan controls. NvidiaGpu has parallel _fans (Sensor[]),
        # _controls (Sensor[] for PWM% readout) and _fanControls
        # (Control[] - the writable IControl objects). Indexed by fan number.
        gpu_ch_dict = {}
        for hw in c.Hardware:
            if str(hw.HardwareType) != "GpuNvidia":
                continue
            ng_type = hw.GetType()
            fc_field = ng_type.GetField("_fanControls", flags)
            f_field = ng_type.GetField("_fans", flags)
            ctrl_field = ng_type.GetField("_controls", flags)
            if fc_field is None or f_field is None or ctrl_field is None:
                continue
            fc_arr = fc_field.GetValue(hw)
            f_arr = f_field.GetValue(hw)
            ctrl_arr = ctrl_field.GetValue(hw)
            if fc_arr is None:
                continue
            n_gpu = int(fc_arr.Length)
            for i in range(n_gpu):
                gctrl = fc_arr[i]
                fan_s = f_arr[i] if (f_arr is not None and i < f_arr.Length) else None
                ctrl_s = ctrl_arr[i] if (ctrl_arr is not None and i < ctrl_arr.Length) else None
                if gctrl is None or fan_s is None or ctrl_s is None:
                    continue
                gname = (fan_s.Name or "GPU Fan") + " #" + str(i + 1)
                gpu_ch_dict[i] = _make_channel(i, gname, fan_s, ctrl_s, gctrl)
            break

        _state["computer"] = c
        _state["channels"] = ch_dict
        _state["gpu_channels"] = gpu_ch_dict
        _state["cpu_package_sensor"] = pkg
        _state["cpu_core_temp_sensors"] = core_temps
        _state["cpu_thread_load_sensors"] = thread_loads
        _state["cpu_core_clock_sensors"] = core_clocks
        _state["cpu_bus_clock_sensor"] = bus_clock
        _state["available"] = True

        if _state["monitor_thread"] is None:
            t = threading.Thread(target=_watchdog_loop, daemon=True, name="thermal-watchdog")
            t.start()
            _state["monitor_thread"] = t
    except Exception as e:
        _state["error"] = type(e).__name__ + ": " + str(e)
        _logger.exception("fan control initialization failed")


def _init_on_worker():
    with _lock:
        _init_locked()
        return bool(_state["available"])


def _ensure():
    if _is_wedged():
        return False
    with _lock:
        if _state["available"]:
            return True
        if _state["error"]:
            return False
    try:
        return bool(_worker.submit(_init_on_worker).result(timeout=INIT_TIMEOUT_S))
    except concurrent.futures.TimeoutError:
        _mark_wedged("fan control initialization timed out (service restart required)")
        return False
    except Exception as e:
        _set_runtime_error("fan control initialization failed: " + type(e).__name__ + ": " + str(e))
        return False


def _refresh_locked():
    now = time.time()
    if now - _state["last_update"] < 0.25:
        return
    c = _state["computer"]
    for hw in c.Hardware:
        hw.Update()
        for sub in hw.SubHardware:
            sub.Update()
    _state["last_update"] = now


def _channel_dict(ch):
    fv = ch["fan_sensor"].Value
    cv = ch["control_sensor"].Value
    return {
        "index": ch["index"],
        "name": ch["name"],
        "mode": ch["mode"],
        "rpm": round(float(fv), 1) if fv is not None else None,
        "pwm_percent": round(float(cv), 1) if cv is not None else None,
        "pwm_set": ch["pwm_set"],
    }


def _status_impl():
    with _lock:
        _refresh_locked()
        chs = [_channel_dict(_state["channels"][i])
               for i in sorted(_state["channels"].keys())]
    return {
        "available": True,
        "error": None,
        "channels": chs,
        "modes": list(MODES.keys()),
    }


def _thermal_overlay(d):
    d["thermal_emergency"] = _state["thermal_emergency"]
    d["peak_temp_c"] = _state["peak_temp_c"]
    d["critical_temp_c"] = CRITICAL_TEMP_C
    d["recovery_temp_c"] = RECOVERY_TEMP_C
    d["elevated"] = is_elevated()
    return d


def rescan():
    """Clear a failed (non-wedged) init so the next attempt retries discovery.

    Covers the case where the chip/driver was briefly unavailable at startup;
    a wedged native call still requires a service restart."""
    if _is_wedged():
        return {"ok": False, "error": "fan control wedged (service restart required)"}
    with _lock:
        if _state["available"]:
            return {"ok": True, "note": "fan control already active"}
        _state["error"] = None
    if _ensure():
        return {"ok": True, "note": "fan control initialized"}
    return {"ok": False, "error": _state["error"] or "fan control unavailable"}


def status():
    """Snapshot used by GET /api/cpu/fan and /api/stats."""
    if _is_wedged():
        return _thermal_overlay({
            "available": False,
            "error": _state["error"] or "fan control wedged (service restart required)",
            "channels": [], "modes": list(MODES.keys()),
        })
    if not _ensure():
        return _thermal_overlay({
            "available": False, "error": _state["error"],
            "channels": [], "modes": list(MODES.keys()),
        })
    try:
        return _thermal_overlay(_worker.submit(_status_impl).result(timeout=READ_TIMEOUT_S))
    except concurrent.futures.TimeoutError:
        _mark_wedged("fan control read timed out (service restart required)")
        return _thermal_overlay({
            "available": False,
            "error": _state["error"],
            "channels": [], "modes": list(MODES.keys()),
        })
    except Exception as e:
        msg = "fan control read failed: " + type(e).__name__ + ": " + str(e)
        _set_runtime_error(msg)
        return _thermal_overlay({
            "available": False,
            "error": msg,
            "channels": [], "modes": list(MODES.keys()),
        })


def _set_mode_impl(mode, channel):
    with _lock:
        if channel is None:
            channel = sorted(_state["channels"].keys())[0]
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return {"ok": False, "error": "channel must be an integer"}
        if channel not in _state["channels"]:
            return {"ok": False, "error": "no such channel " + str(channel),
                    "valid_channels": sorted(_state["channels"].keys())}

        ch = _state["channels"][channel]
        ctrl = ch["control"]
        target = MODES[mode]
        try:
            if target is None:
                ctrl.SetDefault()
                ch["pwm_set"] = None
            else:
                applied = max(SAFETY_FLOOR, int(target))
                _refresh_locked()
                pkg = _state["cpu_package_sensor"]
                if pkg is not None and pkg.Value is not None and pkg.Value > HOT_TEMP_C:
                    applied = max(applied, HOT_FLOOR)
                ctrl.SetSoftware(float(applied))
                ch["pwm_set"] = applied
            ch["mode"] = mode
            _refresh_locked()
            snapshot = _channel_dict(ch)
        except Exception as e:
            return {"ok": False, "error": type(e).__name__ + ": " + str(e)}
    return {"ok": True, "channel": snapshot}


def set_mode(mode, channel=None):
    """Apply a mode to a specific channel. channel=None means first channel."""
    mode = (mode or "").lower().strip()
    if mode not in MODES:
        return {"ok": False, "error": "unknown mode '" + str(mode) + "'",
                "valid": list(MODES.keys())}
    if _is_wedged():
        return {"ok": False, "error": "fan control wedged (service restart required)"}
    if _state["thermal_emergency"]:
        return {"ok": False,
                "error": "thermal emergency: CPU >= " + str(CRITICAL_TEMP_C) +
                         "C, all fans locked at 100% until <" + str(RECOVERY_TEMP_C) + "C"}
    if not _ensure():
        return {"ok": False, "error": _state["error"] or "fan control unavailable"}

    try:
        return _worker.submit(_set_mode_impl, mode, channel).result(timeout=WRITE_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        _mark_wedged("fan control write timed out after " + str(WRITE_TIMEOUT_S) + "s (service restart required)")
        return {"ok": False,
                "error": "write timed out after " + str(WRITE_TIMEOUT_S) + "s (service restart required)"}
    except Exception as e:
        msg = "fan control write failed: " + type(e).__name__ + ": " + str(e)
        _set_runtime_error(msg)
        return {"ok": False, "error": msg}


def _gpu_aggregate_snapshot():
    """Collapse N GPU coolers into one logical card. RTX FE-class cards
    expose front + rear as separate NVAPI fans but want to be driven in
    lockstep; users only want one toggle."""
    ch_list = [_state["gpu_channels"][i] for i in sorted(_state["gpu_channels"].keys())]
    if not ch_list:
        return None
    rpms = []
    pwms = []
    for c in ch_list:
        v = c["fan_sensor"].Value
        if v is not None:
            rpms.append(float(v))
        cv = c["control_sensor"].Value
        if cv is not None:
            pwms.append(float(cv))
    return {
        "index": 0,
        "name": "GPU Fans" + (" (" + str(len(ch_list)) + ")" if len(ch_list) > 1 else ""),
        "mode": ch_list[0]["mode"],
        "rpm": round(max(rpms), 1) if rpms else None,
        "pwm_percent": round(sum(pwms) / len(pwms), 1) if pwms else None,
        "pwm_set": ch_list[0]["pwm_set"],
    }


def _gpu_status_impl():
    with _lock:
        _refresh_locked()
        snap = _gpu_aggregate_snapshot()
    return {
        "available": True,
        "error": None,
        "channels": [snap] if snap is not None else [],
        "modes": list(MODES.keys()),
    }


def gpu_status():
    if _is_wedged():
        return _thermal_overlay({"available": False,
                "error": _state["error"] or "fan control wedged (service restart required)",
                "channels": [], "modes": list(MODES.keys())})
    if not _ensure():
        return _thermal_overlay({"available": False, "error": _state["error"],
                "channels": [], "modes": list(MODES.keys())})
    if not _state["gpu_channels"]:
        return _thermal_overlay({"available": False, "error": "no NVIDIA GPU fan controls discovered",
                "channels": [], "modes": list(MODES.keys())})
    try:
        return _thermal_overlay(_worker.submit(_gpu_status_impl).result(timeout=READ_TIMEOUT_S))
    except concurrent.futures.TimeoutError:
        _mark_wedged("GPU fan read timed out (service restart required)")
        return _thermal_overlay({"available": False, "error": "GPU fan read timed out (service restart)",
                "channels": [], "modes": list(MODES.keys())})
    except Exception as e:
        msg = "GPU fan read failed: " + type(e).__name__ + ": " + str(e)
        _set_runtime_error(msg)
        return _thermal_overlay({"available": False, "error": msg,
                "channels": [], "modes": list(MODES.keys())})


def _set_gpu_mode_impl(mode, channel):
    # channel arg ignored - GPU coolers are driven as one logical group
    with _lock:
        if not _state["gpu_channels"]:
            return {"ok": False, "error": "no GPU coolers"}
        target = MODES[mode]
        try:
            for ch in _state["gpu_channels"].values():
                ctrl = ch["control"]
                if target is None:
                    ctrl.SetDefault()
                    ch["pwm_set"] = None
                else:
                    applied = max(SAFETY_FLOOR, int(target))
                    ctrl.SetSoftware(float(applied))
                    ch["pwm_set"] = applied
                ch["mode"] = mode
            _refresh_locked()
            snapshot = _gpu_aggregate_snapshot()
        except Exception as e:
            return {"ok": False, "error": type(e).__name__ + ": " + str(e)}
    return {"ok": True, "channel": snapshot}


def set_gpu_mode(mode, channel=None):
    mode = (mode or "").lower().strip()
    if mode not in MODES:
        return {"ok": False, "error": "unknown mode '" + str(mode) + "'",
                "valid": list(MODES.keys())}
    if _is_wedged():
        return {"ok": False, "error": "fan control wedged (service restart required)"}
    if _state["thermal_emergency"]:
        return {"ok": False,
                "error": "thermal emergency: CPU >= " + str(CRITICAL_TEMP_C) +
                         "C, all fans locked at 100% until <" + str(RECOVERY_TEMP_C) + "C"}
    if not _ensure():
        return {"ok": False, "error": _state["error"] or "fan control unavailable"}
    if not _state["gpu_channels"]:
        return {"ok": False, "error": "no NVIDIA GPU fan controls discovered"}
    try:
        return _worker.submit(_set_gpu_mode_impl, mode, channel).result(timeout=WRITE_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        _mark_wedged("GPU fan write timed out after " + str(WRITE_TIMEOUT_S) + "s (service restart required)")
        return {"ok": False,
                "error": "GPU write timed out after " + str(WRITE_TIMEOUT_S) + "s"}
    except Exception as e:
        msg = "GPU fan write failed: " + type(e).__name__ + ": " + str(e)
        _set_runtime_error(msg)
        return {"ok": False, "error": msg}


def _cpu_telemetry_impl():
    with _lock:
        _refresh_locked()
        pkg_s = _state["cpu_package_sensor"]
        pkg = float(pkg_s.Value) if pkg_s is not None and pkg_s.Value is not None else None
        cores = []
        for idx in sorted((_state.get("cpu_core_temp_sensors") or {}).keys()):
            v = _state["cpu_core_temp_sensors"][idx].Value
            cores.append(round(float(v), 1) if v is not None else None)
        threads = []
        for idx in sorted((_state.get("cpu_thread_load_sensors") or {}).keys()):
            v = _state["cpu_thread_load_sensors"][idx].Value
            threads.append(round(float(v), 1) if v is not None else None)
        clocks = []
        for idx in sorted((_state.get("cpu_core_clock_sensors") or {}).keys()):
            v = _state["cpu_core_clock_sensors"][idx].Value
            clocks.append(round(float(v), 1) if v is not None and float(v) > 0 else None)
        bus_s = _state.get("cpu_bus_clock_sensor")
        bus = float(bus_s.Value) if bus_s is not None and bus_s.Value is not None else None
    valid_clocks = [c for c in clocks if c is not None]
    avg_clock = round(sum(valid_clocks) / len(valid_clocks), 1) if valid_clocks else None
    return {"package_c": round(pkg, 1) if pkg is not None else None,
            "cores_c": cores,
            "threads_load": threads,
            "clock_mhz": avg_clock,
            "clocks_mhz": clocks,
            "clock_min_mhz": min(valid_clocks) if valid_clocks else None,
            "clock_max_mhz": max(valid_clocks) if valid_clocks else None,
            "bus_clock_mhz": round(bus, 1) if bus is not None else None}


def get_cpu_telemetry():
    """LHM-sourced per-core temps + per-thread loads. None on failure."""
    if _is_wedged() or not _ensure():
        return None
    try:
        return _worker.submit(_cpu_telemetry_impl).result(timeout=READ_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        _mark_wedged("CPU temperature read timed out (service restart required)")
        return None
    except Exception as e:
        _logger.warning("CPU telemetry read failed: %s: %s", type(e).__name__, e)
        return None


# Back-compat alias
def get_cpu_temps():
    t = get_cpu_telemetry()
    if t is None:
        return None
    return {"package_c": t.get("package_c"), "cores_c": t.get("cores_c")}


def _release_all_impl():
    with _lock:
        if not _state["available"]:
            return
        for d in (_state["channels"], _state["gpu_channels"]):
            for ch in d.values():
                try:
                    ch["control"].SetDefault()
                    ch["mode"] = "auto"
                    ch["pwm_set"] = None
                except Exception:
                    pass


def release_all():
    """Reset every Control to BIOS / driver default. Call on shutdown."""
    _stop_event.set()
    if _is_wedged():
        return
    with _lock:
        if not _state["available"]:
            return
    try:
        _worker.submit(_release_all_impl).result(timeout=WRITE_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        _mark_wedged("fan release timed out during service shutdown")
    except Exception as e:
        _logger.warning("fan release failed during service shutdown: %s: %s", type(e).__name__, e)



def _force_all_pwm_impl(pct):
    with _lock:
        for d in (_state["channels"], _state["gpu_channels"]):
            for ch in d.values():
                try:
                    ch["control"].SetSoftware(float(pct))
                    ch["pwm_set"] = int(pct)
                except Exception:
                    pass


def _restore_modes_impl(saved):
    with _lock:
        for key, v in saved.items():
            devname, k = key
            d = _state["channels"] if devname == "cpu" else _state["gpu_channels"]
            ch = d.get(k)
            if ch is None:
                continue
            mode = v.get("mode", "auto")
            target = MODES.get(mode)
            try:
                if target is None:
                    ch["control"].SetDefault()
                    ch["pwm_set"] = None
                else:
                    applied = max(SAFETY_FLOOR, int(target))
                    ch["control"].SetSoftware(float(applied))
                    ch["pwm_set"] = applied
                ch["mode"] = mode
            except Exception:
                pass


def _watchdog_loop():
    """Poll CPU temp; force 100% on all channels above CRITICAL_TEMP_C,
    restore previous modes once below RECOVERY_TEMP_C."""
    while not _stop_event.is_set():
        # Sleep first so we don't hammer on startup races
        if _stop_event.wait(WATCHDOG_POLL_S):
            return
        if _is_wedged() or not _state["available"]:
            continue
        try:
            tele = get_cpu_telemetry()
            if tele is None:
                continue
            cores = [c for c in (tele.get("cores_c") or []) if c is not None]
            pkg = tele.get("package_c")
            samples = list(cores)
            if pkg is not None:
                samples.append(pkg)
            if not samples:
                continue
            peak = max(samples)
            _state["peak_temp_c"] = round(peak, 1)

            in_emerg = _state["thermal_emergency"]
            if peak >= CRITICAL_TEMP_C and not in_emerg:
                with _lock:
                    saved = {}
                    for devname, d in (("cpu", _state["channels"]),
                                       ("gpu", _state["gpu_channels"])):
                        for k, ch in d.items():
                            saved[(devname, k)] = {"mode": ch["mode"], "pwm_set": ch["pwm_set"]}
                    _state["saved_modes_pre_emergency"] = saved
                    _state["thermal_emergency"] = True
                try:
                    _worker.submit(_force_all_pwm_impl, 100).result(timeout=WRITE_TIMEOUT_S)
                except concurrent.futures.TimeoutError:
                    _mark_wedged("thermal watchdog fan write timed out (service restart required)")
            elif peak < RECOVERY_TEMP_C and in_emerg:
                with _lock:
                    saved = _state.get("saved_modes_pre_emergency") or {}
                    _state["thermal_emergency"] = False
                if saved:
                    try:
                        _worker.submit(_restore_modes_impl, saved).result(timeout=WRITE_TIMEOUT_S)
                    except concurrent.futures.TimeoutError:
                        _mark_wedged("thermal watchdog fan restore timed out (service restart required)")
                with _lock:
                    _state["saved_modes_pre_emergency"] = None
            elif in_emerg:
                # While in emergency, keep re-asserting 100% in case anything
                # else (BIOS curve trying to fight, intermittent write) slipped.
                try:
                    _worker.submit(_force_all_pwm_impl, 100).result(timeout=WRITE_TIMEOUT_S)
                except concurrent.futures.TimeoutError:
                    _mark_wedged("thermal watchdog fan write timed out (service restart required)")
        except Exception:
            pass
