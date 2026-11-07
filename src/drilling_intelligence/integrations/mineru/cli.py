"""Resolve the supported MinerU CLI generation without importing the runtime."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path


def resolve_cli_binary(
    configured: str, *, find_executable: Callable[[str], str | None] = shutil.which
) -> str:
    """Prefer MinerU 4's ``mineru-kit`` when the configured name is ``mineru``.

    MinerU 4 installs both entry points: ``mineru`` is its document-library CLI,
    while ``mineru-kit`` exposes the stateless parse command used by this adapter.
    Older installs that expose only ``mineru`` retain the legacy invocation.
    Explicit custom executable names and paths are left untouched.
    """
    name = Path(configured).name.lower()
    if name not in {"mineru", "mineru.exe"}:
        return configured

    kit_name = "mineru-kit.exe" if os.name == "nt" else "mineru-kit"
    configured_path = Path(configured).expanduser()
    if configured_path.parent != Path():
        sibling = configured_path.with_name(kit_name)
        if sibling.is_file():
            return str(sibling)
    else:
        found = find_executable(kit_name)
        if found:
            return str(found)
    return configured


def is_mineru_kit(binary: str) -> bool:
    """Whether a resolved entry point is MinerU 4's stateless parser CLI."""
    return Path(binary).name.lower() in {"mineru-kit", "mineru-kit.exe", "mineru-kit.py"}


__all__ = ["is_mineru_kit", "resolve_cli_binary"]
