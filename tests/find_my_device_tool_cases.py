"""No network: find_devices is a stand-in.

    python tests/find_my_device_tool_cases.py
"""
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import agent_tools as T  # noqa: E402
from app import location as L  # noqa: E402
from app import messaging as M  # noqa: E402
from app import rig_agent  # noqa: E402

LISTED = [{"name": "MacBook Pro", "type": 'MacBook Pro 15"', "online": True,
           "battery": 96, "place": None, "fix_age_min": None, "map_link": None}]


def _tool():
    tool = T.build_default_registry().get("find_my_device")
    assert tool is not None, "find_my_device is not registered"
    return tool


CALLS = []


def _call(owner, found=LISTED):
    ctx = T.AgentContext(sender="him", allow_private=True)

    def fake(*args, **kwargs):
        CALLS.append(kwargs)
        if isinstance(found, Exception):
            raise found
        return found

    with patch.object(M, "load_config", return_value={"auto_reply": {}}), \
            patch.object(rig_agent, "owner_allowed", return_value=owner), \
            patch.object(L, "find_devices", side_effect=fake):
        return _tool().handler({}, ctx)


def test_the_owner_gets_every_device():
    assert _call(True) == {"devices": LISTED}


def test_each_located_device_gets_its_city():
    # without the city geocoder find_devices leaves every "place" empty
    CALLS.clear()
    _call(True)
    assert CALLS and CALLS[-1].get("geocode") is L.nominatim_geocode, CALLS


def test_nobody_else_does():
    try:
        _call(False)
    except T.ToolRefused:
        return
    raise AssertionError("a non-owner got his devices")


def test_an_expired_login_is_said_out_loud():
    got = _call(True, found=L.SessionExpired("wants a new login"))
    assert got.get("devices") == [] and got.get("login_needed") is True, got
    assert got.get("note"), got


def test_it_is_private_read_only_and_only_in_his_own_chat():
    tool = _tool()
    assert tool.risk == T.READ_ONLY and tool.private is True
    assert "find_my_device" in M._LIVE_ASSISTANT_AGENT_TOOLS
    assert "find_my_device" not in M._LIVE_TRUSTED_CONTACT_ACTION_TOOLS
    assert "find_my_device" not in M._OWNER_ONLY_CONTACT_TOOLS


def test_description_explains_last_seen_semantics():
    tool = _tool()
    desc = tool.description
    assert "last heard" in desc, "description must explain battery/online are last-seen values"
    assert "place" in desc and "fix_age_min" in desc and "map_link" in desc, "description must name place, fix_age_min, map_link as current location"
    assert "last_seen_place" in desc and "last_seen_age_min" in desc and "last_seen_map_link" in desc, "description must name last_seen_* fallback fields"
    assert "never seen" in desc, "description must state the rig has never seen it when all fields are empty"
    assert "current location (city, battery, online status)" not in desc, "old bare 'current location' phrasing must be removed"


if __name__ == "__main__":
    test_the_owner_gets_every_device()
    test_each_located_device_gets_its_city()
    test_nobody_else_does()
    test_an_expired_login_is_said_out_loud()
    test_it_is_private_read_only_and_only_in_his_own_chat()
    test_description_explains_last_seen_semantics()
    print("OK")
