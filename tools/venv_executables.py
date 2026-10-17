"""Virtualenv program paths for POSIX and Windows.

``venv`` installs ``bin/python`` on POSIX and ``Scripts/python.exe`` on
Windows. A path without the suffix is not a file ``CreateProcess`` can start.
Callers pass the returned path as one argv element. A directory name that
contains spaces is not split.
"""

from __future__ import annotations

import os
from pathlib import Path


def venv_bindir(root: Path, *, platform_name: str | None = None) -> Path:
    """``Scripts`` on Windows, ``bin`` elsewhere. ``root`` is the virtualenv."""
    platform = platform_name if platform_name is not None else os.name
    return root / ("Scripts" if platform == "nt" else "bin")


def venv_executable(root: Path, name: str, *, platform_name: str | None = None) -> Path:
    """Installed interpreter or console script inside ``root``.

    Windows names end in ``.exe`` unless ``name`` already has that suffix.
    The result is a path, not a shell command.
    """
    platform = platform_name if platform_name is not None else os.name
    filename = name
    if platform == "nt" and not name.lower().endswith(".exe"):
        filename = f"{name}.exe"
    return venv_bindir(root, platform_name=platform) / filename
