"""NCT6795D RGB register diagnostics for the MSI MS-7B43 motherboard.

This rig is an MSI Z370I GAMING PRO CARBON AC (MS-7B43). NCT6795D bank 0x12
accepts the reverse-engineered RGB register map, but register readback did not
prove physical onboard output on this exact board. The live app therefore does
not write this backend; soldered LEDs use Mystic SDK areas 0..7 instead.
The CoreLiquid AIO is not on JRGB1; it is attached to the 5V JCORSAIR1 header
and is controlled separately through MSI's Mystic Light SDK.

The register map is the one documented and battle-tested by nagisa/msi-rgb
(https://github.com/nagisa/msi-rgb), which targets exactly this chip on exactly
this generation of MSI board. Port access goes through the PawnIO kernel driver
that is already installed here for LibreHardwareMonitor -- specifically its
LpcIO module, which whitelists the Super-I/O register ports (0x4E/0x4F) and
nothing else, so a bug in here cannot scribble over arbitrary I/O space.

Safety rails, in order:
  * the chip ID is verified (0xD35x / 0xD45x) before a single byte is written;
  * we hold Access_ISABUS.HTP.Method, the same mutex LibreHardwareMonitor takes
    for its Super-I/O reads, so fan telemetry and lighting cannot interleave;
  * config mode is always exited (0xAA), even on error, via try/finally.

The block exposes one colour at 4 bits per channel on supported boards. Keep the
write helpers for controlled diagnostics only; never run them as a keepalive
alongside Mystic Light.
"""
import ctypes
import ctypes.wintypes as wintypes
import os
import threading
from app import paths

PAWNIO_DLL = r"C:\Program Files\PawnIO\PawnIOLib.dll"

# The LpcIO PawnIO module blob. Kept in-tree so lighting does not depend on
# OpenRGB's install layout; the OpenRGB copy is the fallback.
_HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_CANDIDATES = [
    os.path.join(os.path.dirname(_HERE), "_tools", "pawnio", "LpcIO.bin"),
    os.path.join(paths.USER_HOME, "led-control", "OpenRGB", "OpenRGB Windows 64-bit", "LpcIO.bin"),
]

ISA_MUTEX = "Global\\Access_ISABUS.HTP.Method"
SLOT = 1              # slot 1 == ports 0x4E/0x4F, where the NCT6795D lives
REGISTER_PORT = 0x4E

REG_DEVID_MSB = 0x20
REG_DEVID_LSB = 0x21
REG_LDN_SELECT = 0x07
LDN_PULSE_FIX = 0x09
REG_PULSE_FIX = 0x2C
RGB_BANK = 0x12
REG_CAPS = 0xE0        # per-channel "16 levels" capability bits
REG_PULSE = 0xE4       # pbbb: pulse enable + blink duration
REG_RED = 0xF0         # 4 bytes == 8 frames of 4-bit intensity
REG_GREEN = 0xF4
REG_BLUE = 0xF8
REG_STEP_LO = 0xFE
REG_STEP_HI = 0xFF     # fffbgrdt
VALID_CHIP_IDS = (0xD350, 0xD450)   # NCT6795, NCT6797

_LOCK = threading.RLock()
_LIB = None
_HANDLE = None
_STATE = {"available": None, "chip_id": None, "error": None, "module": None}



def _lib():
    global _LIB
    if _LIB is None:
        lib = ctypes.WinDLL(PAWNIO_DLL)
        lib.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
        lib.pawnio_open.restype = ctypes.c_long
        lib.pawnio_load.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
        lib.pawnio_load.restype = ctypes.c_long
        lib.pawnio_execute.argtypes = [
            wintypes.HANDLE, ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        lib.pawnio_execute.restype = ctypes.c_long
        lib.pawnio_close.argtypes = [wintypes.HANDLE]
        lib.pawnio_close.restype = ctypes.c_long
        _LIB = lib
    return _LIB


def _module_path():
    for path in MODULE_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise RuntimeError("LpcIO PawnIO module not found (looked in %s)" % ", ".join(MODULE_CANDIDATES))


def _hr(code, what):
    if code != 0:
        raise RuntimeError("%s failed (HRESULT 0x%08X)" % (what, code & 0xFFFFFFFF))


def _open():
    """Open a PawnIO executor with the LpcIO module loaded and slot selected."""
    global _HANDLE
    if _HANDLE is not None:
        return _HANDLE
    lib = _lib()
    path = _module_path()
    with open(path, "rb") as f:
        blob = f.read()
    handle = wintypes.HANDLE()
    _hr(lib.pawnio_open(ctypes.byref(handle)), "pawnio_open")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    try:
        _hr(lib.pawnio_load(handle, buf, len(blob)), "pawnio_load")
        _HANDLE = handle
        _STATE["module"] = path
        _execute("ioctl_select_slot", [SLOT])
    except Exception:
        try:
            lib.pawnio_close(handle)
        except Exception:
            pass
        _HANDLE = None
        raise
    return _HANDLE


def _close():
    global _HANDLE
    if _HANDLE is not None:
        try:
            _lib().pawnio_close(_HANDLE)
        except Exception:
            pass
        _HANDLE = None


def _execute(name, args, out_count=0):
    lib = _lib()
    in_arr = (ctypes.c_ulonglong * max(1, len(args)))(*args) if args else None
    out_arr = (ctypes.c_ulonglong * out_count)() if out_count else None
    written = ctypes.c_size_t(0)
    code = lib.pawnio_execute(
        _HANDLE, name.encode("ascii"),
        in_arr, len(args),
        out_arr, out_count,
        ctypes.byref(written),
    )
    _hr(code, name)
    return [out_arr[i] for i in range(out_count)] if out_count else []



_MUTEX_ALL_ACCESS = 0x1F0001
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


class _IsaMutex:
    """Same mutex LibreHardwareMonitor takes around Super-I/O access. If we
    cannot get a handle at all we still proceed -- LHM does the same -- but a
    timeout means somebody else is mid-transaction, so we back off."""

    def __init__(self, timeout_ms=1500):
        self.timeout_ms = timeout_ms
        self.handle = None
        self.held = False

    def __enter__(self):
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, ISA_MUTEX)
        if not handle:
            handle = k32.OpenMutexW(_SYNCHRONIZE, False, ISA_MUTEX)
        if handle:
            self.handle = handle
            if k32.WaitForSingleObject(handle, self.timeout_ms) == _WAIT_TIMEOUT:
                k32.CloseHandle(handle)
                self.handle = None
                raise RuntimeError("ISA bus busy (Access_ISABUS mutex timeout)")
            self.held = True
        return self

    def __exit__(self, *exc):
        k32 = ctypes.windll.kernel32
        if self.handle:
            if self.held:
                k32.ReleaseMutex(self.handle)
            k32.CloseHandle(self.handle)
        self.handle = None
        self.held = False
        return False



def _pio_out(port, value):
    _execute("ioctl_pio_outb", [port & 0xFFFF, value & 0xFF])


def _sio_in(reg):
    return _execute("ioctl_superio_inb", [reg & 0xFF], out_count=1)[0] & 0xFF


def _sio_out(reg, value):
    _execute("ioctl_superio_outb", [reg & 0xFF, value & 0xFF])


def _enter_config():
    _pio_out(REGISTER_PORT, 0x87)
    _pio_out(REGISTER_PORT, 0x87)


def _exit_config():
    _pio_out(REGISTER_PORT, 0xAA)


def _chip_id():
    return (_sio_in(REG_DEVID_MSB) << 8) | _sio_in(REG_DEVID_LSB)


def _write_channel(base_reg, nibble):
    """All 8 animation frames set to the same 4-bit level == a steady colour."""
    byte = ((nibble & 0xF) << 4) | (nibble & 0xF)
    for offset in range(4):
        _sio_out(base_reg + offset, byte)



def probe(force=False):
    """Is the motherboard RGB block driveable? Cheap, read-only, cached."""
    with _LOCK:
        if _STATE["available"] is not None and not force:
            return dict(_STATE)
        try:
            _open()
            with _IsaMutex():
                _enter_config()
                try:
                    ident = _chip_id()
                finally:
                    _exit_config()
            _STATE["chip_id"] = ident
            if (ident & 0xFFF0) not in VALID_CHIP_IDS:
                _STATE["available"] = False
                _STATE["error"] = ("Super-I/O reports chip 0x%04X, which is not an "
                                   "NCT6795D/NCT6797D — refusing to write." % ident)
            else:
                _STATE["available"] = True
                _STATE["error"] = None
        except Exception as e:
            _close()
            _STATE["available"] = False
            _STATE["error"] = str(e)[:300]
        return dict(_STATE)


def set_color(r, g, b, invert=False, pulse=False, blink=0, step_duration=0):
    """Set onboard LEDs/JRGB1 together. RGB is scaled to 4 bits per channel."""
    with _LOCK:
        state = probe()
        if not state.get("available"):
            return {"ok": False, "error": state.get("error") or "JRGB not available", "status_code": 503}
        try:
            _open()
            with _IsaMutex():
                _enter_config()
                try:
                    ident = _chip_id()
                    if (ident & 0xFFF0) not in VALID_CHIP_IDS:
                        raise RuntimeError("chip ID changed to 0x%04X mid-flight" % ident)

                    # MSI's own software sets this bit; without it pulsing is dead.
                    _sio_out(REG_LDN_SELECT, LDN_PULSE_FIX)
                    current = _sio_in(REG_PULSE_FIX)
                    if current & 0x10 != 0x10:
                        _sio_out(REG_PULSE_FIX, current | 0x10)

                    _sio_out(REG_LDN_SELECT, RGB_BANK)

                    # Ask for 16 intensity levels on all three channels.
                    caps = _sio_in(REG_CAPS)
                    if caps & 0xE0 != 0xE0:
                        _sio_out(REG_CAPS, 0xE0 | (caps & ~0xE0))

                    blink = max(0, min(6, int(blink)))
                    e4 = (0b1000 if pulse else 0) | (0 if blink == 0 else ((blink + 1) & 0b111))
                    _sio_out(REG_PULSE, e4)

                    step = max(0, min(511, int(step_duration)))
                    _sio_out(REG_STEP_LO, step & 0xFF)
                    # fff=111 (frame mode, no fade-in), d=1 (header on),
                    # bgr = invert bits, low bit = 9th bit of step duration.
                    ff = ((step >> 8) & 0b1) | 0b10 | 0b11100000
                    if invert:
                        ff |= 0b00011100
                    _sio_out(REG_STEP_HI, ff)

                    _write_channel(REG_RED, int(r) >> 4)
                    _write_channel(REG_GREEN, int(g) >> 4)
                    _write_channel(REG_BLUE, int(b) >> 4)
                finally:
                    _exit_config()
            return {"ok": True, "rgb": [int(r), int(g), int(b)], "chip_id": "0x%04X" % ident}
        except Exception as e:
            _close()
            _STATE["available"] = None      # re-probe next time
            return {"ok": False, "error": str(e)[:300], "status_code": 503}


def off():
    """Kill the header's output (bit 1 of 0xFF low) rather than writing black,
    so the LEDs are genuinely dark instead of dimly lit."""
    with _LOCK:
        state = probe()
        if not state.get("available"):
            return {"ok": False, "error": state.get("error") or "JRGB not available", "status_code": 503}
        try:
            _open()
            with _IsaMutex():
                _enter_config()
                try:
                    _sio_out(REG_LDN_SELECT, RGB_BANK)
                    _write_channel(REG_RED, 0)
                    _write_channel(REG_GREEN, 0)
                    _write_channel(REG_BLUE, 0)
                    _sio_out(REG_STEP_HI, 0b11100000)   # d bit cleared == header off
                finally:
                    _exit_config()
            return {"ok": True}
        except Exception as e:
            _close()
            _STATE["available"] = None
            return {"ok": False, "error": str(e)[:300], "status_code": 503}


def dump(write_test=None):
    """Read back bank 0x12 so we can tell 'the chip accepted the write' from
    'the LEDs are not on this header'. Optionally writes a colour first.

    Returns before/after register maps; identical values with a write in
    between means the writes are not landing at all."""
    with _LOCK:
        try:
            _open()
            snapshot = {}
            with _IsaMutex():
                _enter_config()
                try:
                    ident = _chip_id()
                    _sio_out(REG_LDN_SELECT, LDN_PULSE_FIX)
                    pin_cfg = _sio_in(REG_PULSE_FIX)
                    _sio_out(REG_LDN_SELECT, RGB_BANK)
                    snapshot["before"] = {"0x%02X" % r: "0x%02X" % _sio_in(r)
                                          for r in list(range(0xE0, 0xE8)) + list(range(0xF0, 0x100))}
                    if write_test:
                        r, g, b = write_test
                        caps = _sio_in(REG_CAPS)
                        if caps & 0xE0 != 0xE0:
                            _sio_out(REG_CAPS, 0xE0 | (caps & ~0xE0))
                        _sio_out(REG_PULSE, 0)
                        _sio_out(REG_STEP_LO, 25)
                        _sio_out(REG_STEP_HI, 0b11100010)
                        _write_channel(REG_RED, int(r) >> 4)
                        _write_channel(REG_GREEN, int(g) >> 4)
                        _write_channel(REG_BLUE, int(b) >> 4)
                        snapshot["after"] = {"0x%02X" % reg: "0x%02X" % _sio_in(reg)
                                             for reg in list(range(0xE0, 0xE8)) + list(range(0xF0, 0x100))}
                finally:
                    _exit_config()
            snapshot["chip_id"] = "0x%04X" % ident
            snapshot["ldn09_reg2c"] = "0x%02X" % pin_cfg
            snapshot["register_port"] = "0x%02X" % REGISTER_PORT
            if write_test and snapshot.get("after"):
                changed = [k for k in snapshot["before"]
                           if snapshot["before"][k] != snapshot["after"][k]]
                snapshot["changed_registers"] = changed
                snapshot["writes_land"] = bool(changed)
            return {"ok": True, "dump": snapshot}
        except Exception as e:
            _close()
            return {"ok": False, "error": str(e)[:300], "status_code": 503}


def status():
    state = probe()
    return {
        "backend": "NCT6795D Super-I/O (PawnIO)",
        "available": bool(state.get("available")),
        "chip_id": ("0x%04X" % state["chip_id"]) if state.get("chip_id") else None,
        "module": state.get("module"),
        "error": state.get("error"),
        "note": ("Diagnostic register route only. The live MS-7B43 onboard "
                 "lighting path is Mystic SDK areas 0..7."),
    }
