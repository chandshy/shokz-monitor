"""
Shokz per-earbud battery reader — two parallel paths.

Path 1 (BLE GATT): subscribes to PropertiesChanged on the Shokz
proprietary GATT characteristics (UUID 77777777 / 0000fef1).  Fires
when the earbuds push A5 5A frames over BLE.

Path 2 (RFCOMM ch28): connects to the GAIA channel in a background
thread so the main loop is never blocked.  The earbuds broadcast CMD
0x30 battery frames passively every ~2 s on this channel without any
init sequence.  This channel is only available when the Shokz phone
app is NOT open (phone holds it exclusively when active).

Both paths call battery_cb(role, pct) for "left" or "right" whenever
a battery update is decoded.

CMD 0x30 decode (payload 44 bytes):
  payload[0]   device_id byte (stable per physical earbud)
  payload[1]   battery percent 0-100
  payload[42]  role flag: 0xFF = primary earbud -> "right",
               0x00 = secondary earbud -> "left"
Left/right assignment is tentative — primary earbud is the one that
accepted the host BT connection first.  Swap the role strings in
_parse_a55a() and re-verify with a btsnoop capture if needed.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
from typing import Callable

import dbus
from gi.repository import GLib

log = logging.getLogger(__name__)

_BLUEZ       = "org.bluez"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"
_GATT_IFACE  = "org.bluez.GattCharacteristic1"

_RFCOMM_CHANNEL = 28   # GAIA channel on Shokz OpenFit 2
_RFCOMM_TIMEOUT = 4.0  # seconds for connect attempt

# BLE GATT notify characteristics (relative to device object path)
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
    Per-earbud battery reader combining BLE GATT and RFCOMM ch28.

    start(device_path) activates both paths on the GLib main loop.
    stop() tears both down cleanly.  No persistent threads.
    """

    def __init__(self, mac: str, battery_cb: BatteryCallback) -> None:
        self._mac         = mac.upper()
        self._battery_cb  = battery_cb
        self._device_path: str | None = None

        # ── GATT path ──────────────────────────────────────────────────────────
        self._bus:         dbus.SystemBus | None = None
        self._signal_match = None
        self._notifiers:   list = []

        # ── RFCOMM ch28 path ───────────────────────────────────────────────────
        self._rfcomm_sock:  socket.socket | None = None
        self._rfcomm_watch: int | None = None
        self._rfcomm_buf:   bytes = b""

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self, device_path: str | None = None) -> None:
        if device_path:
            self._device_path = device_path
        if not self._device_path:
            log.debug("RfcommReader: no device path, skipping")
            return
        self._start_gatt()
        self._start_rfcomm()

    def stop(self) -> None:
        self._stop_gatt()
        self._stop_rfcomm()

    # ── GATT path ─────────────────────────────────────────────────────────────

    def _start_gatt(self) -> None:
        try:
            self._bus = dbus.SystemBus()
            self._signal_match = self._bus.add_signal_receiver(
                self._on_gatt_props,
                signal_name="PropertiesChanged",
                dbus_interface=_PROPS_IFACE,
                bus_name=_BLUEZ,
                path_keyword="path",
            )
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
        except Exception as exc:
            log.debug("GATT reader start error: %s", exc)

    def _stop_gatt(self) -> None:
        for char in self._notifiers:
            try:
                char.StopNotify()
            except Exception:
                pass
        self._notifiers.clear()
        if self._signal_match is not None:
            self._signal_match.remove()
            self._signal_match = None
        self._bus = None

    def _on_gatt_props(
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

    # ── RFCOMM ch28 path ──────────────────────────────────────────────────────

    def _start_rfcomm(self) -> None:
        t = threading.Thread(
            target=self._rfcomm_connect_thread,
            daemon=True,
            name="shokz-rfcomm-connect",
        )
        t.start()

    def _rfcomm_connect_thread(self) -> None:
        try:
            sock = socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            )
            sock.settimeout(_RFCOMM_TIMEOUT)
            sock.connect((self._mac, _RFCOMM_CHANNEL))
            sock.setblocking(False)
            GLib.idle_add(self._rfcomm_on_connected, sock)
        except OSError as exc:
            log.debug("RFCOMM ch28 unavailable (phone app may be open): %s", exc)

    def _rfcomm_on_connected(self, sock: socket.socket) -> bool:
        self._rfcomm_sock  = sock
        self._rfcomm_buf   = b""
        self._rfcomm_watch = GLib.io_add_watch(
            sock.fileno(),
            GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
            self._rfcomm_on_data,
        )
        log.info("RFCOMM ch28 connected — per-earbud battery active")
        return False  # one-shot idle

    def _rfcomm_on_data(self, _fd: int, condition: int) -> bool:
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            log.debug("RFCOMM ch28 closed by device")
            self._stop_rfcomm()
            return False

        try:
            chunk = self._rfcomm_sock.recv(256)
        except OSError:
            self._stop_rfcomm()
            return False

        if not chunk:
            self._stop_rfcomm()
            return False

        self._rfcomm_buf += chunk

        while True:
            idx = self._rfcomm_buf.find(b"\xa5\x5a")
            if idx < 0:
                self._rfcomm_buf = b""
                break
            if idx > 0:
                self._rfcomm_buf = self._rfcomm_buf[idx:]
            if len(self._rfcomm_buf) < 10:
                break
            pay_len = struct.unpack_from("<H", self._rfcomm_buf, 8)[0]
            total   = 10 + pay_len
            if len(self._rfcomm_buf) < total:
                break
            pkt = bytes(self._rfcomm_buf[:total])
            self._rfcomm_buf = self._rfcomm_buf[total:]
            for role, pct in _parse_a55a(pkt):
                log.info("RFCOMM ch28 battery %s=%d%%", role, pct)
                GLib.idle_add(self._battery_cb, role, pct)

        return True  # keep watching

    def _stop_rfcomm(self) -> None:
        if self._rfcomm_watch is not None:
            GLib.source_remove(self._rfcomm_watch)
            self._rfcomm_watch = None
        if self._rfcomm_sock is not None:
            try:
                self._rfcomm_sock.close()
            except Exception:
                pass
            self._rfcomm_sock = None
        self._rfcomm_buf = b""
