"""The Windows acceptance path is a path, and it is still NOT RUN.

`docs/WINDOWS_TESTING.md` is the only document an owner reads before deciding
whether this platform works on their workstation.  Two failures matter more
than a typo in it:

* a step that cannot be executed as written, because it names a command that
  does not exist, or a flag the tool does not take;
* a checklist that has been ticked without anybody running it.  That is how a
  project claims a desktop acceptance it never performed.

So the document is checked against the tools: every command it prints exists,
the stress command it prints is the gated one, every step has a record row,
and no row anywhere says a step passed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "WINDOWS_TESTING.md"

#: The subjects the acceptance path has to cover.  A missing one is a step the
#: owner cannot perform, not a formatting preference.
REQUIRED_SUBJECTS = {
    "windows edition and build": "WindowsProductName",
    "python version": "-VV",
    "exact sha": "git rev-parse HEAD",
    "exit codes": "$LASTEXITCODE",
    "workspace and well": "wells create",
    "offline doctor": "doctor",
    "synthetic ingest": "ingest",
    "search and index rebuild": "index rebuild",
    "html report": "fields report",
    "workbench start and close": "drillintel-ui.exe",
    "read-only review": "read-only",
    "non-ascii console": "PYTHONUTF8",
    "sanitized logs": ".drillintel",
    "shutdown stress with 25 attempts": "--attempts 25",
    "ran versus skipped": "skip",
    "disposable workspace": "disposable",
}


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_document_covers_every_acceptance_subject() -> None:
    text = _text().lower()
    missing = [name for name, marker in REQUIRED_SUBJECTS.items() if marker.lower() not in text]
    assert not missing, missing


def test_the_stress_command_is_the_gated_one() -> None:
    """An ungated command in the acceptance path measures without failing."""
    text = _text()
    assert "qt_shutdown_stress.py --attempts 25" in text
    for line in text.splitlines():
        if "qt_shutdown_stress.py --attempts 25" in line:
            assert "--gate" in line, line


def test_no_checkbox_is_ticked() -> None:
    assert "- [x]" not in _text().lower()
    assert "[x]" not in _text().lower()


def test_every_recorded_step_is_still_not_run() -> None:
    """The record has one row per step, and every row says NOT RUN until the
    owner writes the result of the step on their own workstation."""
    rows = [line for line in _text().splitlines() if re.match(r"^\| \d+ \|", line)]
    assert len(rows) == 16, len(rows)
    for row in rows:
        assert "| NOT RUN |" in row, row


def test_the_steps_are_numbered_and_the_record_matches_them() -> None:
    text = _text()
    steps = re.findall(r"^(\d+)\. \*\*", text, flags=re.MULTILINE)
    assert [int(number) for number in steps] == list(range(1, 17))
    recorded = [int(number) for number in re.findall(r"^\| (\d+) \|", text, flags=re.MULTILINE)]
    assert recorded == list(range(1, 17))


def test_the_document_names_the_two_windows_skips() -> None:
    text = _text()
    assert "open database file" in text or "open-database-file" in text
    assert "handle count" in text.lower()


def test_the_document_does_not_claim_desktop_acceptance() -> None:
    text = _text()
    assert "**Status: NOT RUN.**" in text
    lowered = text.lower()
    assert "desktop acceptance: pass" not in lowered
    # The one sentence that mentions full support has to be the disclaimer.
    for line in lowered.splitlines():
        if "fully supported" in line:
            assert "not a claim" in lowered, line


def test_the_tool_flags_the_document_prints_exist() -> None:
    """Every `--flag` on a `drillintel.exe` line is a flag the CLI accepts.

    The check reads the parser rather than a help screen, so it does not need
    an installed console script to answer.
    """
    import sys

    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from drilling_intelligence.cli.app import build_parser

    parser = build_parser()
    known: set[str] = set()

    def collect(candidate: object) -> None:
        for action in getattr(candidate, "_actions", []):
            known.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for sub in choices.values():
                    collect(sub)

    collect(parser)
    assert known

    printed: set[str] = set()
    for line in _text().splitlines():
        if "drillintel.exe" not in line:
            continue
        printed.update(re.findall(r"(--[a-z][a-z0-9-]+)", line))
    assert printed, "the document prints no drillintel.exe command"
    unknown = {flag for flag in printed if flag not in known}
    assert not unknown, sorted(unknown)
