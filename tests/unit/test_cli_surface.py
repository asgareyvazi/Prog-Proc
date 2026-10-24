"""The command-line surface is a contract, and it is reviewed as a diff.

``tools/cli_surface.py`` renders every command, flag, default, choice and
handler of ``drillintel`` in a stable form.  The rendered form is committed
at ``tests/snapshots/cli_surface.txt``; this test says the built parser and
the committed snapshot are the same thing.

The snapshot was taken **before** the V8.17 refactor of ``build_parser``,
so a refactor that changed a flag, a default, a choice, an ordering or a
handler would fail here instead of reaching a user.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "snapshots" / "cli_surface.txt"

_SPEC = importlib.util.spec_from_file_location(
    "cli_surface", ROOT / "tools" / "cli_surface.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cli_surface = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli_surface)


def test_the_committed_snapshot_matches_the_built_parser() -> None:
    assert SNAPSHOT.exists(), "the CLI surface snapshot is missing"
    expected = SNAPSHOT.read_text(encoding="utf-8")
    actual = cli_surface.render_surface()
    if expected != actual:
        import difflib

        difference = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile="tests/snapshots/cli_surface.txt",
                tofile="the surface as built",
                lineterm="",
            )
        )
        raise AssertionError("the CLI surface changed:\n" + difference)


def test_the_check_mode_of_the_tool_agrees_with_the_test() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "cli_surface.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_snapshot_covers_every_top_level_command() -> None:
    text = SNAPSHOT.read_text(encoding="utf-8")
    for command in (
        "version",
        "workspace",
        "wells",
        "ingest",
        "search",
        "evidence",
        "index",
        "knowledge",
        "doctor",
        "ai",
        "timeline",
        "records",
        "fields",
        "analyze",
        "patterns",
        "lessons",
    ):
        assert f"command  {command}\n" in text, command


def test_the_snapshot_names_the_handler_of_every_command() -> None:
    """A command that lost its handler would parse and then do nothing."""
    text = SNAPSHOT.read_text(encoding="utf-8")
    for handler in (
        "command_version",
        "command_workspace_create",
        "command_wells",
        "command_ingest",
        "command_search",
        "command_evidence",
        "command_index",
        "command_knowledge",
        "command_doctor",
        "command_ai_status",
        "command_timeline",
        "command_records",
        "command_fields",
        "command_analyze",
        "command_patterns",
        "command_lessons",
    ):
        assert f"handler {handler}" in text, handler
