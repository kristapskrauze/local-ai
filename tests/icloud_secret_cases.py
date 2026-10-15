# -*- coding: utf-8 -*-
"""The stored Apple ID password: encrypted, and retired on the first refusal.

Spec: app/icloud_secret.py save/load/forget/mark_refused/status, and
app/location.py _default_factory / _open_find_my retiring it.

No network, no real secret: a dummy value in a temp dir; pyicloud is a
stand-in.

    python tests/icloud_secret_cases.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import icloud_secret as S  # noqa: E402
from app import location as L  # noqa: E402

DUMMY = "not-a-real-password-āš"


def test_round_trip_and_never_in_the_clear():
    d = tempfile.mkdtemp()
    S.save(DUMMY, d)
    raw = open(os.path.join(d, S.PASSWORD_NAME), "rb").read()
    assert DUMMY.encode("utf-8") not in raw and b"not-a-real" not in raw
    assert S.load(d) == DUMMY
    assert S.status(d) == {"stored": True, "refused": False, "refused_note": ""}


def test_a_refusal_retires_it_until_the_next_save():
    d = tempfile.mkdtemp()
    S.save(DUMMY, d)
    S.mark_refused(d, "Invalid email/password combination.")
    assert S.load(d) is None and S.status(d)["refused"]
    S.save(DUMMY, d)
    assert S.load(d) == DUMMY and not S.status(d)["refused"]
    S.forget(d)
    assert S.load(d) is None and not S.status(d)["stored"]


def _fake_pyicloud(behaviour, made):
    import pyicloud

    class Fake:
        def __init__(self, apple_id, password="unset", **kw):
            made.append(password)
            self.requires_2fa = behaviour == "2fa"

        def authenticate(self):
            if behaviour == "refused":
                from pyicloud.exceptions import PyiCloudFailedLoginException
                raise PyiCloudFailedLoginException("Invalid email/password combination.")

        @property
        def devices(self):
            if behaviour == "findmy-refused":
                from pyicloud.exceptions import PyiCloudFailedLoginException
                raise PyiCloudFailedLoginException("Invalid email/password combination.")
            return []
    real = pyicloud.PyiCloudService
    pyicloud.PyiCloudService = Fake
    return real


def _run(behaviour):
    import pyicloud
    d = tempfile.mkdtemp()
    S.save(DUMMY, d)
    made = []
    real = _fake_pyicloud(behaviour, made)
    try:
        try:
            svc = L._default_factory("him@example.com", "fixed-client", d)
            L._open_find_my(svc)
        except Exception as exc:
            err = exc
        else:
            err = None
    finally:
        pyicloud.PyiCloudService = real
    return d, made, err


def test_the_rig_uses_it_when_stored():
    d, made, err = _run("ok")
    assert made == [DUMMY] and err is None, (made, err)
    assert not S.status(d)["refused"]


def test_a_refused_sign_in_retires_it_at_once():
    d, made, err = _run("refused")
    assert err is not None and S.status(d)["refused"], err
    assert S.load(d) is None, "the next poll would sign in with it again"


def test_find_my_refusing_it_retires_it_and_says_why():
    d, made, err = _run("findmy-refused")
    assert isinstance(err, L.SessionExpired) and "stored password" in str(err), err
    assert S.status(d)["refused"]


def test_a_2fa_demand_retires_it_so_his_phone_is_not_pinged_every_poll():
    d, made, err = _run("2fa")
    assert S.status(d)["refused"] and "2FA" in S.status(d)["refused_note"]


if __name__ == "__main__":
    test_round_trip_and_never_in_the_clear()
    test_a_refusal_retires_it_until_the_next_save()
    test_the_rig_uses_it_when_stored()
    test_a_refused_sign_in_retires_it_at_once()
    test_find_my_refusing_it_retires_it_and_says_why()
    test_a_2fa_demand_retires_it_so_his_phone_is_not_pinged_every_poll()
    print("OK")
