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
from shokz_monitor.bluetooth import AudioDeviceManager, DeviceState
from shokz_monitor.devices import AudioDevice
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
    def __init__(self, monitor: AudioDeviceManager, device_name: str) -> None:
        self._monitor     = monitor
        self._device_name = device_name
        self._notifier    = Notifier()
        self._prev_conn:  Optional[bool] = None
        self._setup_shown = False
        self._last_state  = DeviceState()

        prewarm()

        # ── AppIndicator ──────────────────────────────────────────────────────
        init_icon = get_icon(None, False, monitor.scan_paused or monitor.auto_paused)
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

        item_devices = Gtk.MenuItem(label="Audio Devices…")
        item_devices.connect("activate", self._on_devices)
        self._menu.append(item_devices)

        item_pause = Gtk.CheckMenuItem(label="Pause Scanning")
        item_pause.set_active(monitor.scan_paused)
        item_pause.connect(
            "toggled", lambda w: self._toggle(self._monitor.set_scan_paused, w)
        )
        self._menu.append(item_pause)

        item_auto = Gtk.CheckMenuItem(label="Pause Auto-Connect")
        item_auto.set_active(monitor.auto_paused)
        item_auto.connect(
            "toggled", lambda w: self._toggle(self._monitor.set_auto_paused, w)
        )
        self._menu.append(item_auto)

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

    def update(self, state: DeviceState, device_name: Optional[str] = None) -> None:
        try:
            if device_name:
                self._device_name = device_name
                self._ind.set_title(device_name)
            self._apply_update(state)
        except Exception as exc:
            log.warning("Indicator update failed (AppIndicator proxy may have died): %s", exc)

    def _toggle(self, setter, widget: Gtk.CheckMenuItem) -> None:
        setter(widget.get_active())
        self.update(self._last_state)

    def _apply_update(self, state: DeviceState) -> None:
        self._last_state = state
        connected = state.connected
        battery   = state.battery
        paused    = self._monitor.scan_paused or self._monitor.auto_paused

        icon  = get_icon(battery, connected, paused)
        label = self._make_label(state)

        self._ind.set_icon_full(icon, label)
        self._ind.set_label(f"  {label}", "")

        self._item_status.set_label(
            f"{self._device_name}: {'Connected' if connected else 'Disconnected'}"
        )

        # Primary battery line: show aggregate or "—"
        if state.battery_left is not None or state.battery_right is not None:
            parts = []
            if state.battery_left  is not None: parts.append(f"L: {state.battery_left}%")
            if state.battery_right is not None: parts.append(f"R: {state.battery_right}%")
            if state.battery_case  is not None: parts.append(f"Case: {state.battery_case}%")
            self._item_battery.set_label("  ".join(parts))
        else:
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

        # Battery threshold notifications (use aggregate — lower of L/R)
        if connected and battery is not None:
            self._notifier.check_battery(battery)

        self._prev_conn = connected

    # ── Menu handlers ─────────────────────────────────────────────────────────

    def _on_connect(self, _widget: Gtk.MenuItem) -> None:
        self._monitor.connect()

    def _on_disconnect(self, _widget: Gtk.MenuItem) -> None:
        self._monitor.disconnect()

    def _on_devices(self, _widget: Gtk.MenuItem) -> None:
        dialog = Gtk.Dialog(
            title="Audio Devices",
            transient_for=None,
            flags=0,
            buttons=(
                "Cancel",
                Gtk.ResponseType.CANCEL,
                "Apply",
                Gtk.ResponseType.APPLY,
            ),
        )
        dialog.set_default_size(620, 340)
        box = dialog.get_content_area()
        note = Gtk.Label(
            label=(
                "The highest checked Auto device currently available is kept "
                "connected; other audio devices are disconnected."
            )
        )
        note.set_line_wrap(True)
        note.set_xalign(0)
        box.pack_start(note, False, False, 8)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for text, width in (
            ("Auto", 40),
            ("Device", 180),
            ("Address", 140),
            ("Status", 100),
        ):
            label = Gtk.Label(label=text, xalign=0)
            label.set_size_request(width, -1)
            header.pack_start(label, False, False, 0)
        box.pack_start(header, False, False, 4)

        device_list = Gtk.ListBox()
        device_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        rows = {}
        connect_buttons = []
        for device in self._monitor.refresh():
            status = (
                "Connected"
                if self._monitor.is_connected(device.mac)
                else "Disconnected"
            )
            row = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            auto = Gtk.CheckButton()
            auto.set_active(device.auto)
            auto.set_size_request(40, -1)
            line.pack_start(auto, False, False, 0)
            for text, width in ((device.name, 180), (device.mac, 140), (status, 100)):
                label = Gtk.Label(label=text, xalign=0)
                label.set_size_request(width, -1)
                line.pack_start(label, False, False, 0)
            connect = Gtk.Button(label="Connect")
            connect.set_sensitive(not self._monitor.is_connected(device.mac))
            connect.set_no_show_all(True)
            connect.connect(
                "clicked",
                lambda _button, mac=device.mac: self._monitor.connect_device(mac),
            )
            line.pack_start(connect, False, False, 0)
            row.add(line)
            device_list.add(row)
            rows[row] = (auto, device)
            if self._monitor.is_available(device.mac):
                connect_buttons.append(connect)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(device_list)
        box.pack_start(scroll, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        up = Gtk.Button(label="Move Up")
        down = Gtk.Button(label="Move Down")
        buttons.pack_start(up, False, False, 0)
        buttons.pack_start(down, False, False, 0)
        box.pack_start(buttons, False, False, 8)

        def move(offset: int) -> None:
            row = device_list.get_selected_row()
            if row is None:
                return
            index = row.get_index()
            target = index + offset
            if 0 <= target < len(rows):
                device_list.remove(row)
                device_list.insert(row, target)
                device_list.select_row(row)

        up.connect("clicked", lambda _button: move(-1))
        down.connect("clicked", lambda _button: move(1))
        dialog.show_all()
        for button in connect_buttons:
            button.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.APPLY:
            self._monitor.configure(
                [
                    AudioDevice(device.mac, device.name, auto.get_active())
                    for row in device_list.get_children()
                    for auto, device in (rows[row],)
                ]
            )
        dialog.destroy()

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
    def _make_label(state: DeviceState) -> str:
        if not state.connected:
            return "—"
        if state.battery_left is not None and state.battery_right is not None:
            return f"L:{state.battery_left} R:{state.battery_right}"
        if state.battery is not None:
            return f"{state.battery}%"
        return "?"

    def _notify_setup(self) -> None:
        try:
            from gi.repository import Notify
            n = Notify.Notification.new(
                "Battery Reporting Unavailable",
                "BlueZ experimental features must be enabled. "
                "Click 'Enable Battery Reporting →' in the tray menu.",
                "dialog-information-symbolic",
            )
            n.set_urgency(Notify.Urgency.NORMAL)
            n.show()
        except Exception as exc:
            log.warning("Could not send setup notification: %s", exc)
