import tempfile
import unittest
from pathlib import Path

from shokz_monitor.devices import (
    AudioDevice,
    load_devices,
    preferred_device,
    save_devices,
)


class DevicePreferencesTest(unittest.TestCase):
    def test_order_and_auto_survive_device_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            saved = [
                AudioDevice("BB:00", "Second", True),
                AudioDevice("AA:00", "First", True),
            ]
            save_devices(saved, path)

            devices = load_devices(
                [("AA:00", "First renamed"), ("BB:00", "Second"), ("CC:00", "New")],
                path,
            )

            self.assertEqual(
                [device.mac for device in devices], ["BB:00", "AA:00", "CC:00"]
            )
            self.assertEqual(preferred_device(devices).mac, "BB:00")
            self.assertEqual(devices[1].name, "First renamed")
            self.assertFalse(devices[2].auto)

            missing = load_devices([], path)
            self.assertEqual([device.mac for device in missing], ["BB:00", "AA:00"])


if __name__ == "__main__":
    unittest.main()
