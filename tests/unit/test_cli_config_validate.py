"""The config-validation CLI is a read-only TOML/environment check."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

from drilling_intelligence.cli.app import main


def _call(*args: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = StringIO(), StringIO()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = main(list(args))
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
    return code, json.loads(stdout.getvalue())


def test_config_validate_reports_unknown_keys_without_opening_workspace_or_network(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "operator.toml"
    config.write_text(
        '[ai]\nprovider = "none"\n[mineru]\nmode = "disabled"\nfuture_option = "noted"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "drilling_intelligence.cli.app._open_workspace",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("workspace must not open")),
    )
    monkeypatch.setattr(
        "drilling_intelligence.integrations.base.http_json",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("configuration validation must not probe")
        ),
    )

    code, payload = _call("config", "validate", "--config", str(config), "--json")

    assert code == 0
    assert payload["valid"] is True
    assert payload["database_opened"] is False
    assert payload["workspace_opened"] is False
    assert payload["services_probed"] is False
    assert payload["unknown_keys"] == ["mineru.future_option"]
    assert not (tmp_path / ".drillintel").exists()


def test_config_validate_strict_mode_rejects_unknown_keys(tmp_path: Path) -> None:
    config = tmp_path / "unknown.toml"
    config.write_text("[mineru]\nnot_a_real_setting = true\n", encoding="utf-8")

    code, payload = _call("config", "validate", "--config", str(config), "--strict", "--json")

    assert code == 1
    assert payload["ok"] is False
    assert payload["context"]["problems"] == [
        "unknown configuration key: mineru.not_a_real_setting"
    ]


def test_config_validate_returns_settings_validation_errors_as_json(tmp_path: Path) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text(
        '[mineru]\nhttp_parse_path = "//example.test/parse"\nmode = "mystery"\n', encoding="utf-8"
    )

    code, payload = _call("config", "validate", "--config", str(config), "--json")

    assert code == 1
    assert payload["ok"] is False
    problems = " ".join(str(item) for item in payload["context"]["problems"])
    assert "mineru.mode" in problems
    assert "mineru.http_parse_path" in problems
