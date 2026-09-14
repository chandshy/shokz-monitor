import os
import tempfile
import unittest
from unittest.mock import patch

from shokz_monitor.bluetooth import AudioDeviceManager, DeviceState
from shokz_monitor.devices import AudioDevice, flag_path


class FakeMonitor:
    instances = {}

    def __init__(self, mac, on_state_change, auto_reconnect=True):
        self.mac = mac
        self.on_state_change = on_state_change
        self.auto_reconnect = auto_reconnect
        self.connects = 0
        self.disconnects = 0
        self.instances[mac] = self

    def set_auto_reconnect(self, enabled):
        self.auto_reconnect = enabled

    def connect(self):
        self.connects += 1

    def disconnect(self):
        self.disconnects += 1


class AudioDeviceManagerTest(unittest.TestCase):
    @patch.object(AudioDeviceManager, "_start_discovery")
    @patch("shokz_monitor.bluetooth.BlueZMonitor", FakeMonitor)
    def test_first_available_auto_device_owns_connection(self, _discovery):
        FakeMonitor.instances = {}
        devices = [
            AudioDevice("AA:00", "Preferred", True),
            AudioDevice("BB:00", "Other", True),
        ]
        AudioDeviceManager(object(), devices, lambda _state, _name: None)

        self.assertEqual(FakeMonitor.instances["AA:00"].connects, 0)
        FakeMonitor.instances["AA:00"].on_state_change(DeviceState(available=True))

        self.assertTrue(FakeMonitor.instances["AA:00"].auto_reconnect)
        self.assertFalse(FakeMonitor.instances["BB:00"].auto_reconnect)
        self.assertEqual(FakeMonitor.instances["AA:00"].connects, 1)

        FakeMonitor.instances["BB:00"].on_state_change(DeviceState(connected=True))

        self.assertEqual(FakeMonitor.instances["BB:00"].disconnects, 1)
        self.assertEqual(FakeMonitor.instances["AA:00"].connects, 2)

    @patch.object(AudioDeviceManager, "_start_discovery")
    @patch("shokz_monitor.bluetooth.BlueZMonitor", FakeMonitor)
    def test_connect_device_selects_available_row(self, _discovery):
        FakeMonitor.instances = {}
        devices = [
            AudioDevice("AA:00", "Preferred", True),
            AudioDevice("BB:00", "Other"),
        ]
        manager = AudioDeviceManager(object(), devices, lambda _state, _name: None)
        FakeMonitor.instances["AA:00"].on_state_change(
            DeviceState(connected=True, available=True)
        )
        FakeMonitor.instances["BB:00"].on_state_change(DeviceState(available=True))

        manager.connect_device("BB:00")

        self.assertEqual(FakeMonitor.instances["AA:00"].disconnects, 1)
        self.assertEqual(FakeMonitor.instances["BB:00"].connects, 1)

    @patch.object(AudioDeviceManager, "_stop_discovery")
    @patch.object(AudioDeviceManager, "_start_discovery")
    @patch("shokz_monitor.bluetooth.BlueZMonitor", FakeMonitor)
    def test_scan_pause_persists(self, start, stop):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            devices = [AudioDevice("AA:00", "Preferred", True)]
            manager = AudioDeviceManager(object(), devices, lambda _s, _n: None)
            self.assertEqual(start.call_count, 1)

            manager.set_scan_paused(True)
            stop.assert_called_once()

            restarted = AudioDeviceManager(object(), devices, lambda _s, _n: None)
            self.assertTrue(restarted.scan_paused)
            self.assertEqual(start.call_count, 1)

            restarted.set_scan_paused(False)
            self.assertEqual(start.call_count, 2)

    @patch.object(AudioDeviceManager, "_start_discovery")
    @patch("shokz_monitor.bluetooth.BlueZMonitor", FakeMonitor)
    def test_auto_connect_pause(self, _discovery):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": tmp}
        ):
            FakeMonitor.instances = {}
            devices = [
                AudioDevice("AA:00", "Preferred", True),
                AudioDevice("BB:00", "Other"),
            ]
            manager = AudioDeviceManager(object(), devices, lambda _s, _n: None)
            a, b = FakeMonitor.instances["AA:00"], FakeMonitor.instances["BB:00"]
            manager.set_auto_paused(True)

            a.on_state_change(DeviceState(available=True))
            b.on_state_change(DeviceState(connected=True, available=True))
            self.assertFalse(a.auto_reconnect)
            self.assertEqual((a.connects, b.disconnects), (0, 0))

            manager.connect_device("AA:00")  # manual still works
            self.assertEqual((a.connects, b.disconnects), (1, 1))
            self.assertFalse(a.auto_reconnect)

            manager.set_auto_paused(False)
            self.assertTrue(a.auto_reconnect)
            self.assertEqual(a.connects, 2)
            self.assertFalse(flag_path("autoconnect-paused").exists())


if __name__ == "__main__":
    unittest.main()
