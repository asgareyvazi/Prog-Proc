"""Deterministic engineering reports (V7.7).

Composition reads certified decision and comparison packs. Rendering is a pure
function of the resulting ``ReportPack``: no database, no charting library, no
network, no second engineering calculation.
"""

from .contract import RENDERER_VERSION, REPORT_SCHEMA, ReportPack, ReportRequest
from .html import render_html, write_report_html
from .service import build_report
from .svg import render_svg

__all__ = [
    "RENDERER_VERSION",
    "REPORT_SCHEMA",
    "ReportPack",
    "ReportRequest",
    "build_report",
    "render_html",
    "render_svg",
    "write_report_html",
]
