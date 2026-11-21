"""Shared design system for the workbench UI.

Single source of truth for the dark theme tokens and the stylesheet applied to the main
window. Pages and builder panes should use these tokens (via :func:`stylesheet`) instead of
ad-hoc colors so the 3-pane builder layouts and dashboard stay visually consistent.
"""

from __future__ import annotations

from typing import Any

#: Color tokens (dark workbench theme).
TOKENS: dict[str, str] = {
    "bg_window": "#20242a",
    "bg_surface": "#292f36",
    "border": "#48515c",
    "text": "#e7ebef",
    "text_muted": "#aeb9c4",
    "accent_selection": "#345e82",
    "warning_bg": "#54441e",
    "error_text": "#f08a8a",
    "radius": "3px",
}

#: Object-name-scoped extra rules layered on top of the base stylesheet.
EXTRA_RULES: dict[str, str] = {
    "workspaceLabel": "font-weight: 600;",
    "statusLabel": f"color: {TOKENS['text_muted']};",
    "warningBanner": f"background: {TOKENS['warning_bg']}; padding: 7px;",
}


def stylesheet() -> str:
    """The full workbench stylesheet built from the tokens."""
    t = TOKENS
    lines = [
        f"QMainWindow {{ background: {t['bg_window']}; color: {t['text']}; }}",
        f"QWidget {{ color: {t['text']}; }}",
        "QLineEdit, QComboBox, QTableView, QTreeWidget, QListWidget {",
        f"    background: {t['bg_surface']}; border: 1px solid {t['border']};"
        f" border-radius: {t['radius']};",
        f"    selection-background-color: {t['accent_selection']};",
        "}",
        "QPushButton { padding: 5px 10px; }",
        f"QGroupBox {{ border: 1px solid {t['border']}; margin-top: 9px; padding-top: 8px; }}",
        "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }",
    ]
    lines += [f"QLabel#{name} {{ {rule} }}" for name, rule in EXTRA_RULES.items()]
    return "\n".join(lines)


def apply_design_system(window: Any) -> None:
    """Apply the design-system stylesheet to a main window."""
    window.setStyleSheet(stylesheet())


def error_text_style() -> str:
    """Inline style for transient error text (used by the status label)."""
    return f"color: {TOKENS['error_text']};"
