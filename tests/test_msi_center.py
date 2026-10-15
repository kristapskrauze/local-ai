import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import msi_center  # noqa: E402


def sample_profile():
    records = []
    for index in range(1, 9):
        records.append(
            "M01,Z370I GAMING PRO CARBON AC,MB Mystic Light%d,T,3,1,2,3,T,0,0"
            % index)
    records.extend([
        "M01,Z370I GAMING PRO CARBON AC,JRGB1,T,6,9,8,7,T,0,0",
        "M01,Z370I GAMING PRO CARBON AC,JCORSAIR-LED STRIP,T,1,4,5,6,F,0,0",
        "M01,Z370I GAMING PRO CARBON AC,JCORSAIR-FAN HD120,T,2,4,5,6,F,0,0",
        "M01,Z370I GAMING PRO CARBON AC,MB Function LED,F,1,7,7,7,F,0,0",
    ])
    return "|".join(records)


def live_profile():
    """Fifteen-record shape currently persisted by MSI Center on this rig."""
    records = sample_profile().split("|")
    records.insert(
        -1, "M01,Z370I GAMING PRO CARBON AC,JCORSAIR-FAN SP120,T,1,4,5,6,F,0,0")
    records.insert(
        -1, "M01,Z370I GAMING PRO CARBON AC,JCORSAIR-FAN LL120,T,1,4,5,6,F,0,0")
    records.append(
        "V01,0,10DE2204147D10DE,NVIDIA GeForce RTX 3090,F,0,200,248,0,F,0,5")
    return "|".join(records)


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeWinreg:
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1

    def __init__(self, values):
        self.values = dict(values)
        self.writes = []
        self.key = FakeKey()
        self.reattach_sync_status = None

    def QueryValueEx(self, key, name):
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        previous = self.values.get(name)
        self.writes.append((name, value))
        self.values[name] = value
        # LEDKeeper2 is driven by RegistryValueChangeEvent on SettingData.
        # Windows emits no event for an identical value, so neither does this
        # fake; forced reconciliation must create a harmless edge first.
        if (name == msi_center.SETTING_VALUE and
                previous != value and
                str(self.values.get(msi_center.APPLY_VALUE)).lower() == "true"):
            self.values[msi_center.APPLY_VALUE] = "False"
            if self.reattach_sync_status is not None:
                self.values[msi_center.SYNC_STATUS_VALUE] = self.reattach_sync_status

    def FlushKey(self, key):
        return None


def fake_registry(profile, sync_status):
    return FakeWinreg({
        msi_center.SETTING_VALUE: profile,
        msi_center.SYNC_STATUS_VALUE: sync_status,
        msi_center.APPLY_VALUE: "False",
        msi_center.WAIT_VALUE: "False",
        msi_center.ALLSYNC_STYLE_VALUE: msi_center.ALLSYNC_STATIC_STYLE,
        msi_center.ALLSYNC_RGB_VALUES[0]: "248",
        msi_center.ALLSYNC_RGB_VALUES[1]: "0",
        msi_center.ALLSYNC_RGB_VALUES[2]: "0",
    })


class MsiCenterProfileTests(unittest.TestCase):
    def test_atomic_update_changes_only_board_and_jcorsair(self):
        source = sample_profile()
        updated, detail = msi_center.update_profile(
            source, (248, 0, 0), (0, 120, 248))

        self.assertTrue(detail["changed"])
        self.assertEqual(detail["board_zones"], 8)
        self.assertEqual(detail["aio_profiles"], 2)
        self.assertIn("JRGB1,T,8,248,0,0,F,0,100", updated)
        self.assertIn("MB Function LED,F,1,7,7,7,F,0,0", updated)

        status = msi_center.inspect_value(updated)
        self.assertEqual(status["board"]["zones"], 8)
        self.assertEqual(status["board"]["color"], (248, 0, 0))
        # An enabled board gets BOARD_LIT_STYLE: style index 8 on this MSI
        # board table reads back as NoAnimation/static. Style 1 is NOT the
        # answer -- MLAPI reads it back as Rainbow here.
        self.assertEqual(status["board"]["styles"], [msi_center.BOARD_LIT_STYLE])
        self.assertTrue(status["board"]["enabled"])
        self.assertEqual(status["jrgb"]["headers"], 1)
        self.assertEqual(status["jrgb"]["color"], (248, 0, 0))
        self.assertEqual(status["jrgb"]["styles"], [msi_center.BOARD_LIT_STYLE])
        self.assertTrue(status["jrgb"]["enabled"])
        self.assertEqual(status["aio"]["profiles"], 2)
        self.assertEqual(status["aio"]["color"], (0, 120, 248))
        self.assertTrue(status["aio"]["enabled"])

    def test_disabled_group_is_black_in_same_snapshot(self):
        updated, _ = msi_center.update_profile(
            sample_profile(), (248, 0, 0), (248, 0, 0),
            board_enabled=False, aio_enabled=True)
        status = msi_center.inspect_value(updated)
        self.assertEqual(status["board"]["color"], (0, 0, 0))
        self.assertFalse(status["board"]["enabled"])
        self.assertEqual(status["jrgb"]["color"], (0, 0, 0))
        self.assertFalse(status["jrgb"]["enabled"])
        self.assertEqual(status["aio"]["color"], (248, 0, 0))
        self.assertTrue(status["aio"]["enabled"])
        # A disabled board stays on plain Steady, so "off" is a genuine black
        # static rather than a black Breathing cycle.
        self.assertEqual(status["board"]["styles"], [msi_center.STATIC_STYLE])

    def test_identical_snapshot_is_a_noop(self):
        first, _ = msi_center.update_profile(
            sample_profile(), (248, 0, 0), (248, 0, 0))
        second, detail = msi_center.update_profile(
            first, (248, 0, 0), (248, 0, 0))
        self.assertEqual(second, first)
        self.assertFalse(detail["changed"])

    def test_force_pulse_changes_only_disabled_function_led(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (0, 120, 248))

        pulsed = msi_center.pulse_profile(profile)

        original = profile.split("|")
        changed = pulsed.split("|")
        changed_indexes = [
            index for index, before in enumerate(original)
            if changed[index] != before
        ]
        self.assertEqual(changed_indexes, [13])
        self.assertIn("MB Function LED,F,1,", changed[13])
        self.assertEqual(changed[13].split(",")[3], "F")
        for index in list(range(13)) + [14]:
            self.assertEqual(changed[index], original[index])

    def test_incomplete_board_profile_is_rejected(self):
        broken = sample_profile().replace(
            "M01,Z370I GAMING PRO CARBON AC,MB Mystic Light8,T,3,1,2,3,T,0,0|", "")
        with self.assertRaisesRegex(msi_center.MsiCenterError, "expected 8"):
            msi_center.update_profile(broken, (1, 2, 3), (4, 5, 6))

    def test_vga_is_detached_from_allsync_without_changing_profile(self):
        profile = live_profile()
        attached = "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14|M0110,V01114"
        expected = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"

        detached, detail = msi_center.detach_vga_from_sync_status(
            attached, profile)
        self.assertEqual(detached, expected)
        self.assertEqual(detail["vga_indexes"], [14])
        self.assertEqual(detail["vga_groups"], 1)

        second, _ = msi_center.detach_vga_from_sync_status(detached, profile)
        self.assertEqual(second, expected)

    def test_sync_normalization_repairs_empty_msi_selection(self):
        profile = live_profile()
        broken = "|M0110,V01014"
        expected = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"

        normalized, detail = msi_center.normalize_sync_status(broken, profile)

        self.assertEqual(normalized, expected)
        self.assertEqual(detail["msi_indexes"], list(range(14)))
        self.assertFalse(detail["msi_sync_selected"])

    def test_identical_registry_state_is_not_reported_as_acknowledged(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (248, 0, 0))
        sync_status = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"
        registry = fake_registry(profile, sync_status)

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key):
            result = msi_center.apply_profile(
                (248, 0, 0), (248, 0, 0), force=False)

        self.assertFalse(result["changed"])
        self.assertFalse(result["committed"])
        self.assertFalse(result["acknowledged"])
        self.assertTrue(result["already_current"])
        self.assertEqual(registry.writes, [])

    def test_force_commits_identical_snapshot_and_waits_for_ack(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (248, 0, 0))
        sync_status = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"
        registry = fake_registry(profile, sync_status)

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key), \
                mock.patch.object(msi_center.time, "sleep", return_value=None):
            result = msi_center.apply_profile(
                (248, 0, 0), (248, 0, 0), force=True)

        self.assertFalse(result["changed"])
        self.assertTrue(result["forced"])
        self.assertTrue(result["committed"])
        self.assertTrue(result["acknowledged"])
        self.assertTrue(result["edge_pulsed"])
        self.assertFalse(result["already_current"])
        self.assertIn((msi_center.APPLY_VALUE, "True"), registry.writes)
        self.assertIn((msi_center.SETTING_VALUE, profile), registry.writes)
        setting_writes = [value for name, value in registry.writes
                          if name == msi_center.SETTING_VALUE]
        self.assertEqual(len(setting_writes), 2)
        self.assertNotEqual(setting_writes[0], profile)
        self.assertEqual(setting_writes[1], profile)

    def test_stale_apply_flag_is_cleared_without_partial_profile_write(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (248, 0, 0))
        sync_status = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"
        registry = fake_registry(profile, sync_status)
        registry.values[msi_center.APPLY_VALUE] = "True"

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key), \
                mock.patch.object(msi_center.time, "monotonic",
                                  side_effect=[0.0, 0.6]):
            with self.assertRaisesRegex(
                    msi_center.MsiCenterError, "stale MSI Center apply flag"):
                msi_center.apply_profile(
                    (248, 0, 0), (248, 0, 0), timeout=0.1, force=True)

        self.assertEqual(registry.writes, [(msi_center.APPLY_VALUE, "False")])
        self.assertEqual(registry.values[msi_center.SETTING_VALUE], profile)

    def test_sync_repair_commits_and_preserves_gpu_record(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (248, 0, 0))
        attached = "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14|M0110,V01114"
        expected = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"
        gpu_record = profile.split("|")[14]
        registry = fake_registry(profile, attached)

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key), \
                mock.patch.object(msi_center.time, "sleep", return_value=None):
            result = msi_center.apply_profile(
                (248, 0, 0), (248, 0, 0), force=False)

        self.assertTrue(result["changed"])
        self.assertTrue(result["sync_status_changed"])
        self.assertTrue(result["gpu_allsync_detached"])
        self.assertTrue(result["committed"])
        self.assertTrue(result["edge_pulsed"])
        self.assertEqual(registry.values[msi_center.SYNC_STATUS_VALUE], expected)
        self.assertEqual(
            registry.values[msi_center.SETTING_VALUE].split("|")[14], gpu_record)

    def test_apply_reenables_aio_and_repairs_empty_selection(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (0, 0, 0), (0, 0, 0),
            board_enabled=False, aio_enabled=False)
        registry = fake_registry(profile, "|M0110,V01014")

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key), \
                mock.patch.object(msi_center.time, "sleep", return_value=None):
            result = msi_center.apply_profile(
                (0, 0, 0), (0, 80, 248),
                board_enabled=False, aio_enabled=True)

        self.assertTrue(result["committed"])
        self.assertTrue(result["msi_sync_selected"])
        self.assertFalse(result["board"]["enabled"])
        self.assertTrue(result["aio"]["enabled"])
        self.assertEqual(
            registry.values[msi_center.SYNC_STATUS_VALUE],
            "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014")

    def test_post_ack_vga_reattach_is_rejected(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (248, 0, 0))
        detached = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"
        attached = "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14|M0110,V01114"
        registry = fake_registry(profile, detached)
        registry.reattach_sync_status = attached

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key), \
                mock.patch.object(msi_center.time, "sleep", return_value=None):
            with self.assertRaisesRegex(
                    msi_center.MsiCenterError, "reattached the VGA"):
                msi_center.apply_profile(
                    (248, 0, 0), (248, 0, 0), force=True)

    def test_profile_current_rejects_style_colour_and_allsync_drift(self):
        profile, _ = msi_center.update_profile(
            live_profile(), (248, 0, 0), (0, 120, 248))
        sync_status = "0,1,2,3,4,5,6,7,8,9,10,11,12,13|M0110,V01014"
        registry = fake_registry(profile, sync_status)

        with mock.patch.object(msi_center, "winreg", registry), \
                mock.patch.object(msi_center, "_open", return_value=registry.key):
            current = msi_center.inspect_profile()

        self.assertTrue(msi_center.profile_is_current(
            current, (248, 0, 0), (0, 120, 248)))

        rainbow = dict(current)
        rainbow["board"] = dict(current["board"])
        rainbow["board"]["styles"] = ["1"]
        self.assertIn("board style mismatch", msi_center.profile_current_errors(
            rainbow, (248, 0, 0), (0, 120, 248)))

        wrong_colour = dict(current)
        wrong_colour["aio"] = dict(current["aio"])
        wrong_colour["aio"]["color"] = (0, 0, 248)
        self.assertIn("AIO colour mismatch", msi_center.profile_current_errors(
            wrong_colour, (248, 0, 0), (0, 120, 248)))

        allsync = dict(current)
        allsync["allsync_style"] = "1"
        self.assertIn("All Sync style mismatch", msi_center.profile_current_errors(
            allsync, (248, 0, 0), (0, 120, 248)))


if __name__ == "__main__":
    unittest.main()
