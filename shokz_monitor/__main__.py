"""
shokz-monitor — event-driven Shokz headphone tray monitor for Linux.

Usage:
  python -m shokz_monitor [--mac AA:BB:CC:DD:EE:FF] [--name "My Headphones"]
                          [--no-auto-reconnect] [--verbose]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from shokz_monitor import __version__, DBUS_NAME
from shokz_monitor import SHOKZ_NAMES
from shokz_monitor.bluetooth import AudioDeviceManager, discover_audio_devices
from shokz_monitor.devices import AudioDevice, config_path, load_devices


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Event-driven Shokz headphone monitor for the Linux system tray."
    )
    p.add_argument("--mac",              metavar="AA:BB:CC:DD:EE:FF",
                   help="Bluetooth MAC address of your headphones.")
    p.add_argument("--name",             metavar="NAME", default=None,
                   help="Display name (auto-detected if omitted).")
    p.add_argument("--no-auto-reconnect", dest="auto_reconnect",
                   action="store_false", default=True,
                   help="Disable automatic reconnection on disconnect.")
    p.add_argument("--verbose", "-v",    action="store_true",
                   help="Enable debug logging.")
    p.add_argument("--version",          action="version",
                   version=f"shokz-monitor {__version__}")
    return p.parse_args()


def _single_instance(bus: dbus.SessionBus) -> bool:
    """
    Claim the well-known D-Bus name.  Returns True if we are the sole instance.
    Exits with a message if another instance is already running.
    """
    try:
        result = bus.request_name(
            DBUS_NAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE
        )
    except dbus.DBusException:
        return False

    if result != dbus.bus.REQUEST_NAME_REPLY_PRIMARY_OWNER:
        print(
            "shokz-monitor is already running.\n"
            "Use your system tray to interact with it.",
            file=sys.stderr,
        )
        sys.exit(0)
    return True


def _wayland_warn() -> None:
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session == "wayland" and not os.environ.get("WAYLAND_APPINDICATOR_OK"):
        print(
            "Warning: running under Wayland. System tray icons require the\n"
            "  'AppIndicator and KStatusNotifierItem Support' GNOME Shell extension.\n"
            "  Install it from https://extensions.gnome.org/extension/615/\n"
            "  or set WAYLAND_APPINDICATOR_OK=1 to suppress this warning.",
            file=sys.stderr,
        )


def main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _wayland_warn()

    # Integrate D-Bus with the GLib main loop before any other D-Bus calls.
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    session_bus = dbus.SessionBus()
    system_bus  = dbus.SystemBus()

    _single_instance(session_bus)

    # ── Resolve paired audio devices and saved priority ──────────────────────
    found = discover_audio_devices(system_bus)
    had_config = config_path().exists()
    devices = load_devices(found)

    if args.mac:
        mac = args.mac.upper()
        selected = next((device for device in devices if device.mac == mac), None)
        if selected is None:
            selected = AudioDevice(mac, args.name or "Bluetooth Audio")
        elif args.name:
            selected.name = args.name
        selected.auto = args.auto_reconnect
        devices = [selected] + [device for device in devices if device.mac != mac]
        for device in devices[1:]:
            device.auto = False
    elif not had_config and devices:
        shokz = next(
            (
                device
                for device in devices
                if any(name in device.name.lower() for name in SHOKZ_NAMES)
            ),
            devices[0],
        )
        devices.remove(shokz)
        devices.insert(0, shokz)
        shokz.auto = args.auto_reconnect
    elif not args.auto_reconnect:
        for device in devices:
            device.auto = False

    if not devices:
        sys.exit(
            "No paired Bluetooth audio device found.\n"
            "Pair one in Bluetooth Settings first, or specify --mac manually."
        )

    # ── Wire up monitor → indicator ───────────────────────────────────────────
    from shokz_monitor.indicator import ShokzIndicator

    indicator: list[ShokzIndicator] = []  # deferred so GTK init happens first

    def on_state(state, name) -> None:
        if indicator:
            GLib.idle_add(indicator[0].update, state, name)

    monitor = AudioDeviceManager(system_bus, devices, on_state)

    ind = ShokzIndicator(monitor, monitor.device_name)
    indicator.append(ind)

    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
