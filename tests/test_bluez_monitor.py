import unittest
from unittest.mock import patch

from shokz_monitor import bluetooth as bt


class BlueZMonitorTest(unittest.TestCase):
    def test_disconnect_clears_available(self):
        states = []
        with patch.object(bt.dbus, "SystemBus"), patch.object(
            bt, "RfcommReader"
        ), patch.object(bt.BlueZMonitor, "_initial_scan"), patch.object(
            bt.GLib, "timeout_add_seconds"
        ), patch.object(bt.GLib, "source_remove"):
            monitor = bt.BlueZMonitor(
                "AA:BB",
                lambda s: states.append((s.connected, s.available)),
                auto_reconnect=False,
            )
            path = "/org/bluez/hci0/dev_AA_BB"
            monitor._on_props_changed(bt._IFACE_DEV, {"Connected": True}, [], path=path)
            monitor._on_props_changed(bt._IFACE_DEV, {"Connected": False}, [], path=path)
        self.assertEqual(states[-1], (False, False))


if __name__ == "__main__":
    unittest.main()
