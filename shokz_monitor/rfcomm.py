"""
Shokz per-earbud battery reader via BlueZ GATT (BLE).

Subscribes to PropertiesChanged on the Shokz proprietary GATT
characteristics on each connect event.  Parses incoming notifications
as A5 5A frames and fires battery_cb(role, pct) for "left"/"right"
when battery data is decoded.

The device exposes two notify channels:
  service 66666666 / char 77777777  (write+notify, Shokz SPP)
  service 0000fef0 / char 0000fef1  (notify, BES Technology)

CMD 0x30 (per-earbud battery) is broadcast passively every ~2 s over
RFCOMM ch28 (GAIA) and likely over the BLE SPP characteristic as well.
Frame decode was derived from passive RFCOMM captures; left/right role
assignment is tentative (confirmed primary=right in multipoint tests;
verify with a btsnoop capture if earbuds swap unexpectedly).
"""
from __future__ import annotations

import logging
import struct
from typing import Callable

import dbus
from gi.repository import GLib

log = logging.getLogger(__name__)

_BLUEZ       = "org.bluez"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_GATT_IFACE  = "org.bluez.GattCharacteristic1"

# Paths relative to the device object (e.g. /org/bluez/hci2/dev_XX_.../...)
# Notify channels: we subscribe to these on connect.
_NOTIFY_REL = [
    "servicea000/chara001",  # UUID 77777777-... (Shokz SPP, write+notify)
    "service8000/char8004",  # UUID 0000fef1-... (BES Technology, notify)
]

BatteryCallback = Callable[[str, int], None]


def _parse_a55a(data: bytes) -> list[tuple[str, int]]:
    """
    Parse an A5 5A notification frame for per-earbud battery data.
    Returns a list of (role, pct) pairs — empty if frame is unrecognised.

    Frame layout (little-endian):
      [0-1]  magic    A5 5A
      [2]    cmd      command byte
      [3]    sub      sub-command
      [4-7]  flags    (observed: 01 01 00 00)
      [8-9]  pay_len  payload length (LE uint16)
      [10+]  payload

    CMD 0x30 — per-earbud battery status (payload 44 bytes):
      payload[0]   device_id byte (stable per physical earbud)
      payload[1]   battery percent (0–100)
      payload[42]  role flag: 0xFF = primary earbud, 0x00 = secondary

    Left/right assignment: primary (0xFF) → "right", secondary (0x00) → "left".
    Observed in multipoint captures: primary earbud is the one that accepted
    the host connection first.  If earbuds swap roles unexpectedly, swap the
    role strings below and re-verify with a btsnoop capture.
    """
    if len(data) < 10 or data[0] != 0xa5 or data[1] != 0x5a:
        return []
    cmd     = data[2]
    pay_len = struct.unpack_from("<H", data, 8)[0]
    if len(data) < 10 + pay_len:
        return []
    payload = data[10:10 + pay_len]
    log.debug("A5 5A CMD=0x%02x sub=0x%02x payload(%d)=%s",
              cmd, data[3], pay_len, payload.hex() if payload else "")

    if cmd == 0x30 and pay_len >= 44:
        pct = payload[1]
        if 0 <= pct <= 100:
            role = "right" if payload[42] == 0xFF else "left"
            return [(role, pct)]

    return []


class RfcommReader:
    """
    Per-earbud battery reader via BlueZ GATT BLE notifications.

    start(device_path) subscribes to characteristic notifications on
    the GLib main loop.  stop() unsubscribes.  No threads, no polling,
    no phone required.
    """

    def __init__(self, mac: str, battery_cb: BatteryCallback) -> None:
        self._mac         = mac.upper()
        self._battery_cb  = battery_cb
        self._device_path: str | None = None
        self._bus:         dbus.SystemBus | None = None
        self._signal_match = None
        self._notifiers:    list = []

    def start(self, device_path: str | None = None) -> None:
        if device_path:
            self._device_path = device_path
        if not self._device_path:
            log.debug("GATT reader: no device path, skipping")
            return
        try:
            self._bus = dbus.SystemBus()
            self._signal_match = self._bus.add_signal_receiver(
                self._on_props_changed,
                signal_name="PropertiesChanged",
                dbus_interface=_PROPS_IFACE,
                bus_name=_BLUEZ,
                path_keyword="path",
            )
            self._subscribe()
            log.debug("GATT battery reader started on %s", self._device_path)
        except Exception as exc:
            log.debug("GATT reader start error: %s", exc)

    def stop(self) -> None:
        self._unsubscribe()
        if self._signal_match is not None:
            self._signal_match.remove()
            self._signal_match = None
        self._bus = None
        log.debug("GATT battery reader stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _subscribe(self) -> None:
        if not self._bus or not self._device_path:
            return
        for rel in _NOTIFY_REL:
            path = f"{self._device_path}/{rel}"
            try:
                char = dbus.Interface(
                    self._bus.get_object(_BLUEZ, path), _GATT_IFACE
                )
                char.StartNotify()
                self._notifiers.append(char)
                log.debug("GATT notify enabled: %s", rel)
            except dbus.DBusException as exc:
                log.debug("GATT StartNotify failed [%s]: %s", rel, exc)

    def _unsubscribe(self) -> None:
        for char in self._notifiers:
            try:
                char.StopNotify()
            except Exception:
                pass
        self._notifiers.clear()

    def _on_props_changed(
        self, interface: str, changed: dict, _invalidated: list, path: str = ""
    ) -> None:
        if interface != _GATT_IFACE:
            return
        if not self._device_path or not path.startswith(self._device_path):
            return
        if "Value" not in changed:
            return

        data = bytes(changed["Value"])
        log.debug("GATT [%s] %s", path.split("/")[-1], data.hex())

        for role, pct in _parse_a55a(data):
            log.info("GATT battery %s=%d%%", role, pct)
            GLib.idle_add(self._battery_cb, role, pct)
