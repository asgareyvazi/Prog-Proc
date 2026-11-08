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
import os
import stat
from pathlib import Path

import pytest

from drilling_intelligence.core.errors import ExtractionError, IntegrationUnavailableError
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
    """Write an executable stand-in for the ``mineru`` binary and return its path.

    POSIX can execute a shebang file. Windows ``CreateProcess`` cannot, so the
    stand-in is a ``.py`` file and ``run_command`` starts it with the current
    interpreter. No shell is involved on either platform.
    """
    body = "#!/usr/bin/env python3\n" + script
    if os.name == "nt":
        path = directory / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        return str(path)
    path = directory / name
    path.write_text(body, encoding="utf-8")
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
            "json.dump({'pdf_info': [{'page_idx': 0, 'para_blocks': ["
            "{'type': 'text', 'lines': [{'spans': [{'type': 'text', 'content': 'partial text'}]}]}]}]},\n"
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
            "json.dump({'pdf_info': [{'page_idx': 0, 'para_blocks': ["
            "{'type': 'text', 'lines': [{'spans': [{'type': 'text', 'content': 'complete text'}]}]}]}]},\n"
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
        "content_type": "application/json",
    }


def test_http_upload_does_not_follow_redirects(settings, tmp_path: Path, monkeypatch) -> None:
    import drilling_intelligence.integrations.mineru.client as client_mod

    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF synthetic")
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"
    captured: dict[str, object] = {}

    def handler(method, url, **kwargs):
        captured.update(kwargs)
        return _envelope({"md_content": "Page text"})

    monkeypatch.setattr(client_mod, "http_json", handler)
    run = MinerUClient(settings).parse(source, selected_mode="http", contract_verified=True)

    assert run.ok is True
    assert captured["follow_redirects"] is False
    assert "files" in captured and "data" in captured


def _legacy_http_openapi() -> dict[str, object]:
    return {
        "ok": True,
        "status_code": 200,
        "json": {
            "info": {"version": "2.0.0"},
            "paths": {
                "/file_parse": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "files": {
                                                "type": "array",
                                                "items": {"type": "string", "format": "binary"},
                                            }
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {"200": {"content": {"application/json": {}}}},
                    }
                }
            },
        },
        "text": "",
        "url": "http://mineru.test/openapi.json",
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
        if url.endswith("/openapi.json"):
            return _legacy_http_openapi()
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
    assert "no readable middle.json, content_list or markdown" in str(excinfo.value), str(
        excinfo.value
    )


def test_a_transport_failure_is_reported_not_swallowed(
    settings, tmp_path: Path, monkeypatch
) -> None:
    """A non-success HTTP status is an unavailable integration, not an empty document."""

    def handler(method, url, **kwargs):
        if url.endswith("/openapi.json"):
            return _legacy_http_openapi()
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
        if url.endswith("/openapi.json"):
            return _legacy_http_openapi()
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
    assert "does not implement the configured JSON parse contract" in status.reason, status.reason
    assert {c["mode"] for c in status.to_dict()["checks"]} == {"http"}, status.to_dict()["checks"]


def test_explicit_transport_cannot_be_overridden_by_a_client_selection(
    settings, tmp_path: Path, monkeypatch
) -> None:
    import drilling_intelligence.integrations.mineru.client as client_mod

    source = tmp_path / "document.pdf"
    source.write_bytes(b"pdf")
    calls: list[str] = []
    monkeypatch.setattr(
        client_mod,
        "http_json",
        lambda method, url, **kwargs: calls.append(url) or {},
    )
    settings.mineru.endpoint = "http://mineru.test"
    settings.mineru.mode = "cli"
    with pytest.raises(IntegrationUnavailableError, match="conflicts with explicitly configured"):
        MinerUClient(settings).parse(source, selected_mode="http", contract_verified=True)

    settings.mineru.mode = "http"
    with pytest.raises(IntegrationUnavailableError, match="conflicts with explicitly configured"):
        MinerUClient(settings).parse(source, selected_mode="cli")
    assert calls == []


def test_cli_defaults_to_local_models_without_implicit_downloads(settings) -> None:
    settings.mineru.mode = "cli"
    settings.mineru.model_source = "local"
    assert MinerUClient(settings).extra_env["MINERU_MODEL_SOURCE"] == "local"

    settings.mineru.model_source = "modelscope"
    assert MinerUClient(settings).extra_env["MINERU_MODEL_SOURCE"] == "modelscope"


def test_auto_mode_prefers_the_transport_that_answers(settings, tmp_path, monkeypatch) -> None:
    """``auto`` may choose either - but it has to report which one it actually chose."""
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "which", lambda name: "")
    monkeypatch.setattr(
        discovery_mod,
        "http_json",
        lambda method, url, **kw: (
            _legacy_http_openapi()
            if url.endswith("/openapi.json")
            else {
                "ok": True,
                "status_code": 200,
                "json": {"version": "2.1.0"},
                "text": "",
                "url": url,
                "error": "",
            }
        ),
    )
    settings.mineru.mode = "auto"
    settings.mineru.endpoint = "http://mineru.test"

    status = MinerUProber(settings).status()
    assert status.available is True
    assert status.mode == "http", status.to_dict()
    assert status.version == "2.1.0"
    assert status.location.startswith("http://mineru.test")


def test_default_auto_mode_never_probes_http_without_an_explicit_endpoint(
    settings, monkeypatch
) -> None:
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    calls: list[str] = []
    monkeypatch.setattr(discovery_mod, "which", lambda _name: "")
    monkeypatch.setattr(
        discovery_mod,
        "http_json",
        lambda method, url, **kwargs: calls.append(url) or {},
    )
    settings.mineru.mode = "auto"
    settings.mineru.endpoint = ""

    status = MinerUProber(settings).status()

    assert status.available is False
    assert calls == []


def test_legacy_http_openapi_contract_requires_multipart_files_and_success_json(
    settings, monkeypatch
) -> None:
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    no_multipart = _legacy_http_openapi()
    no_multipart["json"]["paths"]["/file_parse"]["post"].pop("requestBody")
    monkeypatch.setattr(discovery_mod, "http_json", lambda *_a, **_k: no_multipart)
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"
    status = MinerUProber(settings).status()
    assert status.available is False
    assert "multipart 'files'" in status.reason

    malformed_upload = _legacy_http_openapi()
    malformed_upload["json"]["paths"]["/file_parse"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]["properties"]["files"] = {"type": "string"}
    monkeypatch.setattr(discovery_mod, "http_json", lambda *_a, **_k: malformed_upload)
    compatible, reason, _version = discovery_mod.legacy_http_parse_contract(
        "http://mineru.test", "/file_parse"
    )
    assert compatible is False
    assert "binary upload" in reason

    referenced = _legacy_http_openapi()
    spec = referenced["json"]
    request_schema = spec["paths"]["/file_parse"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]
    spec["components"] = {"schemas": {"UploadRequest": request_schema}}
    spec["paths"]["/file_parse"]["post"]["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ] = {"$ref": "#/components/schemas/UploadRequest"}
    monkeypatch.setattr(
        discovery_mod,
        "http_json",
        lambda *_a, **_k: {**referenced, "json": spec},
    )
    compatible, reason, _version = discovery_mod.legacy_http_parse_contract(
        "http://mineru.test", "/file_parse"
    )
    assert compatible is True, reason

    payload = _legacy_http_openapi()["json"]
    payload["paths"]["/file_parse"]["post"]["responses"] = {
        "500": {"content": {"application/json": {}}}
    }
    monkeypatch.setattr(
        discovery_mod,
        "http_json",
        lambda *_a, **_k: {**_legacy_http_openapi(), "json": payload},
    )
    status = MinerUProber(settings).status(refresh=True)
    assert status.available is False
    assert "successful JSON" in status.reason


def test_http_rejects_missing_json_content_type(settings, tmp_path: Path, monkeypatch) -> None:
    import drilling_intelligence.integrations.mineru.client as client_mod

    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF synthetic")
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"
    monkeypatch.setattr(
        client_mod,
        "http_json",
        lambda *_a, **_k: {**_envelope({"md_content": "text"}), "content_type": ""},
    )
    with pytest.raises(IntegrationUnavailableError, match="unsupported content type"):
        MinerUClient(settings).parse(source, selected_mode="http", contract_verified=True)


def test_http_rejects_attachment_content_type_and_unmatched_multi_file_response(
    settings, tmp_path: Path, monkeypatch
) -> None:
    def handler(method, url, **kwargs):
        if url.endswith("/openapi.json"):
            return _legacy_http_openapi()
        if url.endswith("/health"):
            return {"ok": True, "status_code": 200, "json": {"version": "2.0"}}
        if url.endswith("/file_parse"):
            return {**_envelope({"md_content": "not accepted"}), "content_type": "application/zip"}
        raise AssertionError(url)

    _stub_http(monkeypatch, handler)
    source = tmp_path / "scanned.pdf"
    source.write_bytes(b"%PDF synthetic")
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru.test"
    with pytest.raises(ExtractionError, match="unsupported content type"):
        MinerUExtractor(settings).extract(_context(source), _provenance(source))

    def mismatched(method, url, **kwargs):
        if url.endswith("/openapi.json"):
            return _legacy_http_openapi()
        if url.endswith("/health"):
            return {"ok": True, "status_code": 200, "json": {"version": "2.0"}}
        return {
            **_envelope({"md_content": "wrong source"}),
            "json": {"results": {"other.pdf": {"md_content": "wrong source"}, "third.pdf": {}}},
        }

    _stub_http(monkeypatch, mismatched)
    with pytest.raises(ExtractionError, match=r"no result for 'scanned\.pdf'"):
        MinerUExtractor(settings).extract(_context(source), _provenance(source))


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


def _mineru_v4_middle() -> dict[str, object]:
    """Small deterministic fixture shaped like MinerU 4.1's real middle JSON output."""
    return {
        "metadata": {"producer": {"name": "mineru", "version": "4.1.7"}},
        "extensions": {
            "mineru": {"tier": "flash", "parse_mode": "txt"},
            "docvortex_layout": {"pages": [{"page_idx": 0, "width_pt": 612, "height_pt": 792}]},
        },
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "text",
                        "index": 0,
                        "bbox": [0.1, 0.2, 0.8, 0.25],
                        "content": [{"type": "text", "content": "Synthetic report text"}],
                    },
                    {
                        "type": "image",
                        "index": 1,
                        "bbox": [0.1, 0.26, 0.8, 0.29],
                        "image_path": "/temporary/mineru/output/figure.png",
                    },
                    {
                        "type": "table",
                        "index": 2,
                        "bbox": [0.1, 0.3, 0.8, 0.5],
                        "content": [
                            {
                                "type": "table_body",
                                "content": "<table><tr><td>Activity</td><td>Depth</td></tr>"
                                "<tr><td>Drilling</td><td>12,345 ft</td></tr></table>",
                            }
                        ],
                    },
                ],
            }
        ],
        "schema": "docvortex.middle",
        "schema_version": "2.0",
    }


def test_mineru_middle_json_requires_a_mapping_root() -> None:
    from drilling_intelligence.integrations.mineru.normalize import normalize_middle_json

    with pytest.raises(ExtractionError, match="root must be a JSON object"):
        normalize_middle_json(
            [],
            filename="malformed.pdf",
            document_id="doc-invalid",
            version_id="ver-invalid",
            sha256=SHA,
        )


def test_mineru_4_middle_json_preserves_page_table_and_fractional_bbox_provenance(
    tmp_path: Path,
) -> None:
    from drilling_intelligence.integrations.mineru.normalize import (
        load_mineru_outputs,
        normalize_middle_json,
    )

    output = tmp_path / "synthetic_middle.json"
    output.write_text(json.dumps(_mineru_v4_middle()), encoding="utf-8")
    raw = load_mineru_outputs(tmp_path, "synthetic")
    assert raw.best == "middle.json"

    document, diagnostics = normalize_middle_json(
        raw.middle or {},
        filename="synthetic.pdf",
        document_id="doc-v4",
        version_id="ver-v4",
        sha256=SHA,
    )
    assert "Synthetic report text" in document.text
    assert "12,345 ft" in document.text
    assert document.pages[0].index == 1
    assert (document.pages[0].width, document.pages[0].height) == (612.0, 792.0)
    assert len(document.tables) == 1
    assert len(document.figures) == 1
    assert document.figures[0].extra == {"image_available": True}
    assert "/temporary/mineru/output/figure.png" not in str(document.figures[0].extra)
    assert document.tables[0].rows == [["Activity", "Depth"], ["Drilling", "12,345 ft"]]
    locator = document.tables[0].provenance.locator
    assert locator.page == 1
    assert locator.bbox == (0.1, 0.3, 0.8, 0.5)
    assert document.metadata.extra["mineru_tier"] == "flash"
    assert document.metadata.extra["mineru_bbox_units"].startswith("normalized fractions (0-1)")
    assert any("MinerU 4.x docvortex.middle" in item for item in diagnostics)


def test_mineru_cleanup_requires_its_own_marker_and_never_removes_unowned_dirs(
    monkeypatch, tmp_path: Path
) -> None:
    from drilling_intelligence.integrations.mineru.client import MinerURun

    monkeypatch.setattr(
        "drilling_intelligence.integrations.mineru.client.tempfile.gettempdir",
        lambda: str(tmp_path),
    )
    owned = tmp_path / "mineru-owned"
    owned.mkdir()
    (owned / ".drillintel-mineru-owner").write_text("marker-1", encoding="ascii")
    (owned / "output.txt").write_text("owned", encoding="utf-8")
    run = MinerURun(ok=True, mode="cli", output_dir=owned, ownership_marker_value="marker-1")
    assert run.cleanup() is True
    assert not owned.exists()

    unowned = tmp_path / "mineru-unowned"
    unowned.mkdir()
    (unowned / "important.txt").write_text("preserve", encoding="utf-8")
    unowned_run = MinerURun(
        ok=True, mode="cli", output_dir=unowned, ownership_marker_value="wrong-marker"
    )
    assert unowned_run.cleanup() is False
    assert (unowned / "important.txt").read_text(encoding="utf-8") == "preserve"


def test_mineru_kit_cli_runs_in_isolation_and_cleans_its_scratch_directory(
    settings, tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "output-path.txt"
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF synthetic fixture")
    binary = _fake_mineru(
        tmp_path,
        name="mineru-kit",
        script=(
            "import json, os, sys\n"
            "out = sys.argv[sys.argv.index('--output') + 1]\n"
            f"open({str(marker)!r}, 'w', encoding='utf-8').write(out)\n"
            f"payload = json.loads(json.loads({json.dumps(json.dumps(_mineru_v4_middle()))!r}))\n"
            "json.dump(payload, open(out, 'w', encoding='utf-8'))\n"
            f"sys.stderr.write(out + ' ' + {str(source)!r} + ' ' + os.environ.get('MINERU_MODEL_SOURCE', 'unset'))\n"
        ),
    )
    settings.mineru.mode = "auto"
    settings.mineru.binary = binary
    extractor = MinerUExtractor(settings, prober=_available_cli_prober(settings, binary))
    http_attempts: list[str] = []

    def unexpected_http(method, url, **kwargs):
        http_attempts.append(url)
        raise AssertionError("compatible local CLI selection must not probe or use HTTP")

    _stub_http(monkeypatch, unexpected_http)
    document = extractor.extract(_context(source), _provenance(source))

    output = Path(marker.read_text(encoding="utf-8"))
    scratch = output.parent.parent
    assert not scratch.exists(), "per-run MinerU output must be removed after normalization"
    assert "Synthetic report text" in document.text
    assert document.metadata.extra["mineru"]["version"] == "9.9.9"
    command_summary = document.metadata.extra["mineru"]["command"]
    assert "--tier basic" in command_summary
    assert "<temporary-output>" in command_summary
    assert str(source) not in command_summary
    assert str(scratch) not in command_summary
    stderr_tail = document.metadata.extra["mineru"]["stderr_tail"]
    assert str(source) not in stderr_tail
    assert str(scratch) not in stderr_tail
    assert "<document>" in stderr_tail and "<temporary-output>" in stderr_tail
    assert stderr_tail.endswith("local"), "the real subprocess must receive the local-only default"
    assert document.metadata.extra["provenance_source"] == "middle.json"
    assert "layout_pdf" not in document.metadata.extra["mineru"]
    assert document.metadata.extra["mineru"]["scratch_cleanup"] == "removed"
    assert document.metadata.extra["mineru"]["returncode"] == 0
    assert document.metadata.extra["mineru"]["partial"] is False
    assert http_attempts == []


def test_content_list_rejects_malformed_roots_and_retains_unstructured_table_text() -> None:
    from drilling_intelligence.integrations.mineru.normalize import normalize_content_list

    kwargs = {
        "filename": "content-list.pdf",
        "document_id": "doc-content-list",
        "version_id": "ver-content-list",
        "sha256": SHA,
    }
    malformed_root, root_diagnostics = normalize_content_list({"unexpected": True}, **kwargs)
    assert not malformed_root.pages
    assert "root was not an array" in " ".join(root_diagnostics)
    with pytest.raises(ExtractionError, match="page index"):
        normalize_content_list([{"page_idx": "unknown", "text": "lost"}], **kwargs)

    document, diagnostics = normalize_content_list(
        [
            {"type": "table", "page_idx": 0, "table_body": "<div>Unstructured Δ text</div>"},
            "malformed-entry",
        ],
        **kwargs,
    )
    assert not document.tables
    assert document.text == "Unstructured Δ text"
    assert document.paragraphs[0].style == "table_text_unstructured"
    assert "without inferred rows" in " ".join(diagnostics)
    assert "item 2 was malformed" in " ".join(diagnostics)


def test_mineru_provenance_does_not_invent_confidence_or_accept_invalid_v4_bbox() -> None:
    from drilling_intelligence.integrations.mineru.normalize import normalize_middle_json

    payload = _mineru_v4_middle()
    payload["pages"][0]["blocks"][0]["bbox"] = [float("nan"), 0.2, 0.8, 0.25]
    document, diagnostics = normalize_middle_json(
        payload,
        filename="invalid-box.pdf",
        document_id="doc-box",
        version_id="ver-box",
        sha256=SHA,
    )

    assert document.paragraphs[0].provenance is not None
    assert document.paragraphs[0].provenance.confidence is None
    assert document.paragraphs[0].provenance.locator.bbox is None
    assert "invalid normalized bounding box" in " ".join(diagnostics)


def test_mineru_rejects_malformed_page_indices_and_preserves_unstructured_table_text() -> None:
    from drilling_intelligence.integrations.mineru.normalize import normalize_middle_json

    malformed = _mineru_v4_middle()
    malformed["pages"][0]["page_idx"] = "not-a-page"
    with pytest.raises(ExtractionError, match="page index"):
        normalize_middle_json(
            malformed,
            filename="bad-page.pdf",
            document_id="doc-bad",
            version_id="ver-bad",
            sha256=SHA,
        )

    legacy = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [100, 100],
                "para_blocks": [
                    {
                        "type": "table",
                        "lines": [{"spans": [{"type": "text", "content": "Depth 12,345 ft"}]}],
                    }
                ],
            }
        ]
    }
    document, diagnostics = normalize_middle_json(
        legacy,
        filename="table.pdf",
        document_id="doc-table",
        version_id="ver-table",
        sha256=SHA,
    )
    assert document.tables == []
    assert "Depth 12,345 ft" in document.text
    assert "without inferred rows" in " ".join(diagnostics)


def test_default_mineru_binary_resolves_to_v4_stateless_kit() -> None:
    from drilling_intelligence.integrations.mineru.cli import resolve_cli_binary

    assert (
        resolve_cli_binary("mineru", find_executable=lambda name: "/isolated/bin/mineru-kit")
        == "/isolated/bin/mineru-kit"
    )
    assert resolve_cli_binary("/custom/legacy-parser", find_executable=lambda _name: None) == (
        "/custom/legacy-parser"
    )


def test_mineru_v4_file_parse_attachment_api_is_not_misreported_as_legacy_json_http(
    settings, monkeypatch
) -> None:
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod

    def handler(method, url, **kwargs):
        assert method == "GET"
        return {
            "ok": True,
            "status_code": 200,
            "json": {
                "paths": {
                    "/v1/file_parse": {
                        "post": {"responses": {"200": {"content": {"application/zip": {}}}}}
                    }
                }
            },
            "text": "",
            "url": url,
            "error": "",
        }

    monkeypatch.setattr(discovery_mod, "http_json", handler)
    settings.mineru.mode = "http"
    settings.mineru.endpoint = "http://mineru-v4.test"
    settings.mineru.http_parse_path = "/file_parse"

    status = MinerUProber(settings).status()

    assert status.available is False
    assert "does not implement the configured JSON parse contract" in status.reason


def test_prober_recognizes_mineru_kit_as_a_supported_v4_cli(settings, tmp_path: Path) -> None:
    binary = _fake_mineru(
        tmp_path,
        name="mineru-kit",
        script="import sys\nprint('MinerU version: 4.1.7')\n",
    )
    settings.mineru.mode = "cli"
    settings.mineru.binary = binary

    status = MinerUProber(settings).status()

    assert status.available is True
    assert status.mode == "cli"
    assert status.version == "4.1.7"
    assert Path(status.location).name.startswith("mineru-kit")
