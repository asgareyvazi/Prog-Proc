"""Deterministic SVG for one exhibit specification.

No database, no chart library, no script, no external resource. Source strings
are escaped before they are written. A null value never becomes a bar.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .format import format_value, plain_text
from .layout import layout_exhibit

_SVG_NS = "http://www.w3.org/2000/svg"


def _f(value: float) -> str:
    return f"{float(value):.2f}"


def render_svg(exhibit: Mapping[str, Any]) -> str:
    """SVG text for one exhibit. The same exhibit always produces the same bytes."""
    layout = layout_exhibit(exhibit)
    width = int(layout["width"])
    height = int(layout["height"])
    title = plain_text(layout.get("title") or exhibit.get("title") or "")
    state = plain_text(layout.get("state") or "")
    unit = layout.get("unit")
    unit_text = plain_text(unit) if unit else "none stated"
    reason = plain_text(layout.get("reason") or exhibit.get("reason") or "")
    exhibit_id = "".join(
        ch if ch.isalnum() or ch in "-_" else "-"
        for ch in str(exhibit.get("exhibit_id") or "exhibit")
    )
    parts = [
        (
            f'<svg xmlns="{_SVG_NS}" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" id="{exhibit_id}">'
        ),
        f"<title>{title}</title>",
        f"<desc>Chart state {state}. Unit {unit_text}. {reason}</desc>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f7f6f3"/>',
        f'<text x="12" y="22" fill="#1c2430" font-family="sans-serif" font-size="14">{title}</text>',
        (
            f'<text x="12" y="42" fill="#3d4754" font-family="sans-serif" font-size="12">'
            f"Chart state: {state}. Unit: {unit_text}.</text>"
        ),
    ]
    if reason:
        parts.append(
            f'<text x="12" y="58" fill="#3d4754" font-family="sans-serif" font-size="11">{reason}</text>'
        )
    plot_x = float(layout["plot_x"])
    parts.append(
        f'<line x1="{_f(plot_x)}" y1="64" x2="{_f(plot_x)}" y2="{_f(height - 12)}" '
        f'stroke="#98a0a8" stroke-width="1"/>'
    )
    for label in layout["labels"]:
        parts.append(
            f'<text x="{_f(label["x"])}" y="{_f(label["y"])}" fill="#1c2430" '
            f'font-family="sans-serif" font-size="11">{plain_text(label["text"])}</text>'
        )
    for bar in layout["bars"]:
        width_px = float(bar["w"])
        if bar.get("zero") or width_px == 0.0:
            parts.append(
                f'<line x1="{_f(bar["x"])}" y1="{_f(bar["y"])}" x2="{_f(bar["x"])}" '
                f'y2="{_f(float(bar["y"]) + float(bar["h"]))}" stroke="#4c6a82" stroke-width="2" '
                f'class="zero"/>'
            )
        else:
            parts.append(
                f'<rect class="bar" x="{_f(bar["x"])}" y="{_f(bar["y"])}" width="{_f(width_px)}" '
                f'height="{_f(bar["h"])}" fill="#4c6a82"/>'
            )
        value_text = plain_text(format_value(bar.get("value"), bar.get("unit")))
        parts.append(
            f'<text x="{_f(float(bar["x"]) + width_px + 6)}" y="{_f(float(bar["y"]) + 11)}" '
            f'fill="#1c2430" font-family="sans-serif" font-size="11">{value_text}</text>'
        )
    for marker in layout["markers"]:
        parts.append(
            f'<text class="missing" x="{_f(marker["x"])}" y="{_f(float(marker["y"]) + 12)}" '
            f'fill="#6b7280" font-family="sans-serif" font-size="11">'
            f"{plain_text(marker.get('text'))} {plain_text(marker.get('detail'))}</text>"
        )
    legend_x = 12
    for index, item in enumerate(layout.get("legend") or []):
        if len(layout.get("legend") or []) < 2:
            break
        parts.append(
            f'<text x="{legend_x}" y="{_f(height - 8)}" fill="#3d4754" '
            f'font-family="sans-serif" font-size="10">'
            f"{plain_text(item.get('label'))}</text>"
        )
        legend_x += 120
        if index > 6:
            break
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
