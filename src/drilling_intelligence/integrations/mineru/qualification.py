"""Local-only MinerU readiness and synthetic OCR fixture qualification helpers.

Readiness is a CLI/version check, not model readiness or OCR evidence. Inference
is opt-in, runs only the local MinerU 4 CLI on a generated image, forces the
upstream documented ``local`` model source and Basic tier, and never promotes
synthetic fixture results to a claim about general OCR accuracy.
"""

from __future__ import annotations

import copy
import hashlib
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

from ...config.settings import Settings
from ...core.redact import safe_detail
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
FIXTURE_SCOPE = (
    "deterministic generated synthetic raster corpus; not representative field OCR accuracy"
)

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
        + _png_chunk(b"IDAT", _stored_zlib(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _stored_zlib(data: bytes) -> bytes:
    """Return a deterministic zlib stream using RFC 1951 uncompressed blocks.

    ``zlib.compress`` is valid but may emit different DEFLATE bytes across zlib
    versions/platforms. Stored blocks keep this checked-in synthetic fixture
    byte-identical on Linux and Windows without adding image dependencies.
    """
    output = bytearray(b"\x78\x01")  # zlib header: DEFLATE, fastest/no compression
    block_size = 65_535
    for offset in range(0, len(data), block_size):
        block = data[offset : offset + block_size]
        is_final = offset + len(block) == len(data)
        output.append(1 if is_final else 0)  # BFINAL, BTYPE=00, byte-aligned padding
        output.extend(struct.pack("<HH", len(block), len(block) ^ 0xFFFF))
        output.extend(block)
    if not data:
        output.extend(b"\x01\x00\x00\xff\xff")
    output.extend(struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF))
    return bytes(output)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return (
        struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    )


def synthetic_fixture_corpus() -> dict[str, dict[str, Any]]:
    """Return deterministic, generated-only fixtures and their exact ground truth.

    The layouts are controlled test inputs, not representative field documents. Rotation and skew
    are exact integer raster transforms so fixture hashes are stable across supported platforms.
    """
    specifications: dict[str, dict[str, Any]] = {
        "single_column": {
            "columns": [["DRILLING REPORT 27", "DEPTH 12345 FT"]],
            "scale": 8,
            "background": 255,
            "ink": 16,
            "ground_truth": GROUND_TRUTH_TEXT,
            "layout": "single_column",
            "transform": "none",
        },
        "multi_column": {
            "columns": [["WELL 17", "DEPTH 12345 FT"], ["SHIFT DAY", "PUMP CHECK"]],
            "scale": 6,
            "background": 255,
            "ink": 16,
            "ground_truth": "WELL 17\nDEPTH 12345 FT\nSHIFT DAY\nPUMP CHECK",
            "layout": "two_columns_read_left_then_right",
            "transform": "none",
        },
        "table_mixed": {
            "columns": [
                [
                    "DAILY OPERATIONS",
                    "ACTIVITY      DEPTH",
                    "DRILLING      12345 FT",
                    "CEMENTING     12300 FT",
                ]
            ],
            "scale": 5,
            "background": 255,
            "ink": 16,
            "ground_truth": "DAILY OPERATIONS\nACTIVITY      DEPTH\nDRILLING      12345 FT\nCEMENTING     12300 FT",
            "layout": "heading_with_aligned_table_like_rows",
            "transform": "none",
        },
        "small_print": {
            "columns": [["SHIFT REPORT 27", "MUD CHECK 125"]],
            "scale": 3,
            "background": 255,
            "ink": 16,
            "ground_truth": "SHIFT REPORT 27\nMUD CHECK 125",
            "layout": "single_column_small_glyphs",
            "transform": "none",
        },
        "low_quality": {
            "columns": [["DRILLING REPORT 27", "DEPTH 12345 FT"]],
            "scale": 4,
            "background": 232,
            "ink": 168,
            "ground_truth": GROUND_TRUTH_TEXT,
            "layout": "single_column_low_contrast_with_deterministic_sparse_noise",
            "transform": "deterministic_sparse_noise",
        },
        "rotated_90": {
            "columns": [["ROTATED PAGE 90", "DEPTH 12345 FT"]],
            "scale": 5,
            "background": 255,
            "ink": 16,
            "ground_truth": "ROTATED PAGE 90\nDEPTH 12345 FT",
            "layout": "single_column",
            "transform": "90_degrees_clockwise",
        },
        "skewed": {
            "columns": [["SKEWED PAGE 02", "DEPTH 12345 FT"]],
            "scale": 5,
            "background": 255,
            "ink": 16,
            "ground_truth": "SKEWED PAGE 02\nDEPTH 12345 FT",
            "layout": "single_column",
            "transform": "integer_row_shear_one_pixel_per_twenty_rows",
        },
    }
    result: dict[str, dict[str, Any]] = {}
    for name, specification in specifications.items():
        if name == "single_column":
            image = synthetic_fixture_png()
        else:
            pixels, width, height = _render_columns(
                specification["columns"],
                scale=int(specification["scale"]),
                background=int(specification["background"]),
                ink=int(specification["ink"]),
                sparse_noise=name == "low_quality",
            )
            if name == "rotated_90":
                pixels, width, height = _rotate_clockwise(pixels, width, height)
            elif name == "skewed":
                pixels, width = _shear_rows(pixels, width, height)
            image = _encode_gray_png(pixels, width, height)
        ground_truth = str(specification["ground_truth"])
        result[name] = {
            "name": name,
            "png": image,
            "fixture_sha256": hashlib.sha256(image).hexdigest(),
            "ground_truth": ground_truth,
            "ground_truth_sha256": hashlib.sha256(ground_truth.encode("utf-8")).hexdigest(),
            "layout": specification["layout"],
            "transform": specification["transform"],
            "scale": specification["scale"],
        }
    return result


def _render_columns(
    columns: list[list[str]], *, scale: int, background: int, ink: int, sparse_noise: bool
) -> tuple[bytearray, int, int]:
    margin = 16
    line_gap = max(3, scale * 2)
    column_gap = scale * 5
    column_widths = [
        max((len(line) for line in column), default=0) * 6 * scale for column in columns
    ]
    width = max(1, 2 * margin + sum(column_widths) + max(0, len(columns) - 1) * column_gap)
    row_count = max((len(column) for column in columns), default=0)
    height = max(1, 2 * margin + row_count * 7 * scale + max(0, row_count - 1) * line_gap)
    pixels = bytearray([background]) * (width * height)
    x_origins: list[int] = []
    origin = margin
    for column_width in column_widths:
        x_origins.append(origin)
        origin += column_width + column_gap
    for column_index, column_lines in enumerate(columns):
        for row_index, line in enumerate(column_lines):
            y_origin = margin + row_index * (7 * scale + line_gap)
            x_origin = x_origins[column_index]
            for character in line:
                glyph = _FONT.get(character)
                if glyph is None:
                    raise ValueError(f"synthetic fixture character {character!r} has no glyph")
                for glyph_row, pattern in enumerate(glyph):
                    for glyph_column, bit in enumerate(pattern):
                        if bit == "1":
                            x0 = x_origin + glyph_column * scale
                            y0 = y_origin + glyph_row * scale
                            for y in range(y0, y0 + scale):
                                start = y * width + x0
                                pixels[start : start + scale] = bytes([ink]) * scale
                x_origin += 6 * scale
    if sparse_noise:
        for y in range(height):
            for x in range(width):
                offset = y * width + x
                if pixels[offset] == background and (x * 17 + y * 31) % 211 == 0:
                    pixels[offset] = background - 12
    return pixels, width, height


def _rotate_clockwise(pixels: bytearray, width: int, height: int) -> tuple[bytearray, int, int]:
    rotated_width, rotated_height = height, width
    rotated = bytearray([255]) * (rotated_width * rotated_height)
    for y in range(height):
        for x in range(width):
            rotated[x * rotated_width + (height - 1 - y)] = pixels[y * width + x]
    return rotated, rotated_width, rotated_height


def _shear_rows(pixels: bytearray, width: int, height: int) -> tuple[bytearray, int]:
    padding = height // 20 + 1
    sheared_width = width + padding
    sheared = bytearray([255]) * (sheared_width * height)
    for y in range(height):
        shift = y // 20
        source = y * width
        destination = y * sheared_width + shift
        sheared[destination : destination + width] = pixels[source : source + width]
    return sheared, sheared_width


def _encode_gray_png(pixels: bytearray, width: int, height: int) -> bytes:
    scanlines = b"".join(
        b"\x00" + bytes(pixels[row * width : (row + 1) * width]) for row in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", _stored_zlib(scanlines))
        + _png_chunk(b"IEND", b"")
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
    """Report local readiness; optionally measure the generated corpus with local MinerU only."""
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
            "note": "Readiness is not an OCR result; inference is opt-in and no metric threshold is configured.",
        },
    }
    if not run_synthetic_ocr:
        return result, 0 if ready else 1
    if not ready:
        result["ok"] = False
        result["ocr_qualification"]["note"] = (
            "Local CLI is unavailable; model files were not checked, inference was not run, and metrics are null."
        )
        return result, 1
    if not is_mineru_kit(status.location):
        result["ok"] = False
        result["ocr_qualification"]["note"] = (
            "Synthetic OCR requires the supported MinerU 4 mineru-kit CLI; the detected legacy CLI was not invoked."
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
    case_results: list[dict[str, Any]] = []
    overall_cleanup_ok = True
    for name, case in synthetic_fixture_corpus().items():
        case_result: dict[str, Any] = {
            "name": name,
            "fixture_sha256": case["fixture_sha256"],
            "ground_truth_sha256": case["ground_truth_sha256"],
            "ground_truth": case["ground_truth"],
            "layout": case["layout"],
            "transform": case["transform"],
            "status": "NOT_RUN",
            "inference_completed": False,
            "metrics": None,
        }
        run = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="drillintel-mineru-qualification-"
            ) as directory:
                fixture = Path(directory) / f"{name}.png"
                fixture.write_bytes(case["png"])
                try:
                    run = MinerUClient(local_settings).parse(fixture, selected_mode="cli")
                    if not run.ok or run.returncode != 0 or run.artefacts is None:
                        case_result["status"] = "INFERENCE_FAILED"
                        case_result["returncode"] = run.returncode
                        case_result["timed_out"] = run.timed_out
                        case_result["detail"] = safe_detail(
                            run.error or "Local inference produced no clean output."
                        )
                    else:
                        artefacts = run.artefacts
                        kwargs = {
                            "filename": fixture.name,
                            "document_id": f"synthetic-ocr-{name}",
                            "version_id": f"synthetic-ocr-{name}-v1",
                            "sha256": case["fixture_sha256"],
                        }
                        if artefacts.middle:
                            document, _diagnostics = normalize_middle_json(
                                artefacts.middle, **kwargs
                            )
                        elif artefacts.content_list:
                            document, _diagnostics = normalize_content_list(
                                artefacts.content_list, **kwargs
                            )
                        else:
                            document, _diagnostics = normalize_markdown(
                                artefacts.markdown, **kwargs
                            )
                        recognized = document.text or artefacts.markdown
                        if clean_text(recognized):
                            case_result["status"] = "METRICS_RECORDED"
                            case_result["inference_completed"] = True
                            case_result["metrics"] = error_metrics(case["ground_truth"], recognized)
                        else:
                            case_result["status"] = "INFERENCE_FAILED"
                            case_result["inference_completed"] = True
                            case_result["detail"] = (
                                "Inference completed without readable text; metrics are null."
                            )
                except Exception as exc:  # noqa: BLE001 - optional external parser boundary
                    fixture_text = str(fixture)
                    detail = str(exc).replace(fixture_text, "<synthetic-fixture>")
                    detail = detail.replace(fixture.as_posix(), "<synthetic-fixture>")
                    case_result["status"] = "INFERENCE_FAILED"
                    case_result["detail"] = safe_detail(f"{type(exc).__name__}: {detail[:500]}")
                finally:
                    if run is not None:
                        had_scratch = run.output_dir is not None
                        cleanup_ok = run.cleanup()
                        overall_cleanup_ok = overall_cleanup_ok and cleanup_ok
                        case_result["scratch_cleanup"] = (
                            "removed"
                            if had_scratch and cleanup_ok
                            else "not_applicable"
                            if cleanup_ok
                            else "failed"
                        )
        except Exception as exc:  # noqa: BLE001 - local temp-file boundary
            case_result["status"] = "INFERENCE_FAILED"
            case_result["detail"] = safe_detail(f"{type(exc).__name__}: {str(exc)[:500]}")
        case_results.append(case_result)

    all_completed = all(case["status"] == "METRICS_RECORDED" for case in case_results)
    result["ok"] = bool(all_completed and overall_cleanup_ok)
    result["ocr_qualification"] = {
        "status": (
            "SYNTHETIC_METRICS_RECORDED"
            if all_completed and overall_cleanup_ok
            else "METRICS_RECORDED_CLEANUP_FAILED"
            if all_completed
            else "INFERENCE_FAILED"
        ),
        "scope": FIXTURE_SCOPE,
        "inference_completed": all_completed,
        "fixture_pass": None,
        "fixtures": case_results,
        "metrics": (
            {case["name"]: case["metrics"] for case in case_results}
            if all(case["metrics"] is not None for case in case_results)
            else None
        ),
        "general_ocr_accuracy_qualified": False,
        "model_source": "local",
        "tier": "basic",
        "scratch_cleanup": "removed" if overall_cleanup_ok else "failed",
        "note": "CER/WER are descriptive per generated fixture; no pass threshold or field-accuracy claim is configured.",
    }
    return result, 0 if result["ok"] else 1


__all__ = [
    "FIXTURE_SCOPE",
    "GROUND_TRUTH_TEXT",
    "error_metrics",
    "levenshtein_distance",
    "synthetic_fixture_corpus",
    "synthetic_fixture_png",
    "validate_local_mineru",
]
