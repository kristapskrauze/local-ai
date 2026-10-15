"""Loaded automatically by Python when this folder is on PYTHONPATH (see
tests/run_offline.py): refuse every outbound network connection, so an
offline test cannot quietly call the model, the service, BlueBubbles or the
web. Child processes that are not Python (node + Chrome for the browser
tests) are not affected."""
import socket
import sys


def _refuse(self, address, *a, **k):
    msg = "offline test tried to connect to %r" % (address,)
    try:
        sys.stderr.write(msg + "\n")
    except Exception:
        pass
    raise OSError(msg)


socket.socket.connect = _refuse
socket.socket.connect_ex = _refuse
socket.create_connection = lambda address, *a, **k: _refuse(None, address)
