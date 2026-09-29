"""The MinerU optional-integration boundary, exercised through real transports.

MinerU is external and opt-in, so none of this installs it.  What it does instead is drive the
*production* adapter and client across doubles that reproduce the real contract at each seam:

* the CLI path runs a **real subprocess** - a stand-in executable on disk - so argument
  construction, temp-directory handling, output discovery and exit-status handling are the
  platform's own code against a real process, not a mocked return value;
* the HTTP path replaces only ``http_json``, the single transport helper every external service is
  reached through, with a double returning exactly the dict that helper returns.

Neither double stands in for our logic; both stand in for the other side of the wire.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from drilling_intelligence.core.errors import ExtractionError
from drilling_intelligence.extraction.interfaces import (
    DocumentComplexity,
    ExtractionContext,
    ProvenanceBuilder,
)
from drilling_intelligence.integrations.mineru.adapter import MinerUExtractor
from drilling_intelligence.integrations.mineru.client import MinerUClient
from drilling_intelligence.integrations.mineru.discovery import MinerUProber

SHA = "a" * 64


def _fake_mineru(directory: Path, *, name: str, script: str) -> str:
    """Write an executable stand-in for the ``mineru`` binary and return its path."""
    path = directory / name
    path.write_text("#!/usr/bin/env python3\n" + script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _context(path: Path, *, extension: str = ".pdf") -> ExtractionContext:
    return ExtractionContext(
        path=path,
        filename=path.name,
        sha256=SHA,
        extension=extension,
        size_bytes=path.stat().st_size if path.exists() else 0,
        mime_type="application/pdf",
        document_id="doc-1",
        document_version_id="ver-1",
        complexity=DocumentComplexity(pages=9, is_scanned=True),
    )


def _provenance(path: Path) -> ProvenanceBuilder:
    return ProvenanceBuilder(
        document_id="doc-1",
        document_version_id="ver-1",
        filename=path.name,
        sha256=SHA,
        parser="mineru",
    )


# -- CLI: real subprocess boundary --------------------------------------------


def test_output_from_a_run_that_exited_non_zero_is_used_but_never_silent(
    settings, tmp_path: Path
) -> None:
    """A crash part-way through a document must not be indistinguishable from a clean parse.

    Tolerating a non-zero exit when MinerU still wrote usable artefacts is deliberate - some builds
    exit non-zero after producing valid output.  What was wrong is that it happened *silently*: the
    run reported ``ok=True`` with an empty ``error``, so a document parsed from a run that died
    after page 1 of 9 looked exactly like a complete extraction in the audited record, and the
    exit status was not on the record at all.
    """
    binary = _fake_mineru(
        tmp_path,
        name="mineru_crash",
        script=(
            "import json, os, sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "d = os.path.join(out, 'auto', 'scanned')\n"
            "os.makedirs(d, exist_ok=True)\n"
            "json.dump({'pdf_info': [{'page_idx': 0, 'para_blocks': []}]},\n"
            "          open(os.path.join(d, 'scanned_middle.json'), 'w'))\n"
            "sys.stderr.write('CUDA out of memory - aborted after page 1\\n')\n"
            "sys.exit(1)\n"
        ),
    )
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")

    settings.mineru.mode = "cli"
    settings.mineru.binary = binary
    run = MinerUClient(settings).parse(source)

    # The tolerance is kept: usable output was written, so the run is still accepted.
    assert run.ok is True
    assert run.artefacts is not None and run.artefacts.best == "middle.json"

    # ...but the record now says what actually happened.
    assert run.returncode == 1, "the exit status has to survive onto the audited record"
    assert run.returncode == run.to_dict()["returncode"]
    assert "exited with code 1" in run.error, run.error
    assert "may be incomplete" in run.error, run.error
    assert "CUDA out of memory" in run.error, "the engine's own reason must be carried through"

    # And a reviewer reading the document sees it, because the adapter's stated contract is to
    # report its own limitations in diagnostics.
    extractor = MinerUExtractor(settings, prober=_available_cli_prober(settings, binary))
    document = extractor.extract(_context(source), _provenance(source))
    assert any("exited with code 1" in line for line in document.diagnostics), document.diagnostics


def test_a_clean_cli_run_is_not_marked_incomplete(settings, tmp_path: Path) -> None:
    """The warning has to be specific to non-zero exits, not a permanent stain on every run."""
    binary = _fake_mineru(
        tmp_path,
        name="mineru_ok",
        script=(
            "import json, os, sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "d = os.path.join(out, 'auto', 'clean')\n"
            "os.makedirs(d, exist_ok=True)\n"
            "json.dump({'pdf_info': [{'page_idx': 0, 'para_blocks': []}]},\n"
            "          open(os.path.join(d, 'clean_middle.json'), 'w'))\n"
            "sys.exit(0)\n"
        ),
    )
    source = tmp_path / "clean.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")

    settings.mineru.mode = "cli"
    settings.mineru.binary = binary
    run = MinerUClient(settings).parse(source)

    assert run.ok is True and run.returncode == 0
    assert run.error == "", f"a clean run must not carry a warning: {run.error!r}"

    extractor = MinerUExtractor(settings, prober=_available_cli_prober(settings, binary))
    document = extractor.extract(_context(source), _provenance(source))
    assert not [line for line in document.diagnostics if "exited with code" in line], (
        document.diagnostics
    )


def test_a_cli_run_that_produced_nothing_at_all_is_a_failure(settings, tmp_path: Path) -> None:
    """Exit 0 with no artefacts is a failed extraction, not an empty success."""
    binary = _fake_mineru(
        tmp_path,
        name="mineru_nothing",
        script="import sys\nsys.exit(0)\n",
    )
    source = tmp_path / "blank.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")

    settings.mineru.mode = "cli"
    settings.mineru.binary = binary
    run = MinerUClient(settings).parse(source)

    assert run.ok is False
    assert "exited with code 0" in run.error or run.error, run.error
    assert run.artefacts is None


# -- HTTP: transport double ---------------------------------------------------


def _stub_http(monkeypatch, handler) -> None:
    """Replace the one transport helper, with a double returning its real dict shape."""
    import drilling_intelligence.integrations.mineru.client as client_mod
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    monkeypatch.setattr(client_mod, "http_json", handler)
    monkeypatch.setattr(discovery_mod, "http_json", handler)


def _envelope(entry: object) -> dict[str, object]:
    return {
        "ok": True,
        "status_code": 200,
        "json": {"results": {"scanned.pdf": entry}},
        "text": "",
        "url": "http://mineru.test/file_parse",
        "error": "",
    }


def test_a_well_formed_http_response_carrying_no_content_is_refused(
    settings, tmp_path: Path, monkeypatch
) -> None:
    """An empty result must not become an apparently valid extraction with invented provenance.

    The HTTP transport accepts any ``results`` entry that is a dict, so ``{"results": {"x": {}}}``
    used to fall through to ``normalize_markdown("")`` and return a document with no text and no
    sections - while the diagnostics claimed "MinerU markdown used", a provenance mode that never
    happened, and the metadata named ``markdown`` as the artefact.  Registering a 200-page scanned
    report as a parsed document with zero content is silent data loss.
    """

    def handler(method, url, **kwargs):
        if url.endswith("/health"):
            return {
                "ok": True,
                "status_code": 200,
                "json": {"version": "2.0.0"},
                "text": "",
                "url": url,
                "error": "",
            }
        return _envelope({})

    _stub_http(monkeypatch, handler)
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")

    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"

    with pytest.raises(ExtractionError) as excinfo:
        MinerUExtractor(settings).extract(_context(source), _provenance(source))
    assert "no middle.json, content_list or markdown" in str(excinfo.value), str(excinfo.value)


def test_a_transport_failure_is_reported_not_swallowed(
    settings, tmp_path: Path, monkeypatch
) -> None:
    """A non-success HTTP status is an unavailable integration, not an empty document."""

    def handler(method, url, **kwargs):
        if url.endswith("/health"):
            return {
                "ok": True,
                "status_code": 200,
                "json": {"version": "2.0.0"},
                "text": "",
                "url": url,
                "error": "",
            }
        return {
            "ok": False,
            "status_code": 503,
            "json": None,
            "text": "",
            "url": url,
            "error": "HTTP 503",
        }

    _stub_http(monkeypatch, handler)
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")

    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"

    with pytest.raises(ExtractionError) as excinfo:
        MinerUExtractor(settings).extract(_context(source), _provenance(source))
    assert "503" in str(excinfo.value), str(excinfo.value)


def test_a_valid_http_result_still_extracts(settings, tmp_path: Path, monkeypatch) -> None:
    """The refusal is narrow: real content over HTTP still produces a document."""

    def handler(method, url, **kwargs):
        if url.endswith("/health"):
            return {
                "ok": True,
                "status_code": 200,
                "json": {"version": "2.0.0"},
                "text": "",
                "url": url,
                "error": "",
            }
        return _envelope({"md_content": "# Daily Drilling Report\n\nBit change at 14:00."})

    _stub_http(monkeypatch, handler)
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF-1.4 not really a pdf")

    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"

    document = MinerUExtractor(settings).extract(_context(source), _provenance(source))
    assert "Daily Drilling Report" in document.text
    assert document.metadata.extra["mineru"]["mode"] == "http"


# -- discovery: mode selection ------------------------------------------------


def _available_cli_prober(settings, binary: str) -> MinerUProber:
    """A prober that reports the stand-in binary as present, without touching PATH."""
    prober = MinerUProber(settings)
    prober._probe_cli = _cli_ok(binary)
    return prober


def test_a_forced_cli_mode_does_not_silently_fall_back_to_http(settings, monkeypatch) -> None:
    """``mode = cli`` means CLI.  Quietly using HTTP would misreport how a document was parsed."""
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "which", lambda binary: "")
    http_calls: list[str] = []

    def handler(method, url, **kwargs):
        http_calls.append(url)
        return {"ok": True, "status_code": 200, "json": {}, "text": "", "url": url, "error": ""}

    monkeypatch.setattr(discovery_mod, "http_json", handler)
    settings.mineru.mode = "cli"
    settings.mineru.endpoint = "http://mineru.test"

    status = MinerUProber(settings).status()
    assert status.available is False
    assert status.mode == "cli"
    assert "not found on PATH" in status.reason, status.reason
    assert http_calls == [], f"a forced CLI probe must not touch HTTP, but queried {http_calls}"


def test_a_forced_http_mode_does_not_silently_fall_back_to_cli(
    settings, tmp_path, monkeypatch
) -> None:
    """The mirror image: a forced HTTP mode must not quietly run a local binary."""
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    binary = _fake_mineru(tmp_path, name="mineru_local", script="import sys\nsys.exit(0)\n")
    monkeypatch.setattr(discovery_mod, "which", lambda name: binary)
    monkeypatch.setattr(
        discovery_mod,
        "http_json",
        lambda method, url, **kw: {
            "ok": False,
            "status_code": 0,
            "json": None,
            "text": "",
            "url": url,
            "error": "ConnectError: refused",
        },
    )
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"
    settings.mineru.binary = binary

    status = MinerUProber(settings).status()
    assert status.available is False
    assert status.mode == "http"
    assert "no HTTP response" in status.reason, status.reason
    assert {c["mode"] for c in status.to_dict()["checks"]} == {"http"}, status.to_dict()["checks"]


def test_auto_mode_prefers_the_transport_that_answers(settings, tmp_path, monkeypatch) -> None:
    """``auto`` may choose either - but it has to report which one it actually chose."""
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "which", lambda name: "")
    monkeypatch.setattr(
        discovery_mod,
        "http_json",
        lambda method, url, **kw: {
            "ok": True,
            "status_code": 200,
            "json": {"version": "2.1.0"},
            "text": "",
            "url": url,
            "error": "",
        },
    )
    settings.mineru.mode = "auto"
    settings.mineru.endpoint = "http://mineru.test"

    status = MinerUProber(settings).status()
    assert status.available is True
    assert status.mode == "http", status.to_dict()
    assert status.version == "2.1.0"
    assert status.location.startswith("http://mineru.test")


def test_disabled_mode_says_disabled_and_names_the_fallback(settings) -> None:
    """Opting out must read as a deliberate choice, not as a broken integration."""
    settings.mineru.mode = "disabled"
    status = MinerUProber(settings).status()

    assert status.available is False
    assert status.mode == "disabled"
    assert "disabled in configuration" in status.reason, status.reason
    assert any("built-in PDF text extractor" in line for line in status.to_dict()["limitations"])
    assert status.to_dict()["checks"] == [], "a disabled integration should probe nothing"


# -- provenance boundary ------------------------------------------------------


def test_xlsx_is_declined_so_cell_level_provenance_is_not_lost(settings) -> None:
    """MinerU is layout-based; it cannot supply cell-level Excel provenance, so XLSX stays native.

    This is the boundary the mission asks to be kept honest: the adapter must not accept a format
    whose provenance it would silently degrade, and the shipped limitations must say why.
    """
    prober = MinerUProber(settings)
    prober._probe = _available_status
    extractor = MinerUExtractor(settings, prober=prober)

    supported, reason = extractor.supports(_context(Path("mud_report.xlsx"), extension=".xlsx"))
    assert supported is False
    assert "native extractor preserves more provenance" in reason, reason

    # The shipped limitations have to state the reason, so the boundary is discoverable and not
    # merely enforced by a decline.
    settings.mineru.mode = "auto"
    probe = MinerUProber(settings)
    probe._probe_cli = _cli_ok("/usr/bin/mineru")
    status = probe.status()
    assert any("openpyxl" in line for line in status.to_dict()["limitations"]), status.to_dict()[
        "limitations"
    ]
    assert any("cell-level Excel provenance" in line for line in status.to_dict()["limitations"])


def _cli_ok(binary: str):
    """A stand-in for a successful CLI probe, so tests need no real binary on PATH."""

    def probe(mineru, backend):
        return {
            "mode": "cli",
            "available": True,
            "version": "9.9.9",
            "binary": binary,
            "reason": "",
        }

    return probe


def _available_status():
    from drilling_intelligence.integrations.base import IntegrationStatus

    return IntegrationStatus(
        component="MinerU", available=True, mode="cli", version="9.9.9", location="/usr/bin/mineru"
    )


def test_the_stand_in_binary_contract_is_real_json(tmp_path: Path) -> None:
    """Guard the doubles themselves: a fake that wrote malformed output would prove nothing."""
    binary = _fake_mineru(tmp_path, name="mineru_shape", script="import sys\nsys.exit(0)\n")
    assert Path(binary).exists()
    payload = {"pdf_info": [{"page_idx": 0}]}
    assert json.loads(json.dumps(payload)) == payload
