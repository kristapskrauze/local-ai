"""One-time iCloud login so the rig can read your iPhone's Find My location.

Run it YOURSELF (it asks for your Apple ID password; nobody else should type it).
In PowerShell (the app's Terminal panel, or over SSH):

    python _tools/icloud_login.py

At the end it lists your devices and whether Find My has a location for
each. It never prints coordinates.
"""
import getpass
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = ROOT / ".secrets" / "icloud"
ACCOUNT = SESSION_DIR / "account.json"
LAST_RUN = SESSION_DIR / "last_run.txt"


# Did Apple check the password in this run? A still-valid saved session is
# reused without it, so a mistyped password could otherwise be stored unseen.
# Only the status of Apple's sign-in answer is looked at (200, or 409 = right
# password and a 2FA code wanted) -- never the request body.
_APPLE_CHECKED = {"ok": False}


def _watch_password_checks():
    import requests
    original = requests.Session.request

    def request(self, method, url, *args, **kwargs):
        resp = original(self, method, url, *args, **kwargs)
        try:
            if str(url).rstrip("/").endswith("/signin/complete") and \
                    resp.status_code in (200, 409):
                _APPLE_CHECKED["ok"] = True
        except Exception:
            pass
        return resp
    requests.Session.request = request


def _record(step, outcome):
    """What happened, for reading afterwards. Never the password."""
    try:
        LAST_RUN.write_text("%s  step=%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   step, outcome), encoding="utf-8")
    except Exception:
        pass


def _apple_said(exc):
    """Apple's own answer under pyicloud's wording: the HTTP status and
    Apple's error code and message, from the exception chain."""
    parts = []
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        resp = getattr(e, "response", None)
        if resp is not None:
            said = ""
            try:
                body = resp.json()
                errs = (body.get("serviceErrors") or []) if isinstance(body, dict) else []
                said = "; ".join("%s %s" % (x.get("code", ""), x.get("message", ""))
                                 for x in errs if isinstance(x, dict))
            except Exception:
                said = (resp.text or "")[:200]
            parts.append("HTTP %s %s" % (resp.status_code, said.strip()))
        elif getattr(e, "code", None):
            parts.append("code %s %s" % (e.code, getattr(e, "reason", "") or ""))
        e = e.__cause__ or e.__context__
    return " | ".join(p.strip() for p in parts if p.strip())


def _fail(step, exc, hint):
    text = "%s: %s" % (type(exc).__name__, str(exc)[:400]) if exc else ""
    apple = _apple_said(exc) if exc else ""
    print("\nFAILED at: %s" % step)
    if text:
        print("pyicloud said: %s" % text)
    if apple:
        print("Apple answered: %s" % apple)
    print(hint)
    _record(step, "FAILED %s | apple: %s | %s" % (text, apple or "-", hint))
    sys.exit(1)


def _two_factor(api):
    print("Apple now wants a 2FA code: a 'Sign in requested' prompt should appear "
          "on your iPhone -- tap Allow and read the 6 digits.")
    try:
        api.request_2fa_code()
    except Exception:
        pass  # Apple may already have pushed a prompt to your devices
    code = input("2FA code: ").strip().replace(" ", "")
    try:
        ok = api.validate_2fa_code(code)
    except Exception as exc:
        _fail("2FA code", exc, "Checking the code failed. Run this again for a fresh code.")
    if not ok:
        _fail("2FA code", None, "Apple rejected that code (wrong or expired). Run this "
                                "again and use the newest code.")
    _record("2FA code", "ok")
    try:
        if not api.is_trusted_session:
            api.trust_session()
    except Exception as exc:
        _fail("trust session", exc, "Signed in, but Apple would not trust this session. "
                                    "Run this again.")


_REFUSED = ("Apple refused the password when Find My asked for it. Look at 'Apple "
            "answered' above: a wrong password (it is not shown while you type) or a "
            "locked / throttled account. To check the password itself, open "
            "https://www.icloud.com/find in a browser -- it asks for it the same way.")


def main():
    try:
        from pyicloud import PyiCloudService
    except Exception as exc:
        _fail("import", exc, "pyicloud is not installed in this Python.")
    _watch_password_checks()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    conf = {}
    if ACCOUNT.exists():
        try:
            conf = json.loads(ACCOUNT.read_text(encoding="utf-8"))
        except Exception:
            conf = {}
    hint = conf.get("apple_id") or ""
    apple_id = input("Apple ID%s: " % (" [%s]" % hint if hint else "")).strip() or hint
    if not apple_id:
        _fail("apple id", None, "No Apple ID given.")
    client_id = conf.get("client_id") or str(uuid.uuid1()).lower()
    password = getpass.getpass("Apple ID password (not shown, not saved): ")
    if not password:
        _fail("password", None, "No password typed. (In some terminals the "
                                "password does not show while you type; that is normal.)")
    print("Signing in to Apple...")
    try:
        api = PyiCloudService(apple_id, password=password,
                              cookie_directory=str(SESSION_DIR), client_id=client_id)
    except Exception as exc:
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "terms" in text:
            advice = ("Apple wants new iCloud terms accepted. Sign in once at "
                      "https://www.icloud.com in a browser, accept them, then run this again.")
        elif "failedlogin" in name or "invalid" in text or "401" in text:
            advice = ("Apple rejected the Apple ID or password. Check both and run this "
                      "again. If the password is right, sign in at https://www.icloud.com "
                      "once to see whether Apple asks for anything.")
        elif "503" in text or "unavailable" in text:
            advice = "Apple's sign-in service refused for now. Wait a few minutes and try again."
        else:
            advice = "Unexpected sign-in error; the text above says what Apple returned."
        _fail("sign-in (password)", exc, advice)
    # kept until the end: you may choose to store it for the rig (cleared there)
    # A still-valid saved session is reused WITHOUT checking the password, so
    # this is not yet proof the password is right; Find My below is.
    _record("sign-in", "ok")
    if api.requires_2fa:
        _two_factor(api)
    elif getattr(api, "requires_2sa", False):
        _fail("2FA", None, "This account uses the old two-step verification, which this "
                           "tool does not handle.")
    ACCOUNT.write_text(json.dumps({"apple_id": apple_id, "client_id": client_id},
                                  indent=1), encoding="utf-8")
    print("\nSession saved in %s (the password only if you say so at the end)."
          % SESSION_DIR)
    print("Trusted session: %s" % bool(api.is_trusted_session))
    print("\nOpening Find My (Apple checks the password again here)...")
    devices = None
    try:
        devices = api.devices
    except Exception as exc:
        if not api.requires_2fa:
            _fail("Find My", exc, _REFUSED)
        _two_factor(api)
        try:
            devices = api.devices
        except Exception as exc2:
            _fail("Find My", exc2, _REFUSED)
    try:
        rows = [(dev.status(), bool(dev.location)) for dev in devices]
    finally:
        stop = getattr(devices, "stop_event", None)
        if stop is not None:
            stop.set()  # pyicloud's refresher thread; not needed past this list
    print("Find My devices:")
    for status, has_loc in rows:
        print("  - %s (%s): %s" % (status.get("name"), status.get("deviceDisplayName"),
                                   "location available" if has_loc else "no location"))
    sys.path.insert(0, str(ROOT))
    from app import icloud_secret
    answer = input("\nKeep the password on the rig so Find My keeps working by itself?\n"
                   "It is stored encrypted for this PC only, and the first time Apple "
                   "refuses it the rig stops using it. [y/N]: ").strip().lower()
    if answer in ("y", "yes", "j", "ja", "jā"):
        try:
            icloud_secret.save(password, SESSION_DIR)
        except Exception as exc:
            password = None
            _fail("store password", exc, "Signed in and Find My works, but the password "
                                         "could not be stored; the location will go stale "
                                         "in a few hours.")
        password = None
        note = ("" if _APPLE_CHECKED["ok"] else
                " Apple did not need the password in this run, so it is unchecked: "
                "the rig finds out the first time Find My asks, and stops if it is wrong.")
        print("\nDone. Password stored (encrypted); Find My keeps working.%s" % note)
        _record("done", "Find My ok, %d devices, trusted=%s, password stored%s"
                % (len(rows), bool(api.is_trusted_session),
                   "" if _APPLE_CHECKED["ok"] else " (unchecked)"))
    else:
        password = None
        icloud_secret.forget(SESSION_DIR)
        print("\nDone. Password not stored; Apple will ask for it again in a few hours.")
        _record("done", "Find My ok, %d devices, trusted=%s, password not stored"
                % (len(rows), bool(api.is_trusted_session)))


if __name__ == "__main__":
    main()
