import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / ".secrets" / "icloud"
PASSWORD_NAME = "password.dpapi"
REFUSED_NAME = "password_refused.txt"
_ENTROPY = b"rig-icloud-findmy"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(data, protect):
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    inb, _keep_in = _blob(data)
    ent, _keep_ent = _blob(_ENTROPY)
    out = _Blob()
    flags = _CRYPTPROTECT_UI_FORBIDDEN | (_CRYPTPROTECT_LOCAL_MACHINE if protect else 0)
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inb), "rig icloud", ctypes.byref(ent),
                                      None, None, flags, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inb), None, ctypes.byref(ent),
                                        None, None, flags, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI failed (%d)" % ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dir(session_dir=None):
    return Path(session_dir) if session_dir else DEFAULT_DIR


def _restrict(path):
    """SYSTEM, Administrators and whoever saved it -- nobody else."""
    try:
        user = os.environ.get("USERNAME") or ""
        cmd = ["icacls", str(path), "/inheritance:r",
               "/grant:r", "*S-1-5-18:F", "*S-1-5-32-544:F"]
        if user:
            cmd.append("%s:F" % user)
        subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    except Exception:
        pass


def save(password, session_dir=None):
    """Encrypt and store; clears an earlier refusal. Returns the file path."""
    if not password:
        raise ValueError("empty password")
    d = _dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / PASSWORD_NAME
    tmp = d / (PASSWORD_NAME + ".tmp")
    tmp.write_bytes(_crypt(str(password).encode("utf-8"), True))
    os.replace(tmp, path)
    _restrict(path)
    try:
        (d / REFUSED_NAME).unlink()
    except FileNotFoundError:
        pass
    return path


def forget(session_dir=None):
    """Remove the stored password (and any refusal note)."""
    d = _dir(session_dir)
    for name in (PASSWORD_NAME, REFUSED_NAME):
        try:
            (d / name).unlink()
        except FileNotFoundError:
            pass


def load(session_dir=None):
    """The stored password, or None: nothing stored, refused once, or it
    cannot be decrypted on this PC."""
    d = _dir(session_dir)
    if (d / REFUSED_NAME).exists():
        return None
    path = d / PASSWORD_NAME
    if not path.exists():
        return None
    try:
        return _crypt(path.read_bytes(), False).decode("utf-8") or None
    except Exception:
        return None


def mark_refused(session_dir=None, reason=""):
    """Apple refused it (or wants 2FA): stop using it until the next login."""
    d = _dir(session_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / REFUSED_NAME).write_text("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    str(reason)[:300]), encoding="utf-8")
    except Exception:
        pass


def status(session_dir=None):
    d = _dir(session_dir)
    refused = d / REFUSED_NAME
    return {"stored": (d / PASSWORD_NAME).exists(),
            "refused": refused.exists(),
            "refused_note": (refused.read_text(encoding="utf-8").strip()
                             if refused.exists() else "")}
