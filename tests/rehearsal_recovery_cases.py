"""A restart in the middle of a rehearsal does not leave its change behind.

Each case kills a real rehearsal mid-suite, the way a restart does, then runs
the startup recovery. Throwaway zz_ files under tests/, removed afterwards; the
rehearsal folder is a temp dir, never the live one.

    python tests/rehearsal_recovery_cases.py
"""
import os
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import self_heal  # noqa: E402

SLOW_SUITE = ('import time\n\n\ndef test_slow():\n    time.sleep(90)\n\n\n'
              'if __name__ == "__main__":\n    test_slow()\n    print("OK")\n')
CHILD = ("import sys\nsys.path.insert(0, %r)\nfrom app import self_heal\n"
         "self_heal.apply_edits(%r, suites=[%r], rehearse=True)\n")


def _real(rel):
    return os.path.join(ROOT, *rel.split("/"))


def _read(rel):
    with open(_real(rel), encoding="utf-8") as fh:
        return fh.read()


def _write(rel, text):
    with open(_real(rel), "w", encoding="utf-8") as fh:
        fh.write(text)


def _kill_mid_rehearsal(edits, suite, written):
    """Start a rehearsal, wait until `written()` holds, kill it like a restart."""
    rdir = tempfile.mkdtemp(prefix="zz_rehearsals_")
    env = dict(os.environ, SELF_HEAL_REHEARSAL_DIR=rdir)
    child = subprocess.Popen([sys.executable, "-X", "utf8", "-c",
                              CHILD % (ROOT, edits, suite)], cwd=ROOT, env=env)
    try:
        deadline = time.time() + 120
        while time.time() < deadline and not written():
            time.sleep(0.3)
        assert written(), "the rehearsal never wrote its change"
    finally:
        child.kill()
        child.wait()
    return rdir


def _names():
    tag = uuid.uuid4().hex[:8]
    return ("tests/zz_recover_%s_data.py" % tag, "tests/zz_recover_%s_cases.py" % tag)


def _cleanup(*rels):
    for rel in rels:
        try:
            os.remove(_real(rel))
        except OSError:
            pass


def test_a_created_file_is_removed():
    data, suite = _names()
    _write(suite, SLOW_SUITE)
    try:
        rdir = _kill_mid_rehearsal([{"path": data, "find": "", "replace": "VALUE = 2\n"}],
                                   suite, lambda: os.path.exists(_real(data)))
        assert os.path.exists(_real(data)), "the kill should have left it behind"
        got = self_heal.recover_interrupted_rehearsals(rdir)
        assert not os.path.exists(_real(data)), got
        assert got["restored"] == [data] and not got["left"], got
        assert not os.listdir(rdir), os.listdir(rdir)
    finally:
        _cleanup(data, suite)


def test_an_edited_file_is_put_back():
    data, suite = _names()
    _write(data, "VALUE = 1\n")
    _write(suite, SLOW_SUITE)
    try:
        rdir = _kill_mid_rehearsal(
            [{"path": data, "find": "VALUE = 1", "replace": "VALUE = 2"}], suite,
            lambda: "VALUE = 2" in _read(data))
        assert "VALUE = 2" in _read(data)
        got = self_heal.recover_interrupted_rehearsals(rdir)
        assert _read(data) == "VALUE = 1\n", (_read(data), got)
        assert got["restored"] == [data], got
    finally:
        _cleanup(data, suite)


def test_a_file_changed_since_is_left_as_found():
    data, suite = _names()
    _write(data, "VALUE = 1\n")
    _write(suite, SLOW_SUITE)
    try:
        rdir = _kill_mid_rehearsal(
            [{"path": data, "find": "VALUE = 1", "replace": "VALUE = 2"}], suite,
            lambda: "VALUE = 2" in _read(data))
        _write(data, "VALUE = 3\n")          # someone edited it after the crash
        got = self_heal.recover_interrupted_rehearsals(rdir)
        assert _read(data) == "VALUE = 3\n", _read(data)
        assert got["left"] == [data] and not got["restored"], got
    finally:
        _cleanup(data, suite)


if __name__ == "__main__":
    test_a_created_file_is_removed()
    test_an_edited_file_is_put_back()
    test_a_file_changed_since_is_left_as_found()
    print("OK")
