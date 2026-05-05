"""
Cairo-rendered system tray icons.

Design: dark circle · battery progress arc · headphone silhouette.
Icons are cached by (battery_bucket, connected) and written to
~/.cache/shokz-monitor/icons/ so the directory listing stays stable.
"""
from __future__ import annotations

import math
import os
import threading
from pathlib import Path
from typing import Optional

import cairo

_ICON_DIR  = (
    Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    / "shokz-monitor" / "icons"
)
_ICON_SIZE = 128
_CACHE: dict[tuple, str] = {}

# Colour stops for the battery arc: (threshold%, R, G, B)
_COLOURS = [
    (100, 0.22, 0.80, 0.22),
    ( 50, 0.85, 0.75, 0.10),
    ( 25, 0.95, 0.45, 0.05),
    (  0, 0.85, 0.15, 0.10),
]


def _arc_colour(pct: int) -> tuple[float, float, float]:
    for i in range(len(_COLOURS) - 1):
        hi_pct, hr, hg, hb = _COLOURS[i]
        lo_pct, lr, lg, lb = _COLOURS[i + 1]
        if pct >= lo_pct:
            t = (pct - lo_pct) / max(hi_pct - lo_pct, 1)
            return (lr + t * (hr - lr), lg + t * (hg - lg), lb + t * (hb - lb))
    return _COLOURS[-1][1:]


def _draw_headphone(
    ctx: cairo.Context, cx: float, cy: float, sz: float, connected: bool
) -> None:
    alpha = 0.88 if connected else 0.35
    ctx.set_source_rgba(1.0, 1.0, 1.0, alpha)
    ctx.set_line_width(sz * 0.18)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)

    # Headband arc
    ctx.arc(cx, cy - sz * 0.08, sz * 0.68, math.pi, 0)
    ctx.stroke()

    # Left stem
    ctx.move_to(cx - sz * 0.68, cy - sz * 0.08)
    ctx.line_to(cx - sz * 0.68, cy + sz * 0.44)
    ctx.stroke()

    # Right stem
    ctx.move_to(cx + sz * 0.68, cy - sz * 0.08)
    ctx.line_to(cx + sz * 0.68, cy + sz * 0.44)
    ctx.stroke()

    # Ear cups
    cup_r = sz * 0.22
    for sign in (-1, 1):
        ctx.arc(cx + sign * sz * 0.68, cy + sz * 0.44, cup_r, 0, 2 * math.pi)
        ctx.fill()


def _render_icon(pct: Optional[int], connected: bool) -> str:
    _ICON_DIR.mkdir(parents=True, exist_ok=True)

    sz   = _ICON_SIZE
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, sz, sz)
    ctx  = cairo.Context(surf)
    cx   = cy = sz / 2.0
    r    = sz / 2.0 - 2

    # ── Background circle ─────────────────────────────────────────────────────
    ctx.arc(cx, cy, r, 0, 2 * math.pi)
    ctx.set_source_rgba(0.09, 0.09, 0.09, 0.93)
    ctx.fill()

    # ── Battery progress arc ──────────────────────────────────────────────────
    arc_r     = r * 0.73
    arc_w     = sz * 0.095
    arc_start = -math.pi / 2  # 12 o'clock

    if connected and pct is not None and pct > 0:
        # Background track
        ctx.set_source_rgba(0.28, 0.28, 0.28, 0.55)
        ctx.set_line_width(arc_w)
        ctx.arc(cx, cy, arc_r, arc_start, arc_start + 2 * math.pi)
        ctx.stroke()

        # Charged portion
        sweep   = 2 * math.pi * (pct / 100.0)
        cr, cg, cb = _arc_colour(pct)
        ctx.set_source_rgba(cr, cg, cb, 0.92)
        ctx.set_line_width(arc_w)
        ctx.arc(cx, cy, arc_r, arc_start, arc_start + sweep)
        ctx.stroke()
    elif not connected:
        # Grey disconnected track
        ctx.set_source_rgba(0.35, 0.35, 0.35, 0.45)
        ctx.set_line_width(arc_w)
        ctx.arc(cx, cy, arc_r, arc_start, arc_start + 2 * math.pi)
        ctx.stroke()

    # ── Outer border ring ─────────────────────────────────────────────────────
    ctx.arc(cx, cy, r - 0.5, 0, 2 * math.pi)
    ctx.set_source_rgba(0.55, 0.55, 0.55, 0.30)
    ctx.set_line_width(1.0)
    ctx.stroke()

    # ── Headphone icon ────────────────────────────────────────────────────────
    _draw_headphone(ctx, cx, cy, r * 0.40, connected)

    # ── Write PNG ─────────────────────────────────────────────────────────────
    tag  = f"{pct if pct is not None else 'x'}_{1 if connected else 0}"
    path = str(_ICON_DIR / f"icon_{tag}.png")
    try:
        surf.write_to_png(path)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to write icon %s: %s", path, exc)
        # Return whatever path we have — AppIndicator will show a missing-icon placeholder.
    surf.finish()
    return path


_CACHE_LOCK = threading.Lock()


def get_icon(battery: Optional[int], connected: bool) -> str:
    """Return path to a cached PNG icon for the given state (exact % precision)."""
    bucket = max(0, min(100, battery)) if battery is not None else None
    key = (bucket, connected)
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = _render_icon(bucket, connected)
        return _CACHE[key]


def prewarm() -> None:
    """Pre-render all 101 battery levels in a background thread.

    Returns immediately so startup is not delayed. By the time a real battery
    update arrives the icon will already be cached.
    """
    def _worker() -> None:
        for pct in range(0, 101):
            get_icon(pct, True)
        get_icon(None, True)
        get_icon(None, False)

    t = threading.Thread(target=_worker, daemon=True, name="shokz-icon-prewarm")
    t.start()
