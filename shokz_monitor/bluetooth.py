"""
BlueZ D-Bus monitor — event-driven, zero polling.

Subscribes to BlueZ object-manager and property-change signals.
Calls on_state_change(DeviceState) whenever the device state changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import dbus
from gi.repository import GLib

from shokz_monitor.rfcomm import RfcommReader

log = logging.getLogger(__name__)

_BLUEZ       = "org.bluez"
_DBUS_OM     = "org.freedesktop.DBus.ObjectManager"
_DBUS_PROPS  = "org.freedesktop.DBus.Properties"
_IFACE_DEV   = "org.bluez.Device1"
_IFACE_BAT   = "org.bluez.Battery1"
_IFACE_ADAPT = "org.bluez.Adapter1"

# Seconds between successive reconnect attempts (last value repeats indefinitely).
_RECONNECT_SCHEDULE = [10, 20, 40, 80, 120]

# Seconds after connect to do a first battery refresh (BlueZ registers Battery1 lazily).
_BATTERY_REFRESH_DELAY  = 5
# If the first refresh gets battery_interface_missing, retry once more after this delay.
_BATTERY_REFRESH_RETRY  = 10


@dataclass
class DeviceState:
    connected: bool = False
    paired:    bool = False
    battery:   Optional[int] = None   # aggregate: min(L, R) or BlueZ Battery1 fallback
    battery_left:  Optional[int] = None
    battery_right: Optional[int] = None
    battery_case:  Optional[int] = None
    name:      str  = ""
    battery_interface_missing: bool = False  # experimental features not enabled


StateCallback   = Callable[[DeviceState], None]
SetupCallback   = Callable[[], None]


class BlueZMonitor:
    """
    Tracks one Bluetooth device identified by MAC address.

    All work happens on the GLib main loop — no threads.
    """

    def __init__(
        self,
        mac: str,
        on_state_change: StateCallback,
        auto_reconnect: bool = True,
    ) -> None:
        # Normalise MAC to BlueZ object-path format and display format.
        self._mac_path    = mac.upper().replace(":", "_")
        self._mac_display = mac.upper()

        self._on_change      = on_state_change
        self._auto_reconnect = auto_reconnect
        self._user_paused    = False   # set when user manually disconnects

        self._state       = DeviceState()
        self._device_path: Optional[str] = None

        self._reconnect_attempt = 0
        self._reconnect_timer:     Optional[int] = None
        self._battery_timer:       Optional[int] = None
        self._battery_retry_timer: Optional[int] = None
        self._rfcomm_timer:        Optional[int] = None

        self._rfcomm = RfcommReader(self._mac_display, self._on_rfcomm_battery)

        self._bus = dbus.SystemBus()
        self._bus.watch_name_owner(_BLUEZ, self._on_bluez_owner)
        self._setup_receivers()
        self._initial_scan()

    # ── D-Bus bookkeeping ─────────────────────────────────────────────────────

    def _setup_receivers(self) -> None:
        self._bus.add_signal_receiver(
            self._on_ifaces_added,
            signal_name="InterfacesAdded",
            dbus_interface=_DBUS_OM,
            bus_name=_BLUEZ,
        )
        self._bus.add_signal_receiver(
            self._on_ifaces_removed,
            signal_name="InterfacesRemoved",
            dbus_interface=_DBUS_OM,
            bus_name=_BLUEZ,
        )
        self._bus.add_signal_receiver(
            self._on_props_changed,
            signal_name="PropertiesChanged",
            dbus_interface=_DBUS_PROPS,
            bus_name=_BLUEZ,
            path_keyword="path",
        )

    def _initial_scan(self) -> None:
        try:
            om = dbus.Interface(self._bus.get_object(_BLUEZ, "/"), _DBUS_OM)
            for path, ifaces in om.GetManagedObjects().items():
                self._absorb(str(path), ifaces)
        except dbus.DBusException as exc:
            log.warning("BlueZ not reachable during initial scan: %s", exc)

    # ── BlueZ service lifecycle ───────────────────────────────────────────────

    def _on_bluez_owner(self, old_owner: str, new_owner: str) -> None:
        if new_owner:
            log.info("BlueZ restarted — re-scanning")
            GLib.timeout_add_seconds(2, self._initial_scan)
        else:
            log.warning("BlueZ went away")
            self._state.connected = False
            self._state.battery   = None
            self._on_change(self._state)

    # ── Object-manager signals ────────────────────────────────────────────────

    def _on_ifaces_added(self, path: str, ifaces: dict) -> None:
        self._absorb(str(path), ifaces)

    def _on_ifaces_removed(self, path: str, ifaces: list) -> None:
        if not str(path).endswith(f"dev_{self._mac_path}"):
            return
        changed = False
        if _IFACE_DEV in ifaces:
            self._state.connected = False
            self._state.battery   = None
            changed = True
        elif _IFACE_BAT in ifaces:
            self._state.battery = None
            changed = True
        if changed:
            self._on_change(self._state)

    def _absorb(self, path: str, ifaces: dict) -> None:
        if not path.endswith(f"dev_{self._mac_path}"):
            return
        self._device_path = path
        changed = False

        try:
            if _IFACE_DEV in ifaces:
                p = ifaces[_IFACE_DEV]
                new_conn   = bool(p.get("Connected", False))
                new_paired = bool(p.get("Paired", False))
                new_name   = str(p.get("Name", self._state.name))
                if (new_conn, new_paired, new_name) != (
                    self._state.connected, self._state.paired, self._state.name
                ):
                    self._state.connected = new_conn
                    self._state.paired    = new_paired
                    self._state.name      = new_name
                    changed = True

            if _IFACE_BAT in ifaces:
                pct = ifaces[_IFACE_BAT].get("Percentage")
                new_bat = int(pct) if pct is not None else None
                if new_bat != self._state.battery:
                    self._state.battery = new_bat
                    self._state.battery_interface_missing = False
                    changed = True
            elif self._state.connected and _IFACE_DEV in ifaces:
                # Device appeared connected but no Battery1 yet — the 5s refresh
                # timer will try again; flag it for now.
                self._state.battery_interface_missing = True
                changed = True
        except Exception as exc:
            log.warning("_absorb error for %s: %s", path, exc)

        if changed:
            self._on_change(self._state)

    # ── Property-change signals ───────────────────────────────────────────────

    def _on_props_changed(
        self, interface: str, changed: dict, _invalidated: list, path: str = ""
    ) -> None:
        if not str(path).endswith(f"dev_{self._mac_path}"):
            return

        updated = False

        try:
            if interface == _IFACE_DEV:
                if "Connected" in changed:
                    new_conn = bool(changed["Connected"])
                    if new_conn != self._state.connected:
                        self._state.connected = new_conn
                        updated = True
                        if new_conn:
                            self._on_connected()
                        else:
                            self._on_disconnected()
                if "Paired" in changed:
                    self._state.paired = bool(changed["Paired"])
                if "Name" in changed:
                    self._state.name = str(changed["Name"])
                    updated = True

            elif interface == _IFACE_BAT:
                if "Percentage" in changed:
                    pct = changed["Percentage"]
                    new_bat = int(pct) if pct is not None else None
                    if new_bat != self._state.battery:
                        self._state.battery = new_bat
                        self._state.battery_interface_missing = False
                        updated = True
        except Exception as exc:
            log.warning("_on_props_changed error [%s]: %s", interface, exc)

        if updated:
            self._on_change(self._state)

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def _on_connected(self) -> None:
        self._reconnect_attempt = 0
        self._user_paused = False
        if self._reconnect_timer is not None:
            GLib.source_remove(self._reconnect_timer)
            self._reconnect_timer = None
        if self._battery_retry_timer is not None:
            GLib.source_remove(self._battery_retry_timer)
            self._battery_retry_timer = None
        if self._battery_timer is not None:
            GLib.source_remove(self._battery_timer)
        self._battery_timer = GLib.timeout_add_seconds(
            _BATTERY_REFRESH_DELAY, self._refresh_battery
        )
        # Start GATT battery reader.
        if self._rfcomm_timer is not None:
            GLib.source_remove(self._rfcomm_timer)
        self._rfcomm_timer = GLib.timeout_add_seconds(2, self._start_rfcomm)

    def _on_disconnected(self) -> None:
        for attr in ("_battery_timer", "_battery_retry_timer", "_rfcomm_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                GLib.source_remove(timer)
                setattr(self, attr, None)
        self._rfcomm.stop()
        self._state.battery_left  = None
        self._state.battery_right = None
        self._state.battery_case  = None
        if self._auto_reconnect and not self._user_paused and self._state.paired:
            self._schedule_reconnect()

    def _start_rfcomm(self) -> bool:
        self._rfcomm_timer = None
        if self._state.connected:
            self._rfcomm.start(self._device_path)
        return False  # one-shot

    def _on_rfcomm_battery(self, role: str, pct: int) -> None:
        if role == "left":
            self._state.battery_left  = pct
        elif role == "right":
            self._state.battery_right = pct
        else:
            self._state.battery_case  = pct
        lr = [v for v in (self._state.battery_left, self._state.battery_right) if v is not None]
        if lr:
            self._state.battery = min(lr)
            self._state.battery_interface_missing = False
        self._on_change(self._state)

    def _refresh_battery(self) -> bool:
        self._battery_timer = None
        if not self._device_path or not self._state.connected:
            return False
        try:
            props = dbus.Interface(
                self._bus.get_object(_BLUEZ, self._device_path), _DBUS_PROPS
            )
            bat_props = props.GetAll(_IFACE_BAT)
            pct = bat_props.get("Percentage")
            if pct is not None:
                self._state.battery_interface_missing = False
                # Only use BlueZ Battery1 if RFCOMM hasn't provided per-earbud data.
                if self._state.battery_left is None and self._state.battery_right is None:
                    self._state.battery = int(pct)
                self._on_change(self._state)
            else:
                self._state.battery_interface_missing = True
                self._on_change(self._state)
                self._schedule_battery_retry()
        except dbus.DBusException as exc:
            err = str(exc)
            if "UnknownObject" in err or "UnknownInterface" in err:
                log.debug("Battery1 not yet available on %s — will retry", self._device_path)
                self._state.battery_interface_missing = True
                self._on_change(self._state)
                self._schedule_battery_retry()
            else:
                log.warning("Battery refresh transient error: %s", exc)
                self._schedule_battery_retry()
        return False  # one-shot

    def _schedule_battery_retry(self) -> None:
        if self._battery_retry_timer is not None:
            return
        self._battery_retry_timer = GLib.timeout_add_seconds(
            _BATTERY_REFRESH_RETRY, self._retry_battery
        )

    def _retry_battery(self) -> bool:
        self._battery_retry_timer = None
        if not self._device_path or not self._state.connected:
            return False
        try:
            props = dbus.Interface(
                self._bus.get_object(_BLUEZ, self._device_path), _DBUS_PROPS
            )
            bat_props = props.GetAll(_IFACE_BAT)
            pct = bat_props.get("Percentage")
            if pct is not None:
                self._state.battery_interface_missing = False
                if self._state.battery_left is None and self._state.battery_right is None:
                    self._state.battery = int(pct)
                self._on_change(self._state)
            else:
                log.warning("Battery1 still unavailable after retry on %s", self._device_path)
        except dbus.DBusException as exc:
            log.warning("Battery retry failed on %s: %s", self._device_path, exc)
        return False  # one-shot

    # ── Auto-reconnect ────────────────────────────────────────────────────────

    def _schedule_reconnect(self) -> None:
        if self._reconnect_timer is not None:
            return
        delay = _RECONNECT_SCHEDULE[
            min(self._reconnect_attempt, len(_RECONNECT_SCHEDULE) - 1)
        ]
        log.info(
            "Reconnect in %ds (attempt %d) — %s",
            delay, self._reconnect_attempt + 1, self._mac_display,
        )
        self._reconnect_timer = GLib.timeout_add_seconds(delay, self._do_reconnect)

    def _do_reconnect(self) -> bool:
        self._reconnect_timer = None
        if self._state.connected or not self._device_path:
            return False
        self._reconnect_attempt += 1
        try:
            dev = dbus.Interface(
                self._bus.get_object(_BLUEZ, self._device_path), _IFACE_DEV
            )
            dev.Connect(reply_handler=lambda: None, error_handler=self._reconnect_error)
        except dbus.DBusException as exc:
            log.debug("Reconnect attempt %d failed: %s", self._reconnect_attempt, exc)
            self._schedule_reconnect()
        return False  # one-shot

    def _reconnect_error(self, error: dbus.DBusException) -> None:
        log.debug("Async reconnect attempt %d error: %s", self._reconnect_attempt, error)
        if not self._state.connected:
            self._schedule_reconnect()

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """User-initiated connect."""
        self._user_paused = False
        self._reconnect_attempt = 0
        GLib.idle_add(self._do_reconnect)

    def disconnect(self) -> None:
        """User-initiated disconnect — suppresses auto-reconnect."""
        self._user_paused = True
        if self._reconnect_timer is not None:
            GLib.source_remove(self._reconnect_timer)
            self._reconnect_timer = None
        if not self._device_path:
            return
        try:
            dev = dbus.Interface(
                self._bus.get_object(_BLUEZ, self._device_path), _IFACE_DEV
            )
            dev.Disconnect(reply_handler=lambda: None, error_handler=lambda e: None)
        except dbus.DBusException:
            pass

    @property
    def device_path(self) -> Optional[str]:
        return self._device_path

    @property
    def mac(self) -> str:
        return self._mac_display


def discover_shokz(bus: dbus.SystemBus) -> list[tuple[str, str]]:
    """
    Scan BlueZ for paired Shokz devices.
    Returns list of (mac, name) tuples.
    """
    from shokz_monitor import SHOKZ_NAMES
    results: list[tuple[str, str]] = []
    try:
        om = dbus.Interface(bus.get_object(_BLUEZ, "/"), _DBUS_OM)
        for path, ifaces in om.GetManagedObjects().items():
            if _IFACE_DEV not in ifaces:
                continue
            props = ifaces[_IFACE_DEV]
            name  = str(props.get("Name", ""))
            addr  = str(props.get("Address", ""))
            paired = bool(props.get("Paired", False))
            if not paired or not addr:
                continue
            if any(kw in name.lower() for kw in SHOKZ_NAMES):
                results.append((addr, name))
    except dbus.DBusException as exc:
        log.error("BlueZ scan failed: %s", exc)
    return results
