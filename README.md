# shokz-monitor

**The battery monitor Shokz Linux users have been waiting for.**

A lightweight, event-driven system tray monitor for Shokz wireless headphones on Ubuntu/GNOME. Zero polling — it wakes only when BlueZ fires a Bluetooth event. Per-earbud battery lives right in your panel as `L:80 R:90`, always visible, no clicking required.

---

## Features

- **Zero CPU between events** — pure D-Bus signal subscription, no polling loop
- **Per-earbud battery in the panel** — shows `L:80 R:90` from the RFCOMM ch28 (GAIA) protocol, decoded without the Shokz phone app
- **Smart low-battery alerts** — notifications at 20 / 15 / 10 / 5 %, driven by the lower of L/R
- **Auto-reconnect** — exponential backoff (10 → 120 s) when headphones go out of range
- **Auto-discovery** — finds your Shokz device automatically; no MAC address needed
- **BlueZ restart recovery** — re-syncs state if `systemctl restart bluetooth` is run
- **Battery re-registration workaround** — refreshes battery 5 s after reconnect, with a 10 s retry, working around a known BlueZ lazy-registration bug
- **Multipoint-aware** — filters out BT accessories forwarded from a paired phone so they don't pollute the earbud readings
- **Graceful degradation** — shows connection status and actionable setup guide if battery reporting isn't available
- **Works with any BlueZ audio device** — optimised for Shokz, compatible with most Bluetooth headphones

---

## Quick install

```bash
git clone https://github.com/chandshy/shokz-monitor.git
cd shokz-monitor
bash install.sh
```

Then start it:

```bash
python3 -m shokz_monitor
# or specify your MAC explicitly:
python3 -m shokz_monitor --mac A0:0C:E2:18:1B:3F
```

It auto-starts on login after installation.

---

## Requirements

Ubuntu 22.04+ (or any GNOME/GTK3 distro). The install script handles everything:

| Package | Purpose |
|---|---|
| `python3-dbus` | D-Bus / BlueZ integration |
| `python3-cairo` | Icon rendering |
| `gir1.2-ayatanaappindicator3-0.1` | System tray |
| `gir1.2-notify-0.7` | Desktop notifications |

No pip dependencies — only standard Ubuntu packages.

---

## Enable battery reporting (BlueZ experimental features)

BlueZ requires experimental features to expose the battery level of Bluetooth headphones. Without this, shokz-monitor shows connection status but not battery percentage.

**One-time setup:**

```bash
sudo sed -i '/^\[Policy\]/a Experimental = true' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
```

Then reconnect your headphones. Battery reporting will appear automatically.

> **Note:** Ubuntu 24.04 ships with experimental features enabled by default. You may not need this step.

If you prefer not to edit config files, the tray menu's **"Enable Battery Reporting →"** item walks you through it interactively.

---

## Shokz app note

shokz-monitor reads per-earbud battery via RFCOMM channel 28 (the GAIA channel). The Shokz Android/iOS app holds this channel exclusively when open — close it and your L/R readings will appear within a few seconds.

---

## Wayland note

Under Wayland, the system tray requires the
[AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/)
GNOME Shell extension. Install it from extensions.gnome.org, then log out and back in.

On X11 (Ubuntu 22.04 default) or GNOME on Wayland with the extension — everything works out of the box.

---

## Usage

```
python3 -m shokz_monitor [options]

Options:
  --mac AA:BB:CC:DD:EE:FF   Bluetooth MAC (auto-detected if omitted)
  --name NAME               Display name override
  --no-auto-reconnect       Disable automatic reconnection
  --verbose, -v             Enable debug logging
  --version                 Show version and exit
```

---

## How it works

shokz-monitor subscribes to BlueZ D-Bus signals — no timers, no polling threads.

| Signal | Source | Trigger |
|---|---|---|
| `InterfacesAdded` | ObjectManager | Device or battery interface appears |
| `InterfacesRemoved` | ObjectManager | Device goes out of range |
| `PropertiesChanged` | Device1 / Battery1 | Connected state or battery % changes |

### Per-earbud battery (RFCOMM ch28)

On connect, shokz-monitor opens RFCOMM channel 28 (the GAIA/BES channel) in a background thread. The earbuds passively broadcast CMD `0x30` frames every ~2 s without any interrogation sequence.

**Decoded CMD 0x30 fields (44-byte payload):**

| Offset | Field | Notes |
|---|---|---|
| `[0]` | device_id | Stable per physical device |
| `[1]` | battery % | 0–100; >100 means in charger (ignored) |
| `[30]` | link marker | `6` = direct Shokz component; other values = phone's BT accessories (filtered) |
| `[42]` | role | `0xFF` = right earbud (relayed via TWS mesh); `0x00` = left earbud |
| `[43]` | charging | `0x01` = device is seated in charger |

The left earbud (primary) handles the BT host connection and relays the right earbud's battery via the inter-earbud TWS link. The case battery is not available on this channel.

When the Shokz earbuds are multipoint-paired (PC + phone simultaneously), the phone's BT accessories also appear on ch28 with `payload[30] != 6` — these are filtered out automatically.

Between events the process is completely idle. CPU usage is effectively zero when your headphones are connected and steady.

---

## Tray icon

The icon shows a **progress arc** around a headphone silhouette:

- **Green arc** — battery 50–100 %
- **Yellow arc** — battery 25–50 %
- **Orange arc** — battery 10–25 %
- **Red arc** — battery below 10 %
- **Grey arc** — disconnected

The panel label updates in real time:

| Label | Meaning |
|---|---|
| `L:80 R:90` | Per-earbud from RFCOMM ch28 (lower of L/R drives low-battery alerts) |
| `73%` | Aggregate battery from BlueZ `Battery1` (fallback if RFCOMM unavailable) |
| `?` | Connected but no battery data yet |
| `—` | Disconnected |

---

## Supported devices

Tested on:

- Shokz OpenFit 2
- Shokz OpenRun Pro 2

Should work with any Bluetooth device that exposes `org.bluez.Battery1` (GATT Battery Service), including most modern Bluetooth headphones and earbuds.

---

## License

MIT © 2026 chandshy
