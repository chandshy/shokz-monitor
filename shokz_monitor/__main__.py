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
from shokz_monitor.bluetooth import BlueZMonitor, discover_shokz


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

    # ── Resolve device MAC ────────────────────────────────────────────────────
    mac  = args.mac
    name = args.name

    if not mac:
        found = discover_shokz(system_bus)
        if not found:
            sys.exit(
                "No paired Shokz device found.\n"
                "Pair your headphones in GNOME Bluetooth Settings first,\n"
                "or specify --mac AA:BB:CC:DD:EE:FF manually."
            )
        if len(found) > 1:
            print("Multiple Shokz devices found — specify --mac:\n")
            for addr, n in found:
                print(f"  {addr}  {n}")
            sys.exit(1)
        mac, discovered_name = found[0]
        name = name or discovered_name
        print(f"Auto-detected: {name}  ({mac})")

    name = name or "Shokz"

    # ── Wire up monitor → indicator ───────────────────────────────────────────
    from shokz_monitor.indicator import ShokzIndicator

    indicator: list[ShokzIndicator] = []  # deferred so GTK init happens first

    def on_state(state) -> None:
        if indicator:
            GLib.idle_add(indicator[0].update, state)

    monitor = BlueZMonitor(
        mac=mac,
        on_state_change=on_state,
        auto_reconnect=args.auto_reconnect,
    )

    ind = ShokzIndicator(monitor, name)
    indicator.append(ind)

    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
