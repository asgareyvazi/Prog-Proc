"""Locale-independent display formatting for reports.

Authoritative numbers stay in the report JSON. These helpers only choose how a
value is written into HTML, SVG, and CLI text. They never convert units and
never turn a missing value into zero.
"""

from __future__ import annotations

from typing import Any

_XML_ILLEGAL = {0x00, 0x0B, 0x0C}


def plain_text(value: Any) -> str:
    """Source text made safe to place in XML/HTML text or attributes.

    Markup characters are escaped. Characters illegal in XML 1.0 are dropped so
    a hostile or corrupt label cannot break the document. Unicode is preserved.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    cleaned = "".join(ch for ch in text if ord(ch) >= 32 or ch in "\t\n\r")
    cleaned = "".join(ch for ch in cleaned if ord(ch) not in _XML_ILLEGAL)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_number(value: Any) -> str:
    """A deterministic, locale-independent rendering of a number.

    Integers stay integers. Floats use a fixed decimal form (never scientific
    notation, never a thousands separator). Negative zero displays as ``0``.
    The caller's object is not modified.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "" if value is None else str(value)
    if isinstance(value, float):
        if value == 0.0:
            return "0"
        text = format(value, ".12f").rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def format_value(value: Any, unit: str | None = None) -> str:
    """Display text for one cell. ``None`` is an em dash, never ``0``."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = format_number(value)
    else:
        rendered = str(value)
    if unit:
        return f"{rendered} {unit}"
    return rendered


def is_number(value: Any) -> bool:
    """True for int/float, false for bool, None, and everything else."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)
