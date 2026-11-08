"""Local-only MinerU readiness and synthetic OCR fixture qualification helpers.

Readiness is a CLI/version check, not model readiness or OCR evidence. Inference
is opt-in, runs only the local MinerU 4 CLI on a generated image, forces the
upstream documented ``local`` model source and Basic tier, and never promotes
synthetic fixture results to a claim about general OCR accuracy.
"""

from __future__ import annotations

import copy
import hashlib
import math
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

from ...config.settings import Settings
from ...extraction.normalized import clean_text
from .cli import is_mineru_kit
from .client import MinerUClient
from .discovery import MinerUProber
from .normalize import (
    normalize_content_list,
    normalize_markdown,
    normalize_middle_json,
)

GROUND_TRUTH_TEXT = "DRILLING REPORT 27\nDEPTH 12345 FT"
FIXTURE_SCOPE = "one deterministic synthetic raster image; not representative field OCR accuracy"
MAX_CHARACTER_ERROR_RATE = 0.10
MAX_WORD_ERROR_RATE = 0.20

_FONT: dict[str, tuple[str, ...]] = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01111", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "11110"),
}


def synthetic_fixture_png() -> bytes:
    """Build a byte-for-byte deterministic, two-line grayscale PNG fixture."""
    scale = 8
    margin = 24
    line_gap = 18
    lines = GROUND_TRUTH_TEXT.splitlines()
    width = max(len(line) for line in lines) * 6 * scale + 2 * margin
    height = len(lines) * 7 * scale + (len(lines) - 1) * line_gap + 2 * margin
    pixels = bytearray([255]) * (width * height)
    for line_number, line in enumerate(lines):
        origin_y = margin + line_number * (7 * scale + line_gap)
        origin_x = margin
        for character in line:
            glyph = _FONT[character]
            for row, pattern in enumerate(glyph):
                for column, bit in enumerate(pattern):
                    if bit == "1":
                        x0 = origin_x + column * scale
                        y0 = origin_y + row * scale
                        for y in range(y0, y0 + scale):
                            start = y * width + x0
                            pixels[start : start + scale] = b"\x10" * scale
            origin_x += 6 * scale

    scanlines = b"".join(
        b"\x00" + bytes(pixels[row * width : (row + 1) * width]) for row in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return (
        struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def levenshtein_distance(left: str, right: str) -> int:
    """Character-level Levenshtein distance with O(min(n,m)) memory."""
    return _sequence_distance(list(left), list(right))


def _sequence_distance(left: list[str], right: list[str]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, left_item in enumerate(left, start=1):
        current = [row]
        for column, right_item in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1]


def error_metrics(expected: str, actual: str) -> dict[str, Any]:
    """Return reproducible CER/WER metrics after whitespace/case normalization."""
    reference = " ".join(clean_text(expected).upper().split())
    recognized = " ".join(clean_text(actual).upper().split())
    reference_words = reference.split()
    recognized_words = recognized.split()
    char_errors = levenshtein_distance(reference, recognized)
    word_errors = _sequence_distance(reference_words, recognized_words)
    return {
        "normalization": "Unicode text, uppercase, collapse all whitespace",
        "character_errors": char_errors,
        "reference_characters": len(reference),
        "character_error_rate": char_errors / max(len(reference), 1),
        "word_errors": word_errors,
        "reference_words": len(reference_words),
        "word_error_rate": word_errors / max(len(reference_words), 1),
        "recognized_text": actual,
    }


def validate_local_mineru(
    settings: Settings, *, run_synthetic_ocr: bool = False
) -> tuple[dict[str, Any], int]:
    """Report local CLI readiness; optionally measure one local synthetic OCR fixture."""
    configured_mode = settings.mineru.mode
    local_settings = copy.copy(settings)
    local_settings.mineru = copy.copy(settings.mineru)
    local_settings.mineru.mode = "cli"
    local_settings.mineru.endpoint = ""
    status = MinerUProber(local_settings).status()
    ready = status.available and status.mode == "cli"
    result: dict[str, Any] = {
        "ok": bool(ready),
        "configured_mode": configured_mode,
        "configured_model_source": settings.mineru.model_source,
        "transport_scope": "local CLI only; HTTP endpoints are not probed",
        "model_readiness": {
            "status": "NOT_CHECKED",
            "detail": "no model files are inspected by the readiness check",
        },
        "runtime_readiness": {
            "status": "READY" if ready else "NOT_READY",
            "available": bool(ready),
            "mode": status.mode,
            "version": status.version,
            "binary": status.location,
            "reason": status.reason,
            "models_checked": False,
            "configured_model_source": settings.mineru.model_source,
        },
        "ocr_qualification": {
            "status": "NOT_RUN",
            "scope": FIXTURE_SCOPE,
            "inference_completed": False,
            "metrics": None,
            "fixture_pass": None,
            "general_ocr_accuracy_qualified": False,
            "note": "CLI readiness is not an OCR accuracy result; inference is opt-in.",
        },
    }
    if not run_synthetic_ocr:
        return result, 0 if ready else 1
    if not ready:
        result["ok"] = False
        result["ocr_qualification"]["status"] = "NOT_RUN"
        result["ocr_qualification"]["note"] = (
            "Local CLI is unavailable; inference and metrics were not run."
        )
        return result, 1
    if not is_mineru_kit(status.location):
        result["ok"] = False
        result["ocr_qualification"]["status"] = "NOT_RUN"
        result["ocr_qualification"]["note"] = (
            "Synthetic OCR validation requires the supported MinerU 4 mineru-kit CLI; "
            "the detected legacy CLI was not invoked."
        )
        return result, 1

    local_settings.mineru.binary = status.location
    local_settings.mineru.tier = "basic"
    local_settings.mineru.model_source = "local"
    local_settings.mineru.env = {
        **dict(local_settings.mineru.env or {}),
        "MINERU_MODEL_SOURCE": "local",
        "MINERU_MODEL_SMALL_BACKEND": "onnx",
    }
    fixture_bytes = synthetic_fixture_png()
    sha256 = hashlib.sha256(fixture_bytes).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="drillintel-mineru-qualification-") as directory:
            fixture = Path(directory) / "synthetic_scan.png"
            fixture.write_bytes(fixture_bytes)
            run = MinerUClient(local_settings).parse(fixture, selected_mode="cli")
            try:
                if not run.ok or run.returncode != 0 or run.artefacts is None:
                    result["ok"] = False
                    result["ocr_qualification"]["status"] = "INFERENCE_FAILED"
                    result["ocr_qualification"]["note"] = (
                        run.error
                        or "Local MinerU did not complete a clean inference; metrics were not computed."
                    )
                    result["ocr_qualification"]["returncode"] = run.returncode
                    result["ocr_qualification"]["timed_out"] = run.timed_out
                    result["ocr_qualification"]["metrics"] = None
                    result["ocr_qualification"]["scratch_cleanup"] = "pending"
                    result["model_readiness"] = {"status": "LOCAL_INFERENCE_FAILED"}
                    return result, 1
                artefacts = run.artefacts
                kwargs = {
                    "filename": fixture.name,
                    "document_id": "synthetic-ocr-fixture",
                    "version_id": "synthetic-ocr-fixture-v1",
                    "sha256": sha256,
                }
                if artefacts.middle:
                    document, _diagnostics = normalize_middle_json(artefacts.middle, **kwargs)
                elif artefacts.content_list:
                    document, _diagnostics = normalize_content_list(
                        artefacts.content_list, **kwargs
                    )
                else:
                    document, _diagnostics = normalize_markdown(artefacts.markdown, **kwargs)
                recognized = document.text or artefacts.markdown
                if not clean_text(recognized):
                    result["ok"] = False
                    result["ocr_qualification"]["status"] = "INFERENCE_FAILED"
                    result["ocr_qualification"]["note"] = (
                        "Inference completed without readable text; no metrics computed."
                    )
                    result["ocr_qualification"]["metrics"] = None
                    result["model_readiness"] = {"status": "LOCAL_INFERENCE_COMPLETED"}
                    return result, 1
                metrics = error_metrics(GROUND_TRUTH_TEXT, recognized)
                fixture_pass = (
                    math.isfinite(metrics["character_error_rate"])
                    and math.isfinite(metrics["word_error_rate"])
                    and metrics["character_error_rate"] <= MAX_CHARACTER_ERROR_RATE
                    and metrics["word_error_rate"] <= MAX_WORD_ERROR_RATE
                )
                result["model_readiness"] = {"status": "LOCAL_INFERENCE_COMPLETED"}
                result["ocr_qualification"] = {
                    "status": "SYNTHETIC_METRICS_RECORDED",
                    "scope": FIXTURE_SCOPE,
                    "inference_completed": True,
                    "fixture_sha256": sha256,
                    "expected_text": GROUND_TRUTH_TEXT,
                    "metrics": metrics,
                    "thresholds": {
                        "max_character_error_rate": MAX_CHARACTER_ERROR_RATE,
                        "max_word_error_rate": MAX_WORD_ERROR_RATE,
                    },
                    "fixture_pass": fixture_pass,
                    "general_ocr_accuracy_qualified": False,
                    "model_source": "local",
                    "tier": "basic",
                    "note": "A synthetic fixture result is not general or field-document OCR accuracy qualification.",
                }
                result["ok"] = bool(fixture_pass)
                had_scratch = run.output_dir is not None
                cleanup_ok = run.cleanup()
                result["ocr_qualification"]["scratch_cleanup"] = (
                    "removed"
                    if had_scratch and cleanup_ok
                    else "not_applicable"
                    if cleanup_ok
                    else "failed"
                )
                if not cleanup_ok:
                    result["ok"] = False
                    result["ocr_qualification"]["status"] = "METRICS_RECORDED_CLEANUP_FAILED"
                return result, 0 if result["ok"] else 1
            finally:
                if run.output_dir is not None:
                    cleanup_ok = run.cleanup()
                    result["ocr_qualification"]["scratch_cleanup"] = (
                        "removed" if cleanup_ok else "failed"
                    )
                    if not cleanup_ok:
                        result["ok"] = False
                        result["ocr_qualification"]["status"] = "METRICS_RECORDED_CLEANUP_FAILED"
                        result["model_readiness"] = {"status": "LOCAL_INFERENCE_COMPLETED"}
    except Exception as exc:  # noqa: BLE001 - external parser/runtime boundary
        result["ok"] = False
        result["ocr_qualification"]["status"] = "INFERENCE_FAILED"
        result["ocr_qualification"]["note"] = f"{type(exc).__name__}: {exc}"
        result["ocr_qualification"]["metrics"] = None
        result["model_readiness"] = {"status": "LOCAL_INFERENCE_FAILED"}
        return result, 1


__all__ = [
    "FIXTURE_SCOPE",
    "GROUND_TRUTH_TEXT",
    "MAX_CHARACTER_ERROR_RATE",
    "MAX_WORD_ERROR_RATE",
    "error_metrics",
    "levenshtein_distance",
    "synthetic_fixture_png",
    "validate_local_mineru",
]
