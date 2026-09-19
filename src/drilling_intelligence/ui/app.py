"""Optional ``drillintel-ui`` application entry point.

The import remains lazy so the default headless package and CLI do not require Qt.  A missing Qt
runtime gets a short actionable error rather than a traceback from the dynamic linker.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drillintel-ui",
        description="Open the read-only Desktop Review Workbench for an existing workspace.",
    )
    parser.add_argument("--workspace", "-w", help="existing workspace directory")
    parser.add_argument("--well", help="well id or name to select after opening")
    parser.add_argument("--config", help="optional existing global settings TOML")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from PySide6.QtWidgets import QApplication

        from .main_window import MainWindow
    except (ImportError, OSError, RuntimeError) as exc:
        print(
            "drillintel-ui could not load Qt. Install the optional UI dependencies with "
            '`pip install "drilling-intelligence[ui]"` and ensure the host has its Qt/OpenGL '
            f"runtime libraries ({exc}).",
            file=sys.stderr,
        )
        return 2

    app = QApplication(["drillintel-ui", *(argv or sys.argv[1:])])
    app.setApplicationName("Prog-Proc Review Workbench")
    window = MainWindow(
        startup_workspace=args.workspace or "",
        startup_well=args.well or "",
        config_path=args.config or "",
    )
    window.show()
    return int(app.exec())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
