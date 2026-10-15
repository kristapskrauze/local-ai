"""RGB lighting for Dobby Console.

Routes one scene through MSI Center's atomic profile transaction for the
AIO on JCORSAIR1, plus a local OpenRGB server
(127.0.0.1:6742) for supported peripherals. The GMMK uses its own native HID
driver. The public Mystic SDK is deliberately not used live: its per-area
setters visibly chase across this MS-7B43 board and the last call erases the
zones written before it.

The engine thread turns rig state into colour: temperature ramps, GPU load,
or "ai" mode where the case breathes while idle and pulses while a model is
generating. Writes are de-duplicated, so a static/idle mode costs zero I/O.
"""
import colorsys
import json
import math
import os
import socket
import struct
import threading
import time

from app import gmmk, msi_center

HOST = os.environ.get("RIG_OPENRGB_HOST", "127.0.0.1")
PORT = int(os.environ.get("RIG_OPENRGB_PORT", "6742"))
CLIENT_NAME = "Dobby Console"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "rgb.json")


MAGIC = b"ORGB"
_HDR = struct.Struct("<4sIII")
_HDR_LEN = _HDR.size

PKT_CONTROLLER_COUNT = 0
PKT_CONTROLLER_DATA = 1
PKT_PROTOCOL_VERSION = 40
PKT_SET_CLIENT_NAME = 50
PKT_UPDATE_LEDS = 1050
PKT_UPDATE_ZONE_LEDS = 1051
PKT_SET_CUSTOM_MODE = 1100
PKT_UPDATE_MODE = 1101      # switch a device to one of its hardware modes

# We deliberately negotiate down to protocol 1. The server then serialises
# device descriptions in the old, stable layout (no per-mode brightness fields,
# no zone segments), which keeps the parser below small and version-proof.
CLIENT_PROTOCOL = 1

DEVICE_TYPES = [
    "Motherboard", "DRAM", "GPU", "Cooler", "LED Strip", "Keyboard", "Mouse",
    "Mousemat", "Headset", "Headset Stand", "Gamepad", "Light", "Speaker",
    "Virtual", "Storage", "Case", "Microphone", "Accessory", "Keypad", "Unknown",
]
# Anything bolted into the case gets driven by default; peripherals (mouse,
# keyboard, headset) keep whatever lighting the operator already set on them.
DEFAULT_ON_TYPES = {0, 1, 2, 3, 4, 11, 14, 15}

MODES = ["off", "static", "breathe", "rainbow", "temp", "load", "ai", "goals"]

DEFAULTS = {
    "enabled": True,
    "mode": "ai",
    "color": "#4b9bff",
    "brightness": 70,
    "speed": 50,
    "interval_ms": 120,
    "alert": True,
    "alert_gpu_temp": 84.0,
    "alert_cpu_temp": 95.0,
    "disabled_devices": [],   # by device name, so it survives re-enumeration
    # Per-key pattern for multi-LED devices (the keyboard is a 126-LED matrix,
    # so one flat colour wastes it). See KEY_EFFECTS. NB set_config() drops any
    # key not present in DEFAULTS, so a new option MUST be listed here.
    "key_effect": "solid",
    # MSI's board profile records and physical JRGB1 12 V strip header are
    # committed atomically with the JCORSAIR header. The JRGB1 profile can be
    # healthy even when no 12 V strip is physically attached.
    "jrgb": True,
    "jrgb_invert": False,     # legacy NCT diagnostic option; not used live
    # JCORSAIR profile, where the CoreLiquid AIO actually lives. It is changed
    # in the same MSI Center transaction as the motherboard zones.
    "mystic": True,
    # Hold the AIO at a FIXED colour instead of the scene colour. Empty = follow
    # the scene, which is the old behaviour. Exists because both MSI groups are
    # committed in one transaction from one `want` colour, so without an
    # override the AIO can never differ from the rest of the rig.
    "mystic_color": "",
    # Legacy v66/v67 route list, retained for API/config compatibility. The
    # live backend groups onboard + JCORSAIR records into one profile commit;
    # individual area writes are intentionally impossible.
    "mystic_areas": ["MSI_MB:9"],
    # The GMMK is driven by app/gmmk.py over raw HID, NOT through OpenRGB --
    # OpenRGB's writes for this model flash and are then lost to the firmware's
    # stored profile. Off by default like the other peripherals.
    "keyboard": False,
    # The 3090 FE's GEFORCE RTX label is a SINGLE COLOR zone: fixed white, no
    # colour control. False holds just that zone black while the small RGBW
    # LEDs beside it keep following the engine.
    "gpu_label": True,
}

# MSI's SetLedColor measured at ~1145 ms per call on this rig — under one write
# per second. It therefore CANNOT sit in the engine's 120 ms tick; it gets a
# worker thread with a coalescing single-slot queue instead, so fast effects
# still animate the GPU smoothly while the AIO follows at roughly 1 Hz.
MSI_PROFILE_MIN_INTERVAL = 1.3
MSI_PROFILE_AUDIT_SECONDS = 5.0

MYSTIC_ONBOARD_AREAS = frozenset("MSI_MB:%d" % area for area in range(8))
MYSTIC_AIO_AREA = "MSI_MB:9"
MYSTIC_DRIVE_DEFAULT = sorted(MYSTIC_ONBOARD_AREAS | {MYSTIC_AIO_AREA})
MYSTIC_SAFE_AREAS = frozenset(MYSTIC_DRIVE_DEFAULT)

class OpenRGBError(Exception):
    pass


class OpenRGBClient:
    """Minimal blocking OpenRGB SDK client. Not thread-safe; guard with _LOCK."""

    def __init__(self, host=HOST, port=PORT, timeout=4.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.protocol = 0

    def _send(self, device_idx, packet_id, data=b""):
        if self.sock is None:
            raise OpenRGBError("not connected")
        self.sock.sendall(_HDR.pack(MAGIC, device_idx, packet_id, len(data)) + data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OpenRGBError("server closed the connection")
            buf += chunk
        return buf

    def _read_packet(self):
        magic, dev, pkt, size = _HDR.unpack(self._recv_exact(_HDR_LEN))
        if magic != MAGIC:
            raise OpenRGBError("bad packet magic %r" % (magic,))
        return dev, pkt, (self._recv_exact(size) if size else b"")

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._send(0, PKT_SET_CLIENT_NAME, CLIENT_NAME.encode("utf-8") + b"\x00")
        self._send(0, PKT_PROTOCOL_VERSION, struct.pack("<I", CLIENT_PROTOCOL))
        _, pkt, data = self._read_packet()
        server_version = struct.unpack("<I", data[:4])[0] if pkt == PKT_PROTOCOL_VERSION and len(data) >= 4 else 0
        self.protocol = min(server_version, CLIENT_PROTOCOL)
        return self.protocol

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def controller_count(self):
        self._send(0, PKT_CONTROLLER_COUNT)
        _, _, data = self._read_packet()
        return struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0

    def controller_data(self, index):
        payload = struct.pack("<I", self.protocol) if self.protocol >= 1 else b""
        self._send(index, PKT_CONTROLLER_DATA, payload)
        _, _, data = self._read_packet()
        return _parse_controller(data, self.protocol, index)

    def set_custom_mode(self, index):
        self._send(index, PKT_SET_CUSTOM_MODE)

    def update_leds(self, index, colors):
        body = struct.pack("<H", len(colors)) + b"".join(struct.pack("<I", c) for c in colors)
        self._send(index, PKT_UPDATE_LEDS, struct.pack("<I", len(body) + 4) + body)

    def update_zone_leds(self, index, zone, colors):
        body = struct.pack("<IH", zone, len(colors)) + b"".join(struct.pack("<I", c) for c in colors)
        self._send(index, PKT_UPDATE_ZONE_LEDS, struct.pack("<I", len(body) + 4) + body)

    def update_mode(self, index, mode_index, blob, color=None):
        """Switch a device to one of its HARDWARE modes.

        The client could only ever send SET_CUSTOM_MODE, so a device whose
        per-key Custom path is broken (the GMMK: OpenRGB detects 126 LEDs,
        reports the write as fine, and nothing lights) had no other route.
        Hardware modes are what the vendor software drives, so they work where
        Custom does not.

        `blob` is the mode's ORIGINAL bytes from controller_data — the struct
        has to go back whole, with only the colour swapped, or the flags and
        speed fields end up wrong.
        """
        raw = bytearray(blob["raw"])
        if color is not None and blob.get("num_colors"):
            off = blob["colors_off"] + 2          # past the num_colors u16
            for i in range(blob["num_colors"]):
                struct.pack_into("<I", raw, off + 4 * i, color)
        body = struct.pack("<I", mode_index) + bytes(raw)
        self._send(index, PKT_UPDATE_MODE, struct.pack("<I", len(body) + 4) + body)


def _read_str(buf, off):
    (length,) = struct.unpack_from("<H", buf, off)
    off += 2
    raw = buf[off:off + length]
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace"), off + length


def _parse_controller(buf, protocol, index):
    """Parse a protocol<=1 device description. Only the identity/zone/LED-count
    fields are kept -- that is everything the console needs to drive it."""
    off = 0
    (_size, dev_type) = struct.unpack_from("<Ii", buf, off)
    off += 8
    name, off = _read_str(buf, off)
    vendor = ""
    if protocol >= 1:
        vendor, off = _read_str(buf, off)
    description, off = _read_str(buf, off)
    _version, off = _read_str(buf, off)
    _serial, off = _read_str(buf, off)
    location, off = _read_str(buf, off)

    (num_modes, active_mode) = struct.unpack_from("<Hi", buf, off)
    off += 6
    modes = []
    mode_blobs = []
    for _ in range(num_modes):
        start = off
        mode_name, off = _read_str(buf, off)
        # value, flags, speed_min, speed_max, colors_min, colors_max,
        # speed, direction, color_mode
        colours_at = off + 36
        off += 36
        (num_colors,) = struct.unpack_from("<H", buf, off)
        off += 2 + 4 * num_colors
        modes.append(mode_name)
        # Keep the RAW bytes: UPDATE_MODE has to send the whole mode struct
        # back, and re-synthesising one by hand gets the flags/speed wrong.
        mode_blobs.append({"raw": bytes(buf[start:off]),
                           "colors_off": colours_at - start,
                           "num_colors": num_colors})

    (num_zones,) = struct.unpack_from("<H", buf, off)
    off += 2
    zones = []
    for _ in range(num_zones):
        zone_name, off = _read_str(buf, off)
        (zone_type, _lmin, _lmax, leds_count, matrix_len) = struct.unpack_from("<iIIIH", buf, off)
        off += 18 + matrix_len
        zones.append({"name": zone_name, "type": zone_type, "leds": leds_count})

    (num_leds,) = struct.unpack_from("<H", buf, off)
    off += 2
    for _ in range(num_leds):
        _led_name, off = _read_str(buf, off)
        off += 4

    return {
        "index": index,
        "name": name,
        "vendor": vendor,
        "description": description,
        "location": location,
        "type": dev_type,
        "type_name": DEVICE_TYPES[dev_type] if 0 <= dev_type < len(DEVICE_TYPES) else "Unknown",
        "leds": num_leds,
        "zones": zones,
        "modes": modes,
        "active_mode": modes[active_mode] if 0 <= active_mode < len(modes) else "",
        "mode_blobs": mode_blobs,
        "active_mode_index": active_mode,
    }



def pack_rgb(r, g, b):
    """OpenRGB stores colours as 0x00BBGGRR."""
    return (int(b) & 0xFF) << 16 | (int(g) & 0xFF) << 8 | (int(r) & 0xFF)


def hex_to_rgb(value, fallback=(75, 155, 255)):
    try:
        s = str(value).strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return fallback
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return fallback


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % (int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF, int(rgb[2]) & 0xFF)


def hsv_to_rgb(h, s, v):
    """h in degrees, s/v in 0..1 -> 0..255 tuple."""
    h = float(h) % 360.0
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return (round((r + m) * 255), round((g + m) * 255), round((b + m) * 255))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _ramp(value, stops):
    """stops = [(value, (r,g,b)), ...] ascending; linear interpolation."""
    if value is None:
        return stops[0][1]
    if value <= stops[0][0]:
        return stops[0][1]
    for i in range(len(stops) - 1):
        v0, c0 = stops[i]
        v1, c1 = stops[i + 1]
        if value <= v1:
            span = (v1 - v0) or 1.0
            return _lerp(c0, c1, (value - v0) / span)
    return stops[-1][1]


TEMP_STOPS = [(40.0, (0, 90, 255)), (60.0, (0, 225, 140)), (74.0, (255, 190, 0)), (86.0, (255, 24, 0))]
LOAD_STOPS = [(0.0, (0, 70, 255)), (50.0, (150, 0, 255)), (100.0, (255, 0, 110))]
# Goal progress: nowhere near -> deep red, halfway -> amber, done -> green.
GOAL_STOPS = [(0.0, (220, 40, 30)), (50.0, (230, 165, 30)), (100.0, (40, 210, 90))]


def _scale(rgb, factor):
    f = max(0.0, min(1.0, factor))
    return (round(rgb[0] * f), round(rgb[1] * f), round(rgb[2] * f))



_LOCK = threading.RLock()
_CFG = dict(DEFAULTS)
_CLIENT = None
_DEVICES = []
_LAST_PUSH = {}          # device index -> packed colour last written
# device index -> (colour, phase, effect). Separate from _LAST_PUSH because
# status() unpacks _LAST_PUSH as an int for last_color.
_LAST_KEY = {}
KEY_ANIM_MIN_INTERVAL = 0.2
_LAST_KEY_WRITE = {"t": 0.0}
KEEPALIVE_SECONDS = 2.0
_LAST_DEV_WRITE = {}     # device index -> monotonic time of last actual write
_ACTIVITY = {}           # label -> expiry timestamp (0 = sticky/active)
_RECONNECT = {"next": 0.0, "delay": 2.0}   # OpenRGB reconnect backoff
_MYSTIC = {"want": None, "last": None, "last_signature": None,
           "ok": False, "error": None, "t": 0.0, "writes": 0,
           # A disabled MSI route must not be turned into an implicit black
           # transaction during startup.  The old True default caused every
           # restart to commit an all-off profile and could make LEDKeeper2
           # flash/crash while the route was supposedly disabled.
           "thread": None, "force": False, "profile": None,
           "audit_t": 0.0}
_STATE_PROVIDER = None
_ENGINE = None
_STOP = threading.Event()
_STATUS = {
    "connected": False,
    "error": None,
    "server": "%s:%d" % (HOST, PORT),
    "protocol": None,
    "last_push": None,
    "last_color": None,
    "pushes": 0,
    "engine": False,
    "jrgb_error": None,
}


def set_state_provider(fn):
    """main.py injects a callable returning the rig vitals dict."""
    global _STATE_PROVIDER
    _STATE_PROVIDER = fn


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in DEFAULTS:
                    _CFG[k] = v
            # Keep only the verified onboard (0..7) and AIO (9) routes. Other
            # JCORSAIR entries are alternate profiles for the same header.
            raw_areas = _CFG.get("mystic_areas")
            if raw_areas is not None:
                _CFG["mystic_areas"] = sorted(
                    str(value) for value in raw_areas
                    if str(value) in MYSTIC_SAFE_AREAS)
    except FileNotFoundError:
        pass
    except Exception as e:
        _STATUS["error"] = "config read failed: %s" % e


def _save_config():
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CFG, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception as e:
        _STATUS["error"] = "config write failed: %s" % e
        return False


_load_config()



def _disconnect(reason=None):
    global _CLIENT
    with _LOCK:
        if _CLIENT:
            _CLIENT.close()
        _CLIENT = None
        _LAST_PUSH.clear()
        _STATUS["connected"] = False
        _STATUS["protocol"] = None
        if reason:
            _STATUS["error"] = reason


def _ensure_client():
    """Connect (and enumerate) if needed. Returns the client or raises.

    Backs off between attempts: with the JRGB header carrying the lighting on
    its own, a stopped OpenRGB service must not mean a connect() syscall on
    every engine tick."""
    global _CLIENT, _DEVICES
    if _CLIENT is not None:
        return _CLIENT
    now = time.time()
    if now < _RECONNECT["next"]:
        raise OpenRGBError("OpenRGB server unavailable (retry in %.0fs)" % (_RECONNECT["next"] - now))
    try:
        client = OpenRGBClient()
        client.connect()
    except Exception:
        _RECONNECT["delay"] = min(30.0, _RECONNECT["delay"] * 1.7)
        _RECONNECT["next"] = now + _RECONNECT["delay"]
        raise
    _RECONNECT["delay"] = 2.0
    _RECONNECT["next"] = 0.0
    count = client.controller_count()
    devices = []
    for i in range(count):
        try:
            devices.append(client.controller_data(i))
        except Exception as e:
            devices.append({
                "index": i, "name": "device %d (unreadable)" % i, "vendor": "",
                "description": str(e), "location": "", "type": 19,
                "type_name": "Unknown", "leds": 0, "zones": [], "modes": [],
                "active_mode": "",
            })
    _CLIENT = client
    _DEVICES = devices
    _LAST_PUSH.clear()
    _STATUS.update({"connected": True, "protocol": client.protocol, "error": None})
    return client


GMMK_TAGS = ("evision", "0c45:652f")


def _is_gmmk(dev):
    name = str(dev.get("name") or "").lower()
    return any(tag in name for tag in GMMK_TAGS)


def _targets():
    """Devices the engine is allowed to drive, honouring the disable list."""
    off = set(_CFG.get("disabled_devices") or [])
    out = []
    for d in _DEVICES:
        if d["leds"] <= 0:
            continue
        if d["name"] in off:
            continue
        # The GMMK is ours (app/gmmk.py), never OpenRGB's -- letting both write
        # would have the native driver's committed colour fighting OpenRGB's
        # transient one. Excluded here rather than via disabled_devices so that
        # enabling it in the UI cannot resurrect the broken path.
        if _is_gmmk(d):
            continue
        out.append(d)
    return out


def _default_disabled():
    """Peripherals start disabled so we do not hijack the mouse/keyboard."""
    return [d["name"] for d in _DEVICES if d["type"] not in DEFAULT_ON_TYPES]



_STATE_CACHE = {"t": 0.0, "v": {}}


def _rig_state():
    now = time.time()
    if now - _STATE_CACHE["t"] < 2.0:
        return _STATE_CACHE["v"]
    value = {}
    try:
        if _STATE_PROVIDER:
            got = _STATE_PROVIDER()
            if isinstance(got, dict):
                value = got
    except Exception as e:
        _STATUS["error"] = "state provider failed: %s" % e
    _STATE_CACHE["t"] = now
    _STATE_CACHE["v"] = value
    return value


def note_activity(label, active=True, ttl=8.0):
    """Flag AI work in flight so 'ai' mode can react. ttl guards against a
    stream that dies without its matching note_activity(False)."""
    with _LOCK:
        if active:
            _ACTIVITY[label] = time.time() + max(1.0, float(ttl))
        else:
            _ACTIVITY.pop(label, None)


def _busy():
    now = time.time()
    with _LOCK:
        for label, expiry in list(_ACTIVITY.items()):
            if expiry < now:
                _ACTIVITY.pop(label, None)
        return sorted(_ACTIVITY.keys())



def _compute_color(now):
    """Current colour for this tick, as (r, g, b). Pure -- no I/O."""
    mode = _CFG.get("mode", "static")
    base = hex_to_rgb(_CFG.get("color"))
    bright = max(0.0, min(1.0, float(_CFG.get("brightness", 70)) / 100.0))
    speed = max(1.0, float(_CFG.get("speed", 50)))
    state = _rig_state()

    gpu_t = state.get("gpu_temp")
    cpu_t = state.get("cpu_temp")

    if _CFG.get("alert"):
        hot = (gpu_t is not None and gpu_t >= float(_CFG.get("alert_gpu_temp", 84))) or \
              (cpu_t is not None and cpu_t >= float(_CFG.get("alert_cpu_temp", 95)))
        if hot:
            blink = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(now * 2.0 * math.pi * 1.6))
            return _scale((255, 20, 0), blink), True

    if mode == "off":
        return (0, 0, 0), False

    if mode == "static":
        return _scale(base, bright), False

    if mode == "breathe":
        period = 12.0 - 9.0 * (speed / 100.0)
        wave = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(now * 2.0 * math.pi / period))
        return _scale(base, bright * wave), False

    if mode == "rainbow":
        period = 60.0 - 54.0 * (speed / 100.0)
        return _scale(hsv_to_rgb((now / period) * 360.0, 1.0, 1.0), bright), False

    if mode == "temp":
        hottest = max([t for t in (gpu_t, cpu_t) if t is not None] or [0.0])
        return _scale(_ramp(hottest, TEMP_STOPS), bright), False

    if mode == "load":
        load = state.get("gpu_util")
        if load is None:
            load = state.get("cpu_load") or 0.0
        return _scale(_ramp(load, LOAD_STOPS), bright), False

    if mode == "goals":
        progress = state.get("goal_progress")
        if progress is None:
            return _scale((60, 60, 70), bright * 0.4), False   # nothing tracked yet
        # A gentle breath keeps it from looking like a dead status LED.
        wave = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(now * 2.0 * math.pi / 6.0))
        return _scale(_ramp(progress, GOAL_STOPS), bright * wave), False

    if mode == "ai":
        busy = _busy()
        if busy:
            # Generating: sweep cyan -> violet and pulse, faster than idle.
            hue = 190.0 + 90.0 * (0.5 + 0.5 * math.sin(now * 2.0 * math.pi / 3.0))
            pulse = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(now * 2.0 * math.pi / 1.1))
            return _scale(hsv_to_rgb(hue, 1.0, 1.0), bright * pulse), False
        if state.get("model_loaded"):
            # Model warm in VRAM: steady teal, dimmed.
            return _scale((0, 210, 180), bright * 0.55), False
        # Idle: very slow deep-blue breath.
        wave = 0.12 + 0.30 * (0.5 + 0.5 * math.sin(now * 2.0 * math.pi / 9.0))
        return _scale((0, 80, 235), bright * wave), False

    return _scale(base, bright), False


def _mystic_aio_override():
    """Fixed AIO colour as a quantised tuple, or None to follow the scene.

    Quantised with the same `& 0xF8` as _queue_mystic(): MSI normalises stored
    channels to 5 bits, so an unquantised override would never equal what
    LEDKeeper2 saved and every pass of the worker would look like a change and
    rewrite the profile once per interval, forever.
    """
    raw = str(_CFG.get("mystic_color") or "").strip()
    if not raw:
        return None
    r, g, b = hex_to_rgb(raw)
    return (r & 0xF8, g & 0xF8, b & 0xF8)


def _group_last_color(key):
    """Hex of the colour last committed for ONE MSI group.

    Falls back to the shared scene colour so a profile committed before the
    per-group keys existed still reports something rather than None. A group
    held at (0, 0, 0) correctly reports #000000, not the scene colour.
    """
    value = _MYSTIC.get(key)
    if value is None:
        value = _MYSTIC.get("last")
    return rgb_to_hex(value) if value else None


def _mystic_profile_healthy(profile, board_enabled, aio_enabled,
                            board_colour, aio_colour):
    """Whether live MSI state still represents the routes Dobby owns."""
    return msi_center.profile_is_current(
        profile, board_colour, aio_colour,
        board_enabled=board_enabled, aio_enabled=aio_enabled)


def _mystic_worker():
    """Commit the newest scene through MSI Center as one profile transaction.

    The engine may update ``want`` many times while LEDKeeper2 is applying a
    profile; this worker deliberately coalesces those frames. Most importantly,
    there is one write for both routes, never one write per onboard LED zone.
    """
    while not _STOP.is_set():
        try:
            routes_enabled = bool(_CFG.get("jrgb") or _CFG.get("mystic"))
            if not routes_enabled:
                # Releasing MSI ownership is intentionally non-destructive:
                # do not enqueue or commit a synthetic black profile.  MSI
                # Center/LEDKeeper2 may keep the last stable hardware state.
                _MYSTIC["force"] = False
                _MYSTIC["want"] = None
                _MYSTIC["last_signature"] = None
                _STOP.wait(1.0)
                continue
            want = _MYSTIC.get("want")
            if want is None:
                if not routes_enabled:
                    want = (0, 0, 0)
                    _MYSTIC["want"] = want
                else:
                    _STOP.wait(0.2)
                    continue

            override = _mystic_aio_override()
            board_colour = want if _CFG.get("jrgb") else (0, 0, 0)
            if not _CFG.get("mystic"):
                aio_colour = (0, 0, 0)
            else:
                aio_colour = want if override is None else override
            signature = (board_colour, aio_colour)
            forced = bool(_MYSTIC.pop("force", False))
            now = time.monotonic()
            if (not forced and signature == _MYSTIC.get("last_signature") and
                    now >= float(_MYSTIC.get("audit_t") or 0.0)):
                audit = msi_center.inspect_profile()
                cached = dict(_MYSTIC.get("profile") or {})
                cached.update(audit)
                _MYSTIC["profile"] = cached
                if not _mystic_profile_healthy(
                        audit, bool(_CFG.get("jrgb")),
                        bool(_CFG.get("mystic")), board_colour, aio_colour):
                    forced = True
                else:
                    _MYSTIC["audit_t"] = now + MSI_PROFILE_AUDIT_SECONDS
            if forced or signature != _MYSTIC.get("last_signature"):
                result = msi_center.apply_profile(
                    board_colour, aio_colour,
                    board_enabled=bool(_CFG.get("jrgb")),
                    aio_enabled=bool(_CFG.get("mystic")),
                    force=forced)
                _MYSTIC["profile"] = result
                _MYSTIC["ok"] = bool(result.get("available") and
                                      result.get("acknowledged") and
                                      result.get("committed"))
                if not _MYSTIC["ok"]:
                    raise OpenRGBError(result.get("error") or
                                       "MSI Center did not acknowledge a fresh profile commit")
                if result.get("committed"):
                    _MYSTIC["writes"] += 1
                _MYSTIC["last"] = want
                # The two groups can now differ (mystic_color), so status()
                # cannot report the scene colour for both -- it would show a
                # cyan swatch for a purple AIO.
                _MYSTIC["last_board"] = board_colour
                _MYSTIC["last_aio"] = aio_colour
                _MYSTIC["last_signature"] = signature
                _MYSTIC["t"] = time.time()
                _MYSTIC["audit_t"] = time.monotonic() + MSI_PROFILE_AUDIT_SECONDS
            _MYSTIC["error"] = None
        except Exception as e:
            _MYSTIC["ok"] = False
            _MYSTIC["error"] = str(e)[:200]
            _MYSTIC["profile"] = msi_center.inspect_profile()
            _MYSTIC["force"] = True
            _STOP.wait(5.0)
            continue
        _STOP.wait(MSI_PROFILE_MIN_INTERVAL)


def _queue_mystic(rgb, mode):
    """Non-blocking hand-off to the atomic MSI Center profile worker."""
    if not (_CFG.get("jrgb") or _CFG.get("mystic")):
        return
    # MSI's profile transaction is a persistent hardware-style change, not a
    # 120 ms animation frame.  Sending temp/load/AI/rainbow frames repeatedly
    # makes LEDKeeper2 reapply the profile and can produce visible flashing or
    # crashes.  Keep MSI on one static scene; dynamic effects stay on the
    # keyboard/OpenRGB routes that can actually accept frame updates.
    if str(mode or "").lower() not in ("static", "off"):
        return
    # MSI Center normalises saved channels this way. Quantising before the
    # comparison makes static #ff0000 settle at (248, 0, 0), with zero
    # repeated writes after the first acknowledged transaction.
    _MYSTIC["want"] = (rgb[0] & 0xF8, rgb[1] & 0xF8, rgb[2] & 0xF8)

KEY_EFFECTS = ("solid", "gradient", "spectrum", "wave", "sweep")

# Driving the GMMK through OpenRGB was tried both ways and neither holds: the
# per-key Custom path and the hardware-mode path (UPDATE_MODE with the mode's
# original bytes) both flash the keys for an instant and then lose to the
# profile the firmware has stored. OpenRGB's write protocol for this model is
# incomplete -- it never closes the transaction the firmware needs to commit
# (upstream issue #3453). The keyboard is driven natively instead; see
# app/gmmk.py, and _targets() keeps it out of the OpenRGB path entirely.
#
# client.update_mode() is kept: it is a real gap in the client (we could only
# ever send SET_CUSTOM_MODE before) and works for devices OpenRGB supports.


def _key_colors(dev, rgb, phase):
    """Per-LED colours for a multi-LED device.

    A 126-key matrix painted one flat colour wastes the hardware. These run in
    OpenRGB's Custom mode, so the pattern is ours rather than one of the board's
    canned effects, and it still follows whatever the engine's colour is.
    """
    n = int(dev.get("leds") or 0)
    effect = str(_CFG.get("key_effect") or "solid").lower()
    if n <= 1 or effect == "solid" or effect not in KEY_EFFECTS:
        return [pack_rgb(*rgb)] * max(n, 0)

    h0f, s0, v0 = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    h0 = h0f * 360.0
    out = []
    for i in range(n):
        t = i / float(max(1, n - 1))
        if effect == "spectrum":                 # full rainbow across the keys
            out.append(pack_rgb(*hsv_to_rgb(((t + phase) % 1.0) * 360.0,
                                            1.0, max(v0, 0.25))))
        elif effect == "gradient":               # colour -> its complement
            out.append(pack_rgb(*hsv_to_rgb(h0 + 180.0 * t, s0, v0)))
        elif effect == "wave":                   # brightness wave, same hue
            k = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * (t * 2 - phase)))
            out.append(pack_rgb(*hsv_to_rgb(h0, s0, max(0.0, min(1.0, v0 * k)))))
        else:                                    # sweep: a lit band travelling
            d = abs(((t - phase) % 1.0) - 0.5)
            k = max(0.0, 1.0 - d * 4.0)
            out.append(pack_rgb(*hsv_to_rgb(h0, s0, max(0.05, v0 * k))))
    return out


def _dark_zones(dev):
    """Zone indices on this device that should be held black.

    Only the GPU's fixed-white label so far: it is a SINGLE COLOR zone that
    cannot take a colour, so the only choice is lit-white or dark, and the
    device-level Off mode would take the small RGBW LEDs down with it.
    """
    if _CFG.get("gpu_label", True):
        return set()
    out = set()
    for zi, zone in enumerate(dev.get("zones") or []):
        if "single color" in str(zone.get("name") or "").lower():
            out.add(zi)
    return out


def _push_openrgb(rgb, force=False):
    """Write to every enabled OpenRGB device: one colour, or a per-key pattern."""
    packed = pack_rgb(*rgb)
    client = _ensure_client()
    written = 0
    effect = str(_CFG.get("key_effect") or "solid").lower()
    animated = effect in ("spectrum", "wave", "sweep")
    # phase advances with time so the pattern moves, but is HELD between frames
    # so the 126-LED write happens at KEY_ANIM_MIN_INTERVAL, not on every tick
    now = time.time()
    if animated and now - _LAST_KEY_WRITE["t"] < KEY_ANIM_MIN_INTERVAL:
        phase = _LAST_KEY_WRITE.get("phase", 0.0)
    else:
        phase = round((now * (0.15 + (int(_CFG.get("speed") or 50) / 100.0) * 0.85)) % 1.0, 2)
        _LAST_KEY_WRITE["t"] = now
        _LAST_KEY_WRITE["phase"] = phase
    for dev in _targets():
        multi = int(dev.get("leds") or 0) > 1 and effect != "solid"
        key = (packed, phase if (multi and animated) else None, effect if multi else None)
        # Static OpenRGB colours are sticky on these devices.  Rewriting them
        # every two seconds is an unnecessary USB transaction and is visible
        # as a flash when another controller is also active.  Animated key
        # effects still get their bounded keepalive/frame cadence.
        stale = animated and ((now - _LAST_DEV_WRITE.get(dev["index"], 0.0)) >= KEEPALIVE_SECONDS)
        if not force and not stale and _LAST_KEY.get(dev["index"]) == key:
            continue
        try:
            client.set_custom_mode(dev["index"])
            dark = _dark_zones(dev)
            if dark:
                # Per-ZONE write. The 3090 FE carries two zones: "0 - RGBW"
                # (the small colour-capable LEDs) and "1 - SINGLE COLOR" (the
                # fixed-white GEFORCE RTX label, which ignores colour). The
                # device-level Off mode kills both, so masking the label means
                # writing its zone black while the rest still follows.
                for zi, zone in enumerate(dev.get("zones") or []):
                    n = int(zone.get("leds") or 0)
                    client.update_zone_leds(
                        dev["index"], zi, [0 if zi in dark else packed] * n)
            else:
                client.update_leds(dev["index"],
                                   _key_colors(dev, rgb, phase) if multi
                                   else [packed] * dev["leds"])
            _LAST_PUSH[dev["index"]] = packed
            _LAST_KEY[dev["index"]] = key
            _LAST_DEV_WRITE[dev["index"]] = now
            written += 1
        except Exception:
            _disconnect("write to %s failed" % dev["name"])
            raise
    return written


def _push_color(rgb, force=False):
    """Write one colour to both backends. They fail independently: the OpenRGB
    server being down must not stop the motherboard header, and vice versa.
    Only a total failure raises, so the engine does not back off needlessly."""
    written = 0
    errors = []
    any_ok = False

    # Hand off to the Mystic worker first: it is a queue write, not I/O, so it
    # costs nothing here and must not be skipped when a later backend throws.
    try:
        _queue_mystic(rgb, _CFG.get("mode"))
    except Exception as e:
        errors.append("mystic queue: %s" % e)

    # The keyboard rate-limits itself (every write commits to its onboard
    # profile), so a False here usually means "nothing to do", not a failure.
    # any_ok is therefore keyed on the device being present, NOT on this
    # returning True -- otherwise a steady colour would mask a genuine
    # all-backends-down condition and stop the engine from backing off.
    if _CFG.get("keyboard"):
        try:
            if gmmk.push(rgb, _CFG.get("mode"), force=force):
                written += 1
            if gmmk.available():
                any_ok = True
        except Exception as e:
            errors.append("keyboard: %s" % e)

    try:
        written += _push_openrgb(rgb, force)
        any_ok = True
    except Exception as e:
        errors.append(str(e))

    # A de-duplicated tick writes nothing and that is a success, not a failure:
    # keying the error on `written` would send the engine into backoff any time
    # the colour was steady and one backend happened to be offline.
    # Mystic can legitimately be the only healthy backend when the other
    # physical routes are paused or disconnected.
    if (_CFG.get("jrgb") or _CFG.get("mystic")) and _MYSTIC.get("ok"):
        any_ok = True

    if errors and not any_ok:
        raise OpenRGBError("; ".join(errors)[:300])
    if written:
        _STATUS["pushes"] += written
        _STATUS["last_push"] = time.time()
        _STATUS["last_color"] = rgb_to_hex(rgb)
    return written



def _engine_loop():
    backoff = 2.0
    while not _STOP.is_set():
        interval = max(0.03, float(_CFG.get("interval_ms", 120)) / 1000.0)
        try:
            if not _CFG.get("enabled"):
                _STOP.wait(0.5)
                continue
            rgb, _alerting = _compute_color(time.time())
            with _LOCK:
                _push_color(rgb)
            backoff = 2.0
            _STOP.wait(interval)
        except Exception as e:
            _STATUS["connected"] = False
            _STATUS["error"] = str(e)[:300]
            _disconnect()
            _STOP.wait(backoff)
            backoff = min(30.0, backoff * 1.7)
    _STATUS["engine"] = False


def start_engine():
    global _ENGINE
    with _LOCK:
        started = False
        _STOP.clear()
        if _ENGINE is None or not _ENGINE.is_alive():
            _ENGINE = threading.Thread(target=_engine_loop, name="rgb-engine", daemon=True)
            _ENGINE.start()
            _STATUS["engine"] = True
            started = True
        # MSI Center commits run separately so registry acknowledgement can
        # never stall the fast loop that drives OpenRGB devices.
        worker = _MYSTIC.get("thread")
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_mystic_worker, name="rgb-mystic", daemon=True)
            worker.start()
            _MYSTIC["thread"] = worker
            started = True
        return started


def stop_engine():
    _STOP.set()
    _STATUS["engine"] = False


def shutdown(blackout=True):
    """Service is going down. Leave the case dark rather than frozen on
    whatever colour happened to be live."""
    stop_engine()
    try:
        if blackout and _CFG.get("enabled"):
            with _LOCK:
                _push_color((0, 0, 0), force=True)
    except Exception:
        pass
    _disconnect()



def status(refresh=False):
    out = {
        "ok": True,
        "config": dict(_CFG),
        "modes": list(MODES),
        "activity": _busy(),
        "state": _rig_state(),
    }
    try:
        with _LOCK:
            if refresh:
                _disconnect()
                _RECONNECT["next"] = 0.0     # an explicit Rescan skips the backoff
                _RECONNECT["delay"] = 2.0
                _MYSTIC["profile"] = msi_center.inspect_profile()
                _MYSTIC["force"] = True
            _ensure_client()
            out["devices"] = [dict(d) for d in _DEVICES]
    except Exception as e:
        out["devices"] = []
        _STATUS["error"] = str(e)[:300]
        _STATUS["connected"] = False
    for dev in out.get("devices", []):
        dev.pop("mode_blobs", None)
        if _is_gmmk(dev):
            # OpenRGB enumerates the GMMK but cannot hold a colour on it, so
            # "driven" here must track the native driver, not the disable list.
            dev["driven"] = bool(_CFG.get("keyboard")) and gmmk.available()
            dev["backend"] = "native HID (app/gmmk.py)"
            dev["last_color"] = gmmk.status().get("last_color")
            continue
        dev["driven"] = dev["name"] not in set(_CFG.get("disabled_devices") or [])
        dev["last_color"] = rgb_to_hex(_unpack(_LAST_PUSH[dev["index"]])) if dev["index"] in _LAST_PUSH else None

    # Historical response key retained for the Lighting UI. The board route is
    # one atomic MSI transaction containing MSI's generic board records and
    # the physical 12 V JRGB1 strip header. Function/status LEDs stay untouched.
    profile = _MYSTIC.get("profile")
    if not profile:
        profile = msi_center.inspect_profile()
        _MYSTIC["profile"] = profile
    board_profile = profile.get("board") or {}
    jrgb_profile = profile.get("jrgb") or {}
    board_ready = bool(profile.get("available") and
                       board_profile.get("zones") == 8 and
                       jrgb_profile.get("headers") == 1)
    board_latched = bool(_CFG.get("jrgb") and board_ready and
                         board_profile.get("enabled") and
                         jrgb_profile.get("enabled") and
                         profile.get("msi_sync_selected") and
                         profile.get("gpu_allsync_detached") and
                         _MYSTIC.get("ok"))
    out["jrgb"] = {
        "backend": "MSI Center atomic profile",
        "available": board_ready,
        "installed": board_ready,
        "error": _MYSTIC.get("error") or profile.get("error"),
        "note": ("MSI's board profile records and physical JRGB1 12 V strip "
                 "header are enabled in the same acknowledged transaction as "
                 "the AIO. Function/status LEDs remain off; a visible JRGB1 "
                 "change requires a strip plugged into that header."),
        "driven": board_latched,
        "latched": board_latched,
        "zones": board_profile.get("zones", 0),
        "headers": jrgb_profile.get("headers", 0),
        "header_enabled": bool(jrgb_profile.get("enabled")),
        "uniform": bool(board_profile.get("uniform")),
        "scope": "msi_motherboard_records_1_8_and_jrgb1",
        "keepalive_s": None,
        "last_color": _group_last_color("last_board"),
    }

    # The GMMK, driven over raw HID rather than through OpenRGB.
    try:
        kb = gmmk.status()
    except Exception as e:
        kb = {"available": False, "error": str(e)[:200]}
    kb["driven"] = bool(_CFG.get("keyboard")) and kb.get("available")
    out["keyboard"] = kb

    # Compatibility response key for the AIO. Both this route and the board
    # route are fields in the same MSI Center SettingData transaction.
    try:
        aio_profile = profile.get("aio") or {}
        aio_ready = bool(profile.get("available") and
                         aio_profile.get("profiles", 0) >= 1)
        aio_latched = bool(_CFG.get("mystic") and aio_ready and
                           aio_profile.get("enabled") and
                           profile.get("msi_sync_selected") and
                           profile.get("gpu_allsync_detached") and
                           _MYSTIC.get("ok"))
        out["mystic"] = {
            "available": aio_ready,
            "latched": aio_latched,
            "enumerating": False,
            "backend": "MSI Center atomic profile",
            "enabled": bool(_CFG.get("jrgb") or _CFG.get("mystic")),
            "writes": _MYSTIC.get("writes", 0),
            "acknowledged": bool(profile.get("acknowledged")),
            "committed": bool(profile.get("committed")),
            "forced": bool(profile.get("forced")),
            "already_current": bool(profile.get("already_current")),
            "changed": bool(profile.get("changed")),
            "apply_pending": bool(profile.get("apply_pending")),
            "gpu_allsync_detached": bool(profile.get("gpu_allsync_detached")),
            "msi_sync_selected": bool(profile.get("msi_sync_selected")),
            "gpu_sync_error": profile.get("gpu_sync_error"),
            "sync_status_changed": bool(profile.get("sync_status_changed")),
            "aio_profiles": aio_profile.get("profiles", 0),
            "last_color": _group_last_color("last_aio"),
            "error": _MYSTIC.get("error") or profile.get("error"),
            "min_interval_s": MSI_PROFILE_MIN_INTERVAL,
            "safe_areas": sorted(MYSTIC_SAFE_AREAS),
            "onboard_backend": "MSI Center atomic profile (board records + JRGB1)",
            "areas": [
                {"type": "MSI_CENTER", "area": "board", "name": "Board records + JRGB1",
                 "key": "MSI_CENTER:BOARD", "zones": board_profile.get("zones", 0),
                 "headers": jrgb_profile.get("headers", 0),
                 "driven": bool(_CFG.get("jrgb")) and board_latched},
                {"type": "MSI_CENTER", "area": "aio", "name": "JCORSAIR 5V ARGB header",
                 "key": "MSI_CENTER:AIO", "profiles": aio_profile.get("profiles", 0),
                 "driven": bool(_CFG.get("mystic")) and aio_latched},
            ],
        }
    except Exception as e:
        out["mystic"] = {"available": False, "backend": "MSI Center atomic profile",
                         "enabled": bool(_CFG.get("jrgb") or _CFG.get("mystic")), "areas": [],
                         "error": str(e)[:200]}

    out["status"] = dict(_STATUS)
    out["hint"] = _hint(out)
    return out


def _unpack(packed):
    return (packed & 0xFF, (packed >> 8) & 0xFF, (packed >> 16) & 0xFF)


def _hint(payload):
    """One line telling the operator what to do when something is missing."""
    myst = payload.get("mystic") or {}
    if (_CFG.get("jrgb") or _CFG.get("mystic")) and not myst.get("available"):
        return ("MSI Center atomic lighting unavailable (%s) — the JRGB1 profile "
                "and JCORSAIR AIO need LEDKeeper2 and Mystic_Light_Service running."
                % (myst.get("error") or "not initialised"))
    if not _STATUS.get("connected"):
        return ("No OpenRGB server on %s — start the OpenRGB service, then Rescan. "
                "MSI profile lighting is unaffected." % _STATUS["server"])
    return None


def set_config(**kwargs):
    changed = {}
    with _LOCK:
        for key, value in kwargs.items():
            if value is None or key not in DEFAULTS:
                continue
            if key == "mode":
                value = str(value).lower()
                if value not in MODES:
                    return {"ok": False, "error": "unknown mode: %s" % value, "status_code": 400}
            elif key == "color":
                value = rgb_to_hex(hex_to_rgb(value))
            elif key == "mystic_color":
                # "" clears the override and hands the AIO back to the scene.
                text = str(value).strip()
                value = rgb_to_hex(hex_to_rgb(text)) if text else ""
            elif key in ("brightness", "speed"):
                value = max(0, min(100, int(value)))
            elif key == "interval_ms":
                value = max(30, min(5000, int(value)))
            elif key in ("enabled", "alert", "keyboard", "jrgb", "jrgb_invert",
                         "mystic", "gpu_label"):
                value = bool(value)
            elif key in ("alert_gpu_temp", "alert_cpu_temp"):
                value = max(40.0, min(110.0, float(value)))
            elif key == "disabled_devices":
                value = sorted({str(v) for v in (value or [])})
            elif key == "mystic_areas":
                requested = {str(v) for v in (value or [])}
                unsafe = sorted(requested - MYSTIC_SAFE_AREAS)
                if unsafe:
                    return {"ok": False, "status_code": 409,
                            "error": ("Unsafe Mystic targets rejected: %s. "
                                      "Supported targets are motherboard areas 0..7 and AIO area 9."
                                      % ", ".join(unsafe))}
                value = sorted(requested)
            elif key == "key_effect":
                value = str(value).lower()
                if value not in KEY_EFFECTS:
                    return {"ok": False, "status_code": 400,
                            "error": "unknown key_effect: %s (have %s)"
                                     % (value, ", ".join(KEY_EFFECTS))}
            _CFG[key] = value
            changed[key] = value
        # Physical-route toggles are authoritative. This also migrates an old
        # cached v66 UI that posted `mystic_areas: [MSI_MB:9]` when toggling the
        # AIO and accidentally removed the motherboard group.
        routes = set(_CFG.get("mystic_areas") or [])
        if _CFG.get("jrgb"):
            routes.update(MYSTIC_ONBOARD_AREAS)
        else:
            routes.difference_update(MYSTIC_ONBOARD_AREAS)
        if _CFG.get("mystic"):
            routes.add(MYSTIC_AIO_AREA)
        else:
            routes.discard(MYSTIC_AIO_AREA)
        normalised_routes = sorted(routes & MYSTIC_SAFE_AREAS)
        if normalised_routes != _CFG.get("mystic_areas"):
            _CFG["mystic_areas"] = normalised_routes
            changed["mystic_areas"] = normalised_routes
        if ("mystic_areas" in changed or "mystic" in changed or
                "jrgb" in changed or "mystic_color" in changed):
            _MYSTIC["last_signature"] = None
            _MYSTIC["force"] = bool(_CFG.get("jrgb") or _CFG.get("mystic"))
            if not (_CFG.get("jrgb") or _CFG.get("mystic")):
                # Releasing MSI ownership must not black the persisted profile.
                _MYSTIC["want"] = None
        _LAST_PUSH.clear()   # force a repaint with the new settings
        _save_config()
    # No matching blackout for the keyboard, deliberately: its colour is
    # committed to its own onboard profile, so releasing it leaves a lit,
    # usable keyboard rather than a stale live push. Blanking someone's keys
    # because they unticked a checkbox would be worse.
    if _CFG.get("enabled"):
        start_engine()
        if _CFG.get("mode") == "off":
            try:
                with _LOCK:
                    _push_color((0, 0, 0), force=True)
            except Exception:
                pass
    else:
        # The engine worker is about to stop, so commit its persistent MSI
        # routes dark here rather than leaving an unconsumed queue item.
        if _CFG.get("jrgb") or _CFG.get("mystic"):
            try:
                result = msi_center.apply_profile(
                    (0, 0, 0), (0, 0, 0),
                    board_enabled=False, aio_enabled=False, force=True)
                _MYSTIC["profile"] = result
                _MYSTIC["writes"] += 1 if result.get("committed") else 0
                _MYSTIC["ok"] = bool(result.get("acknowledged") and
                                      result.get("committed"))
                _MYSTIC["last"] = (0, 0, 0)
                _MYSTIC["last_board"] = (0, 0, 0)
                _MYSTIC["last_aio"] = (0, 0, 0)
                _MYSTIC["last_signature"] = ((0, 0, 0), (0, 0, 0))
            except Exception as exc:
                _MYSTIC["ok"] = False
                _MYSTIC["error"] = str(exc)[:200]
        stop_engine()
        try:
            with _LOCK:
                _push_color((0, 0, 0), force=True)
        except Exception:
            pass
        _disconnect()
    return {"ok": True, "changed": changed, "config": dict(_CFG)}


def set_device_enabled(name, enabled):
    if name in MYSTIC_ONBOARD_AREAS:
        return {"ok": False, "status_code": 409,
                "error": ("%s is part of the motherboard's eight-zone group. Use the "
                          "single motherboard lighting toggle instead."
                          % name)}
    if name in (MYSTIC_AIO_AREA, "MSI_CENTER:AIO"):
        return set_config(mystic=bool(enabled))
    if name == "MSI_CENTER:BOARD":
        return set_config(jrgb=bool(enabled))
    if name.startswith("MSI_MB:") or name.startswith("MSI_CENTER:"):
        return {"ok": False, "status_code": 409,
                "error": ("MSI profile zones are atomic and cannot be driven "
                          "individually; use the board or AIO toggle.")}

    with _LOCK:
        off = set(_CFG.get("disabled_devices") or [])
        if enabled:
            off.discard(name)
        else:
            off.add(name)
            # Dropping a device from the drive list must DARKEN it, not freeze
            # it on whatever colour happened to be live -- same rule the
            # motherboard header already follows. Written before the name lands
            # in the disable list, because _targets() would exclude it after.
            try:
                client = _ensure_client()
                for dev in _targets():
                    if dev["name"] != name:
                        continue
                    # Prefer the device's own "Off" mode. Writing black only
                    # works for LEDs the Direct path owns -- the 3090 FE's
                    # GEFORCE RTX label is a fixed white LED that ignores
                    # colour entirely and only goes dark via the hardware mode.
                    modes = [str(m).lower() for m in (dev.get("modes") or [])]
                    blobs = dev.get("mode_blobs") or []
                    if "off" in modes and modes.index("off") < len(blobs):
                        i = modes.index("off")
                        client.update_mode(dev["index"], i, blobs[i])
                    else:
                        client.set_custom_mode(dev["index"])
                        client.update_leds(dev["index"], [0] * dev["leds"])
                    _LAST_PUSH.pop(dev["index"], None)
                    _LAST_KEY.pop(dev["index"], None)
                    break
            except Exception as exc:
                # Not fatal -- but never silent. A swallowed failure here looked
                # exactly like "the Off mode does not work" when the mode switch
                # was in fact never reaching the device.
                _STATUS["blackout_error"] = "%s: %s" % (name, str(exc)[:160])
        _CFG["disabled_devices"] = sorted(off)
        _save_config()
        _LAST_PUSH.clear()
    if not enabled:
        try:
            with _LOCK:
                client = _ensure_client()
                for dev in _DEVICES:
                    if dev["name"] == name and dev["leds"] > 0:
                        client.set_custom_mode(dev["index"])
                        client.update_leds(dev["index"], [0] * dev["leds"])
        except Exception:
            pass
    return {"ok": True, "disabled_devices": _CFG["disabled_devices"]}


def apply_defaults_for_new_devices():
    """First run: switch peripherals off in the drive list so we only light the
    case hardware until the operator says otherwise."""
    with _LOCK:
        if _CFG.get("disabled_devices"):
            return False
        try:
            _ensure_client()
        except Exception:
            return False
        _CFG["disabled_devices"] = _default_disabled()
        _save_config()
        return True


def identify(name, flashes=3):
    """Blink one device white so the operator can tell which is which."""
    with _LOCK:
        client = _ensure_client()
        target = next((d for d in _DEVICES if d["name"] == name and d["leds"] > 0), None)
        if not target:
            return {"ok": False, "error": "unknown device: %s" % name, "status_code": 404}
        idx, leds = target["index"], target["leds"]
        for _ in range(max(1, min(6, int(flashes)))):
            client.set_custom_mode(idx)
            client.update_leds(idx, [pack_rgb(255, 255, 255)] * leds)
            time.sleep(0.18)
            client.update_leds(idx, [0] * leds)
            time.sleep(0.18)
        _LAST_PUSH.pop(idx, None)
    return {"ok": True, "device": name}


def pulse(color="#ffffff", ms=450):
    """One-shot flash across the driven devices (task done, alert, etc.)."""
    rgb = hex_to_rgb(color)
    try:
        with _LOCK:
            _push_color(rgb, force=True)
        time.sleep(max(0.05, min(3.0, float(ms) / 1000.0)))
        with _LOCK:
            _LAST_PUSH.clear()
        return {"ok": True, "color": rgb_to_hex(rgb)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "status_code": 503}


def test_pattern():
    """Cycle red -> green -> blue -> white on every driven lighting backend."""
    seq = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    try:
        for rgb in seq:
            with _LOCK:
                _push_color(rgb, force=True)
            time.sleep(0.45)
        with _LOCK:
            _LAST_PUSH.clear()
        return {"ok": True, "steps": len(seq)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "status_code": 503}
