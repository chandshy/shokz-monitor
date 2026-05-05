"""
System tray indicator — GTK3 + AyatanaAppIndicator3.

Layout
  Panel label:  "73%"  (visible without clicking)
  Menu:         Status · Battery · ── · Connect · Disconnect
                ── · Setup Guide (conditional) · Quit
"""
from __future__ import annotations

import logging
import subprocess
import webbrowser
from typing import Optional

import gi
gi.require_version("Gtk",                  "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Notify",               "0.7")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator

from shokz_monitor import APP_ID
from shokz_monitor.bluetooth import BlueZMonitor, DeviceState
from shokz_monitor.icons import get_icon, prewarm
from shokz_monitor.notifications import Notifier

log = logging.getLogger(__name__)

_SETUP_URL  = (
    "https://github.com/chandshy/shokz-monitor"
    "#enable-battery-reporting-bluez-experimental"
)
_SETUP_CMD  = (
    "sudo sed -i '/^\\[Policy\\]/a Experimental = true' "
    "/etc/bluetooth/main.conf && sudo systemctl restart bluetooth"
)


class ShokzIndicator:
    def __init__(self, monitor: BlueZMonitor, device_name: str) -> None:
        self._monitor     = monitor
        self._device_name = device_name
        self._notifier    = Notifier()
        self._prev_conn:  Optional[bool] = None
        self._setup_shown = False

        prewarm()

        # ── AppIndicator ──────────────────────────────────────────────────────
        init_icon = get_icon(None, False)
        self._ind  = AppIndicator.Indicator.new(
            APP_ID, init_icon, AppIndicator.IndicatorCategory.HARDWARE
        )
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title(device_name)

        # ── Menu ──────────────────────────────────────────────────────────────
        self._menu = Gtk.Menu()

        self._item_status  = self._static_item("Searching…")
        self._item_battery = self._static_item("Battery: —")
        self._menu.append(Gtk.SeparatorMenuItem())

        item_connect    = Gtk.MenuItem(label="Connect Now")
        item_disconnect = Gtk.MenuItem(label="Disconnect")
        item_connect.connect("activate",    self._on_connect)
        item_disconnect.connect("activate", self._on_disconnect)
        self._item_connect    = item_connect
        self._item_disconnect = item_disconnect
        self._menu.append(item_connect)
        self._menu.append(item_disconnect)

        self._menu.append(Gtk.SeparatorMenuItem())

        self._item_setup = Gtk.MenuItem(
            label="Enable Battery Reporting →"
        )
        self._item_setup.connect("activate", self._on_setup_guide)
        self._item_setup.set_no_show_all(True)
        self._menu.append(self._item_setup)

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._on_quit)
        self._menu.append(item_quit)

        self._menu.show_all()
        self._ind.set_menu(self._menu)

    # ── State update (called from BlueZMonitor via GLib main loop) ────────────

    def update(self, state: DeviceState) -> None:
        connected = state.connected
        battery   = state.battery

        icon  = get_icon(battery, connected)
        label = self._make_label(battery, connected)

        self._ind.set_icon_full(icon, label)
        self._ind.set_label(f"  {label}", "")

        self._item_status.set_label(
            f"{self._device_name}: {'Connected' if connected else 'Disconnected'}"
        )
        self._item_battery.set_label(
            f"Battery: {battery}%" if battery is not None else "Battery: —"
        )

        # Connect / Disconnect sensitivity
        self._item_connect.set_sensitive(not connected)
        self._item_disconnect.set_sensitive(connected)

        # Setup guide visibility
        if state.battery_interface_missing and connected:
            self._item_setup.show()
            if not self._setup_shown:
                self._setup_shown = True
                self._notify_setup()
        else:
            self._item_setup.hide()

        # Transition notifications
        if self._prev_conn is not None and connected != self._prev_conn:
            if connected:
                self._notifier.on_connected(battery)
            else:
                self._notifier.on_disconnected()

        # Battery threshold notifications
        if connected and battery is not None:
            self._notifier.check_battery(battery)

        self._prev_conn = connected

    # ── Menu handlers ─────────────────────────────────────────────────────────

    def _on_connect(self, _widget: Gtk.MenuItem) -> None:
        self._monitor.connect()

    def _on_disconnect(self, _widget: Gtk.MenuItem) -> None:
        self._monitor.disconnect()

    def _on_setup_guide(self, _widget: Gtk.MenuItem) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Enable BlueZ Experimental Features",
        )
        dialog.format_secondary_markup(
            "Battery reporting requires BlueZ experimental features.\n\n"
            "<b>Run this command, then reconnect your headphones:</b>\n"
            f"<tt>{GLib.markup_escape_text(_SETUP_CMD)}</tt>\n\n"
            "Or see the README for the GUI method."
        )
        dialog.connect("response", lambda d, _: d.destroy())
        dialog.show()

    def _on_quit(self, _widget: Gtk.MenuItem) -> None:
        self._notifier.shutdown()
        Gtk.main_quit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _static_item(self, label: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(False)
        self._menu.append(item)
        return item

    @staticmethod
    def _make_label(battery: Optional[int], connected: bool) -> str:
        if not connected:
            return "—"
        if battery is None:
            return "?"
        return f"{battery}%"

    def _notify_setup(self) -> None:
        from gi.repository import Notify
        try:
            n = Notify.Notification.new(
                "Battery Reporting Unavailable",
                "BlueZ experimental features must be enabled. "
                "Click 'Enable Battery Reporting →' in the tray menu.",
                "dialog-information-symbolic",
            )
            n.set_urgency(Notify.Urgency.NORMAL)
            n.show()
        except Exception:
            pass
