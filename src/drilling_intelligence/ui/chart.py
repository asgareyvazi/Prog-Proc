"""Native chart preview for one ReportExhibit.

The widget paints the shared layout. It does not query a database, score a well,
or decide that a missing value is zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..reporting.layout import layout_exhibit

_INK = QColor("#1c2430")
_MUTED = QColor("#6b7280")
_BAR = QColor("#4c6a82")
_PAPER = QColor("#f7f6f3")
_AXIS = QColor("#98a0a8")


class ExhibitChart(QWidget):
    """A painter for the same exhibit specification the SVG renderer consumes."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._exhibit: dict[str, Any] | None = None
        self.setMinimumHeight(180)
        self.setMinimumWidth(360)

    def set_exhibit(self, exhibit: Mapping[str, Any] | None) -> None:
        self._exhibit = dict(exhibit) if exhibit else None
        layout = self.drawing()
        self.setMinimumHeight(max(180, int(layout.get("height") or 180)))
        self.update()

    def exhibit(self) -> dict[str, Any] | None:
        return self._exhibit

    def drawing(self) -> dict[str, Any]:
        return layout_exhibit(self._exhibit)

    def state(self) -> str:
        return str((self._exhibit or {}).get("state") or "")

    def subject_ids(self) -> list[str]:
        return [
            str(subject.get("id") or "") for subject in (self._exhibit or {}).get("subjects") or []
        ]

    def series_count(self) -> int:
        return len((self._exhibit or {}).get("series") or [])

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        layout = self.drawing()
        painter.fillRect(self.rect(), _PAPER)
        painter.setPen(_INK)
        painter.drawText(8, 18, str(layout.get("title") or "No exhibit"))
        painter.setPen(_MUTED)
        unit = layout.get("unit") or "none stated"
        painter.drawText(8, 36, f"Chart state: {layout.get('state') or 'NO_DATA'}. Unit: {unit}.")
        reason = str(layout.get("reason") or "")
        if reason:
            painter.drawText(8, 52, reason[:180])
        plot_x = float(layout.get("plot_x") or 0)
        painter.setPen(QPen(_AXIS, 1))
        painter.drawLine(int(plot_x), 64, int(plot_x), max(80, self.height() - 8))
        painter.setPen(_INK)
        scale = 1.0
        layout_width = float(layout.get("width") or self.width() or 1)
        if self.width() > 0 and layout_width > self.width():
            scale = self.width() / layout_width
        if scale != 1.0:
            painter.scale(scale, scale)
        for label in layout.get("labels") or []:
            painter.setPen(_INK)
            painter.drawText(int(label["x"]), int(label["y"]), str(label.get("text") or ""))
        for bar in layout.get("bars") or []:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_BAR)
            if bar.get("zero") or float(bar.get("w") or 0) == 0.0:
                painter.setPen(QPen(_BAR, 2))
                painter.drawLine(
                    int(bar["x"]),
                    int(bar["y"]),
                    int(bar["x"]),
                    int(float(bar["y"]) + float(bar["h"])),
                )
            else:
                painter.drawRect(
                    QRectF(float(bar["x"]), float(bar["y"]), float(bar["w"]), float(bar["h"]))
                )
            painter.setPen(_INK)
            painter.drawText(
                int(float(bar["x"]) + float(bar["w"]) + 6),
                int(float(bar["y"]) + 12),
                str(bar.get("text") or ""),
            )
        painter.setPen(_MUTED)
        for marker in layout.get("markers") or []:
            painter.drawText(
                int(marker["x"]),
                int(float(marker["y"]) + 12),
                f"{marker.get('text') or ''} {marker.get('detail') or ''}".strip(),
            )
        painter.end()
