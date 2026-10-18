"""The Windows pilot script stays opt-in and does not claim a manual window run.

These checks read the script. They are not a Windows execution. The Windows
workflow runs the script.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path("tools/windows_pilot.ps1")


def test_pilot_script_requires_opt_in_and_does_not_claim_the_manual_checklist() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "param(" in text
    assert "[switch]$AllowInstall" in text
    assert "[switch]$AllowProviderProbe" in text
    assert "manual_window_checklist" in text
    assert "NOT RUN" in text
    assert "opened_user_window" in text
    assert "$false" in text
    assert "Get-ChildItem Env:" not in text
    assert "ai status" not in text or "AllowProviderProbe" in text
    assert "xvfb-run" not in text
    assert "safe_detail" in text


def test_pilot_script_uses_a_disposable_directory_with_a_space() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "drillintel pilot " in text
    assert "$env:TEMP" in text
    assert "sample workspace" in text
