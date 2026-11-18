"""Local MinerU readiness and synthetic OCR metrics are separate contracts."""

from __future__ import annotations

import json
import sys
import zlib
from io import StringIO
from pathlib import Path

import pytest

from drilling_intelligence.cli.app import main
from drilling_intelligence.integrations.base import IntegrationStatus
from drilling_intelligence.integrations.mineru.client import MinerURun
from drilling_intelligence.integrations.mineru.normalize import MinerURawOutput
from drilling_intelligence.integrations.mineru.qualification import (
    GROUND_TRUTH_TEXT,
    error_metrics,
    levenshtein_distance,
    synthetic_fixture_corpus,
    synthetic_fixture_png,
    validate_local_mineru,
)

ROOT = Path(__file__).resolve().parents[2]


def _capture_cli(*args: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = StringIO(), StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        code = main(list(args))
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    return code, json.loads(stdout.getvalue())


def _ready_status():
    return IntegrationStatus(
        component="MinerU",
        available=True,
        mode="cli",
        version="4.1.7",
        location="/isolated/bin/mineru-kit",
    )


def test_synthetic_png_is_a_deterministic_valid_fixture_with_ground_truth() -> None:
    fixture = ROOT / "tests" / "fixtures" / "mineru_ocr" / "synthetic_scan.png"
    expected = ROOT / "tests" / "fixtures" / "mineru_ocr" / "ground_truth.txt"
    png = synthetic_fixture_png()
    assert png == synthetic_fixture_png() == fixture.read_bytes()
    assert expected.read_text(encoding="utf-8").strip() == GROUND_TRUTH_TEXT
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    chunks = []
    image_header = b""
    image_data = bytearray()
    while offset < len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        crc = int.from_bytes(png[offset + 8 + length : offset + 12 + length], "big")
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == crc
        chunks.append(kind)
        if kind == b"IHDR":
            image_header = data
        elif kind == b"IDAT":
            image_data.extend(data)
        offset += 12 + length
    assert chunks == [b"IHDR", b"IDAT", b"IEND"]
    width = int.from_bytes(image_header[:4], "big")
    height = int.from_bytes(image_header[4:8], "big")
    assert len(zlib.decompress(image_data)) == (width + 1) * height


def test_generated_layout_corpus_matches_checked_in_bytes_ground_truth_and_hashes() -> None:
    fixture_root = ROOT / "tests" / "fixtures" / "mineru_ocr"
    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    corpus = synthetic_fixture_corpus()
    manifest_cases = {item["name"]: item for item in manifest["cases"]}
    assert set(corpus) == set(manifest_cases)
    assert set(corpus) == {
        "single_column",
        "multi_column",
        "table_mixed",
        "small_print",
        "low_quality",
        "rotated_90",
        "skewed",
    }
    for name, case in corpus.items():
        recorded = manifest_cases[name]
        image_path = fixture_root / recorded["image"]
        truth_path = fixture_root / recorded["ground_truth"]
        assert case["png"] == image_path.read_bytes()
        assert case["fixture_sha256"] == recorded["fixture_sha256"]
        assert case["ground_truth_sha256"] == recorded["ground_truth_sha256"]
        assert case["ground_truth"] == truth_path.read_text(encoding="utf-8")
        assert case["layout"] == recorded["layout"]
        assert case["transform"] == recorded["transform"]


def test_cer_and_wer_are_true_character_and_word_edit_rates() -> None:
    assert levenshtein_distance("kitten", "sitting") == 3
    metrics = error_metrics("DRILLING REPORT 27", "DRILLING REPORT 28")
    assert metrics["character_errors"] == 1
    assert metrics["character_error_rate"] == pytest.approx(1 / len("DRILLING REPORT 27"))
    assert metrics["word_errors"] == 1
    assert metrics["word_error_rate"] == pytest.approx(1 / 3)


def test_readiness_is_not_model_or_ocr_qualification(settings, monkeypatch) -> None:
    from drilling_intelligence.integrations.mineru.discovery import MinerUProber

    monkeypatch.setattr(MinerUProber, "status", lambda self: _ready_status())
    monkeypatch.setattr(
        "drilling_intelligence.integrations.mineru.client.http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("HTTP must not be called")),
    )
    payload, exit_code = validate_local_mineru(settings)

    assert exit_code == 0
    assert payload["runtime_readiness"]["status"] == "READY"
    assert payload["configured_model_source"] == "local"
    assert payload["model_readiness"]["status"] == "NOT_CHECKED"
    assert payload["ocr_qualification"]["status"] == "NOT_RUN"
    assert payload["ocr_qualification"]["metrics"] is None
    assert payload["ocr_qualification"]["general_ocr_accuracy_qualified"] is False
    assert "HTTP endpoints are not probed" in payload["transport_scope"]


def test_missing_local_runtime_keeps_model_unchecked_ocr_not_run_and_metrics_null(
    settings, monkeypatch
) -> None:
    from drilling_intelligence.integrations.mineru.client import MinerUClient
    from drilling_intelligence.integrations.mineru.discovery import MinerUProber

    unavailable = IntegrationStatus(
        component="MinerU",
        available=False,
        mode="cli",
        reason="local runtime is not installed",
    )
    monkeypatch.setattr(MinerUProber, "status", lambda self: unavailable)
    monkeypatch.setattr(
        MinerUClient,
        "parse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inference must not be invoked without the runtime")
        ),
    )

    payload, exit_code = validate_local_mineru(settings, run_synthetic_ocr=True)

    assert exit_code == 1
    assert payload["runtime_readiness"]["status"] == "NOT_READY"
    assert payload["model_readiness"]["status"] == "NOT_CHECKED"
    assert payload["ocr_qualification"]["status"] == "NOT_RUN"
    assert payload["ocr_qualification"]["metrics"] is None
    assert "fixtures" not in payload["ocr_qualification"]


def test_explicit_synthetic_inference_uses_local_basic_models_and_records_fixture_only_metrics(
    settings, monkeypatch
) -> None:
    from drilling_intelligence.integrations.mineru.client import MinerUClient
    from drilling_intelligence.integrations.mineru.discovery import MinerUProber

    settings.mineru.mode = "http"
    settings.mineru.endpoint = "https://should-not-be-contacted.invalid"
    monkeypatch.setattr(MinerUProber, "status", lambda self: _ready_status())
    captured: list[dict[str, object]] = []

    def parse(self, source, **kwargs):
        captured.append(
            {
                "mode": self.mode,
                "endpoint": self.endpoint,
                "tier": self.tier,
                "env": dict(self.extra_env),
                "source_name": Path(source).name,
                "selected_mode": kwargs.get("selected_mode"),
            }
        )
        return MinerURun(
            ok=True,
            mode="cli",
            artefacts=MinerURawOutput(markdown=GROUND_TRUTH_TEXT),
            returncode=0,
        )

    monkeypatch.setattr(MinerUClient, "parse", parse)
    payload, exit_code = validate_local_mineru(settings, run_synthetic_ocr=True)

    assert exit_code == 0
    assert len(captured) == len(synthetic_fixture_corpus())
    assert all(item["mode"] == "cli" and item["endpoint"] == "" for item in captured)
    assert all(item["tier"] == "basic" for item in captured)
    assert all(item["selected_mode"] == "cli" for item in captured)
    assert all(item["env"]["MINERU_MODEL_SOURCE"] == "local" for item in captured)
    assert all(item["env"]["MINERU_MODEL_SMALL_BACKEND"] == "onnx" for item in captured)
    assert {item["source_name"] for item in captured} == {
        f"{name}.png" for name in synthetic_fixture_corpus()
    }
    assert payload["runtime_readiness"]["status"] == "READY"
    assert payload["model_readiness"]["status"] == "NOT_CHECKED"
    assert payload["ocr_qualification"]["status"] == "SYNTHETIC_METRICS_RECORDED"
    assert payload["ocr_qualification"]["fixture_pass"] is None
    assert payload["ocr_qualification"]["general_ocr_accuracy_qualified"] is False
    assert "no pass threshold" in payload["ocr_qualification"]["note"]
    assert payload["ocr_qualification"]["metrics"]["single_column"]["character_error_rate"] == 0
    assert payload["ocr_qualification"]["metrics"]["single_column"]["word_error_rate"] == 0
    assert len(payload["ocr_qualification"]["fixtures"]) == 7


def test_inference_failure_does_not_emit_accuracy_metrics(settings, monkeypatch) -> None:
    from drilling_intelligence.integrations.mineru.client import MinerUClient
    from drilling_intelligence.integrations.mineru.discovery import MinerUProber

    monkeypatch.setattr(MinerUProber, "status", lambda self: _ready_status())

    def fail(self, source, **kwargs):
        assert self.extra_env["MINERU_MODEL_SOURCE"] == "local"
        raise RuntimeError("local model unavailable; no implicit download attempted")

    monkeypatch.setattr(MinerUClient, "parse", fail)
    payload, exit_code = validate_local_mineru(settings, run_synthetic_ocr=True)
    assert exit_code == 1
    assert payload["ocr_qualification"]["status"] == "INFERENCE_FAILED"
    assert payload["ocr_qualification"]["metrics"] is None
    assert payload["model_readiness"]["status"] == "NOT_CHECKED"
    assert payload["runtime_readiness"]["status"] == "READY"
    assert payload["ocr_qualification"]["general_ocr_accuracy_qualified"] is False
    assert all(case["metrics"] is None for case in payload["ocr_qualification"]["fixtures"])


def test_operator_readiness_command_does_not_parse_or_probe_services(
    settings, monkeypatch, tmp_path
) -> None:
    from drilling_intelligence.integrations.mineru.client import MinerUClient
    from drilling_intelligence.integrations.mineru.discovery import MinerUProber

    config = tmp_path / "config.toml"
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "https://should-not-be-contacted.invalid"
    config.write_text(settings.to_toml(), encoding="utf-8")
    monkeypatch.setattr(MinerUProber, "status", lambda self: _ready_status())
    monkeypatch.setattr(
        MinerUClient,
        "parse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("default must not parse")),
    )
    monkeypatch.setattr(
        "drilling_intelligence.integrations.mineru.discovery.http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("HTTP must not be called")),
    )

    code, payload = _capture_cli("mineru", "validate", "--config", str(config), "--json")

    assert code == 0
    assert payload["ocr_qualification"]["status"] == "NOT_RUN"
    assert payload["ocr_qualification"]["metrics"] is None
    assert payload["runtime_readiness"]["status"] == "READY"
    assert not (tmp_path / ".drillintel").exists()
