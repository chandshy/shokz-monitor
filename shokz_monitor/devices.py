from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AudioDevice:
    mac: str
    name: str
    auto: bool = False


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "shokz-monitor" / "devices.json"


def flag_path(name: str) -> Path:
    return config_path().with_name(name)


def load_devices(
    discovered: list[tuple[str, str]], path: Path | None = None
) -> list[AudioDevice]:
    saved: list[dict] = []
    try:
        data = json.loads((path or config_path()).read_text())
        if isinstance(data, dict) and isinstance(data.get("devices"), list):
            saved = [item for item in data["devices"] if isinstance(item, dict)]
    except (FileNotFoundError, TypeError, ValueError, OSError):
        pass

    available = {mac.upper(): name for mac, name in discovered}
    devices = [
        AudioDevice(
            mac,
            available.pop(mac, item.get("name", mac)),
            bool(item.get("auto")),
        )
        for item in saved
        if (mac := str(item.get("mac", "")).upper())
    ]
    devices.extend(AudioDevice(mac, name) for mac, name in sorted(available.items()))
    return devices


def save_devices(devices: list[AudioDevice], path: Path | None = None) -> None:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"devices": [asdict(device) for device in devices]}, indent=2)
        + "\n"
    )


def preferred_device(devices: list[AudioDevice]) -> AudioDevice | None:
    return next((device for device in devices if device.auto), None)
