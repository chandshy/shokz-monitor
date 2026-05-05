"""
Threshold-based desktop notifications — no spam.

Low-battery alerts fire at 20 / 15 / 10 / 5 %.
Thresholds reset when the device reconnects (battery may have charged).
"""
from __future__ import annotations

import logging
from typing import Optional

from gi.repository import Notify

log = logging.getLogger(__name__)

LOW_THRESHOLDS = (20, 15, 10, 5)


class Notifier:
    def __init__(self, app_name: str = "shokz-monitor") -> None:
        Notify.init(app_name)
        self._fired: set[int] = set()
        self._last_connected: Optional[bool] = None

    # ── Called by indicator on state change ───────────────────────────────────

    def on_connected(self, battery: Optional[int]) -> None:
        self._fired.clear()
        bat_str = f"Battery: {battery}%" if battery is not None else "Battery level unknown"
        self._show("Connected", bat_str, "audio-headset-symbolic", Notify.Urgency.LOW)

    def on_disconnected(self) -> None:
        self._show("Disconnected", "", "audio-headset-symbolic", Notify.Urgency.LOW)

    def check_battery(self, pct: int) -> None:
        for threshold in LOW_THRESHOLDS:
            if pct <= threshold and threshold not in self._fired:
                self._fired.add(threshold)
                urgency = (
                    Notify.Urgency.CRITICAL if pct <= 10 else Notify.Urgency.NORMAL
                )
                self._show(
                    "Low Battery",
                    f"{pct}% remaining — charge soon",
                    "battery-caution-symbolic",
                    urgency,
                )
                break

    # ── Internal ──────────────────────────────────────────────────────────────

    def _show(
        self,
        title: str,
        body: str,
        icon: str,
        urgency: Notify.Urgency,
    ) -> None:
        try:
            n = Notify.Notification.new(title, body or None, icon)
            n.set_urgency(urgency)
            n.show()
        except Exception as exc:
            log.warning("Notification failed: %s", exc)

    def shutdown(self) -> None:
        try:
            Notify.uninit()
        except Exception:
            pass
