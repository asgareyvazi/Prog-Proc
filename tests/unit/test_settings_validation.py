"""Settings are checked on load, not discovered mid-run (spec section 62, P2).

Every one of these values decides how much of a document a user gets to see, or how long a
run hangs before it gives up.  A negative timeout, ``pdf_max_pages = 0``, ``excel_max_cells
= -5`` or ``mineru.mode = "clii"`` is a typo with consequences: under the old behaviour the
first two produced empty extractions that looked like complete ones, the third an infinite
retry loop against a dead Ollama, and the last a silent refusal to use the layout parser.

So :meth:`Settings.load` validates and raises :class:`ConfigurationError` naming every
offending key at once - a person editing a config should not have to run the app four times
to find four typos.  The table in ``_MINIMUMS``/``_RANGES``/``_CHOICES`` is deliberately a
lookup table rather than a validation framework; there is no pydantic here on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.core.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def load(tmp_path, body: str) -> Settings:
    return Settings.load(write(tmp_path, body), apply_env=False)


# --------------------------------------------------------------------------- the shape of the check
def test_the_shipped_development_config_is_valid() -> None:
    """The config users actually copy must survive validation, or validation is a bug."""
    settings = Settings.load(ROOT / "configs" / "development.toml", apply_env=False)
    settings.validate()
    assert settings.extraction.excel_max_cells >= 1
    assert settings.mineru.mode in {"auto", "cli", "http", "disabled"}


def test_defaults_are_valid(tmp_path) -> None:
    settings = load(tmp_path, "[extraction]\n")
    settings.validate()


def test_every_problem_is_reported_in_one_error(tmp_path) -> None:
    """Not one-per-run: a config edit should be fixable in one pass."""
    with pytest.raises(ConfigurationError) as caught:
        load(
            tmp_path,
            """
            [ai]
            timeout_seconds = -5
            retries = -1
            temperature = 7.5
            [mineru]
            mode = "clii"
            """,
        )
    problems = caught.value.context["problems"]
    assert len(problems) == 4, problems
    joined = "; ".join(problems)
    for key in ("ai.timeout_seconds", "ai.retries", "ai.temperature", "mineru.mode"):
        assert key in joined, joined
    assert caught.value.code == "CONFIGURATION"
    assert str(caught.value).count("must") >= 4


# --------------------------------------------------------------------------- individual rules
@pytest.mark.parametrize(
    ("section", "key", "value", "expectation"),
    [
        ("ai", "timeout_seconds", "-5", "must be >="),
        ("ai", "retries", "-1", "must be >="),
        ("ai", "max_context_tokens", "0", "must be >="),
        ("ai", "max_output_tokens", "-1", "must be >="),
        ("ai", "temperature", "-0.1", "must be between"),
        ("ai", "temperature", "2.5", "must be between"),
        ("ai", "provider", '"not-a-provider"', "must be one of"),
        ("mineru", "timeout_seconds", "0", "must be >="),
        ("mineru", "mode", '"clii"', "must be one of"),
        ("mineru", "backend", '"gpu-magic"', "must be one of"),
        ("mineru", "prefer_when_pages_above", "-3", "must be >="),
        ("extraction", "pdf_max_pages", "0", "must be >="),
        ("extraction", "pdf_min_table_rows", "0", "must be >="),
        ("extraction", "pdf_probe_pages", "-2", "must be >="),
        ("extraction", "excel_max_sheets", "0", "must be >="),
        ("extraction", "excel_max_cells", "-5", "must be >="),
        ("extraction", "excel_max_bytes", "0", "must be >="),
        ("extraction", "text_max_bytes", "-1", "must be >="),
        ("ingestion", "max_file_size_mb", "0", "must be >="),
        ("ingestion", "hash_chunk_bytes", "0", "must be >="),
        ("search", "hybrid_results", "0", "must be >="),
        ("database", "sqlite_busy_timeout_ms", "-1", "must be >="),
        ("logging", "level", '"verbose"', "must be one of"),
        ("ui", "window_width", "10", "must be >="),
    ],
)
def test_an_out_of_range_value_is_rejected(
    tmp_path, section: str, key: str, value: str, expectation: str
) -> None:
    with pytest.raises(ConfigurationError, match=rf"{section}\.{key}") as caught:
        load(tmp_path, f"[{section}]\n{key} = {value}\n")
    assert expectation in "; ".join(caught.value.context["problems"])


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("ai", "timeout_seconds", "0.001"),
        ("ai", "retries", "0"),
        ("ai", "temperature", "0"),
        ("ai", "temperature", "2.0"),
        ("extraction", "pdf_max_pages", "1"),
        ("extraction", "excel_max_cells", "1"),
        ("extraction", "pdf_probe_pages", "1"),
        ("ingestion", "hash_chunk_bytes", "1"),
    ],
)
def test_a_boundary_value_is_accepted(tmp_path, section: str, key: str, value: str) -> None:
    """The bounds are inclusive: a run with one page or zero retries is legitimate."""
    settings = load(tmp_path, f"[{section}]\n{key} = {value}\n")
    settings.validate()  # must not raise
    assert getattr(settings, section).__getattribute__(key) is not None


def test_a_negative_timeout_does_not_reach_the_http_client(tmp_path) -> None:
    """The point of validating: the value is unusable *before* anything reads it."""
    path = write(tmp_path, '[ai]\ntimeout_seconds = -3\nprovider = "none"\n')
    with pytest.raises(ConfigurationError):
        Settings.load(path, apply_env=False)
    # The same file is fine once fixed, and the fix is what the message says to do.
    path.write_text('[ai]\ntimeout_seconds = 30\nprovider = "none"\n', encoding="utf-8")
    settings = Settings.load(path, apply_env=False)
    assert settings.ai.timeout_seconds == pytest.approx(30.0)


# --------------------------------------------------------------------------- authority ladder
def test_an_empty_or_repeated_authority_hierarchy_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="at least one tier"):
        load(tmp_path, "[authority]\nhierarchy = []\n")
    with pytest.raises(ConfigurationError, match="repeats tiers"):
        load(tmp_path, "[authority]\nhierarchy = ['field_note', 'field_note', 'manual']\n")


def test_the_hierarchy_order_is_left_to_the_owner(tmp_path) -> None:
    """Ordering the ladder is a judgement call; the loader validates shape, not taste."""
    settings = load(tmp_path, "[authority]\nhierarchy = ['manual', 'field_note']\n")
    assert settings.authority.hierarchy == ["manual", "field_note"]


# --------------------------------------------------------------------------- what stays permissive
def test_an_unknown_key_is_reported_but_not_fatal(tmp_path) -> None:
    """The UI shows unknown keys; rejecting them would break every forward-compatible config.

    ``[ai] enabled = false`` is written by the test suite itself, and a validator that
    refused unknown keys would make a downgrade of the app fail to start.
    """
    settings = load(tmp_path, "[ai]\nenabled = false\n[extraction]\nexcel_max_cells = 5000\n")
    assert settings.extraction.excel_max_cells == 5000
    assert any("ai.enabled" in key or "enabled" in key for key in settings.unknown_keys), (
        settings.unknown_keys
    )


def test_environment_overrides_are_validated_too(monkeypatch) -> None:
    monkeypatch.setenv("DRILLINTEL_EXTRACTION__EXCEL_MAX_CELLS", "-4")
    with pytest.raises(ConfigurationError, match=r"extraction\.excel_max_cells"):
        Settings.load(apply_env=True)


def test_an_environment_override_keeps_the_type_of_the_value_it_replaces(monkeypatch) -> None:
    """A string "600" reaching httpx as a timeout is a runtime error, not a config one.

    The environment is the documented way to set these on a machine without editing a
    file, so the coercion has to happen there too - including for booleans, where
    ``DRILLINTEL_EXTRACTION__CACHE_ENABLED=false`` must mean False and not "false"
    (truthy), which would leave the cache switched on against the operator's wishes.
    """
    monkeypatch.setenv("DRILLINTEL_AI__TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("DRILLINTEL_AI__RETRIES", "7")
    monkeypatch.setenv("DRILLINTEL_EXTRACTION__CACHE_ENABLED", "false")
    monkeypatch.setenv("DRILLINTEL_AUTHORITY__HIERARCHY", "field_note, manual ")
    settings = Settings.load(apply_env=True)
    assert settings.ai.timeout_seconds == pytest.approx(600.0)
    assert isinstance(settings.ai.timeout_seconds, float)
    assert settings.ai.retries == 7 and isinstance(settings.ai.retries, int)
    assert settings.extraction.cache_enabled is False
    assert settings.authority.hierarchy == ["field_note", "manual"]
    assert "env:DRILLINTEL_AI__RETRIES" in settings.applied_overrides


def test_a_malformed_environment_number_is_reported_as_a_configuration_error(monkeypatch) -> None:
    monkeypatch.setenv("DRILLINTEL_AI__RETRIES", "seven")
    with pytest.raises(ConfigurationError, match="not an integer"):
        Settings.load(apply_env=True)


def test_a_non_numeric_value_is_rejected_as_a_type_error(tmp_path) -> None:
    with pytest.raises(ConfigurationError):
        load(tmp_path, '[extraction]\nexcel_max_cells = "lots"\n')
