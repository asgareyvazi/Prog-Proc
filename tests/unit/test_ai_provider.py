"""Ollama capability contract: states, no silent network, no engineering writes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from drilling_intelligence.cli import main
from drilling_intelligence.config.settings import Settings
from drilling_intelligence.integrations.ai.contract import (
    ProviderState,
    UnavailableVectorStore,
    configured_status,
)
from drilling_intelligence.integrations.ai.ollama import OllamaAdapter, probe_provider

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "sk-live-sentinel-should-not-leak"


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "provider": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "model": "qwen3:8b",
        "embedding_model": "nomic-embed-text",
        "embedding_dimensions": 0,
        "temperature": 0.0,
        "max_output_tokens": 16,
        "timeout_seconds": 30.0,
        "retries": 0,
        "retry_backoff_seconds": 0.0,
        "capability_probe": True,
        "require_ai": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network client constructed")

    monkeypatch.setattr(httpx, "Client", boom)
    monkeypatch.setattr(httpx, "request", boom)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_absent_provider_does_not_open_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    status = probe_provider(_config(provider="none"))
    assert status.state is ProviderState.UNCONFIGURED
    assert status.available is False
    assert status.probed is False
    assert status.authoritative is False
    assert SENTINEL not in json.dumps(status.to_dict())


def test_openai_compatible_is_unsupported_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)

    def secret() -> str:
        raise AssertionError("adapter read a credential")

    config = _config(provider="openai-compatible", openai_model="gpt-4o-mini")
    config.api_key = secret  # type: ignore[attr-defined]
    status = probe_provider(config)
    assert status.state is ProviderState.UNSUPPORTED
    assert status.available is False
    assert status.identity.provider == "openai-compatible"
    assert status.identity.model == "gpt-4o-mini"
    assert "no request is sent" in status.reason


def test_capability_probe_off_does_not_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    status = probe_provider(_config(capability_probe=False))
    assert status.state is ProviderState.NOT_PROBED
    assert status.available is False
    assert "capability_probe" in status.reason


def test_credentials_in_the_endpoint_are_refused_and_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_network(monkeypatch)
    status = probe_provider(_config(endpoint=f"http://user:{SENTINEL}@127.0.0.1:11434"))
    rendered = json.dumps(status.to_dict())
    assert status.state is ProviderState.UNSUPPORTED
    assert status.available is False
    assert SENTINEL not in rendered
    assert "user:" not in rendered


def test_a_listed_model_is_successful_only_after_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen3:8b"}, {"name": "nomic-embed-text"}]},
        )

    injected = _client(handler)
    _forbid_network(monkeypatch)
    status = probe_provider(_config(), client=injected)
    assert status.state is ProviderState.SUCCESSFUL
    assert status.available is True
    assert status.probed is True
    assert status.model_present is True
    assert status.embedding_model_listed is True
    assert status.identity.model == "qwen3:8b"
    assert status.authoritative is False
    assert calls == ["http://127.0.0.1:11434/api/tags"]


def test_a_missing_model_is_not_excused_by_a_live_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "other:latest"}]})

    injected = _client(handler)
    _forbid_network(monkeypatch)
    status = probe_provider(_config(), client=injected)
    assert status.state is ProviderState.UNSUPPORTED
    assert status.available is False
    assert status.model_present is False
    assert "qwen3:8b" in status.reason


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, ProviderState.UNAUTHORIZED),
        (403, ProviderState.UNAUTHORIZED),
        (404, ProviderState.UNSUPPORTED),
    ],
)
def test_http_errors_keep_their_state(
    monkeypatch: pytest.MonkeyPatch, status_code: int, expected: ProviderState
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=SENTINEL)

    injected = _client(handler)
    _forbid_network(monkeypatch)
    status = probe_provider(_config(), client=injected)
    assert status.state is expected
    assert status.available is False
    assert SENTINEL not in status.reason
    assert SENTINEL not in json.dumps(status.to_dict())


def test_connection_failure_is_unreachable_and_does_not_echo_the_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(SENTINEL)

    injected = _client(handler)
    _forbid_network(monkeypatch)
    status = probe_provider(_config(), client=injected)
    assert status.state is ProviderState.UNREACHABLE
    assert status.reason == "transport failed"
    assert SENTINEL not in json.dumps(status.to_dict())


def test_redirects_are_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"http://evil.example/{SENTINEL}"})

    injected = _client(handler)
    _forbid_network(monkeypatch)
    status = probe_provider(_config(), client=injected)
    assert status.state is ProviderState.UNSUPPORTED
    assert "redirects are not followed" in status.reason
    assert SENTINEL not in status.reason


def test_malformed_json_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SENTINEL)

    injected = _client(handler)
    _forbid_network(monkeypatch)
    status = probe_provider(_config(), client=injected)
    assert status.state is ProviderState.UNSUPPORTED
    assert status.reason == "response was not JSON"
    assert SENTINEL not in json.dumps(status.to_dict())


def test_completion_is_not_authoritative_and_retries_only_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"model": "qwen3:8b", "response": "a note"})

    injected = _client(handler)
    _forbid_network(monkeypatch)
    adapter = OllamaAdapter(
        _config(retries=5, retry_backoff_seconds=0.25),
        client=injected,
        sleeper=slept.append,
    )
    result = adapter.complete("  describe the header  ")
    assert result.state is ProviderState.SUCCESSFUL
    assert result.text == "a note"
    assert result.authoritative is False
    assert result.to_dict()["authoritative"] is False
    assert calls["n"] == 3
    assert slept == [0.25, 0.5]


def test_transport_retries_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    injected = _client(handler)
    _forbid_network(monkeypatch)
    result = OllamaAdapter(
        _config(retries=9, retry_backoff_seconds=0), client=injected, sleeper=lambda _delay: None
    ).complete("prompt")
    assert result.state is ProviderState.UNREACHABLE
    assert calls["n"] == 4


def test_http_401_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text=SENTINEL)

    injected = _client(handler)
    _forbid_network(monkeypatch)
    result = OllamaAdapter(
        _config(retries=2), client=injected, sleeper=lambda _delay: None
    ).complete("prompt")
    assert result.state is ProviderState.UNAUTHORIZED
    assert calls["n"] == 1
    assert SENTINEL not in result.reason


def test_a_mismatched_response_model_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "other:latest", "response": "secret text"})

    injected = _client(handler)
    _forbid_network(monkeypatch)
    result = OllamaAdapter(_config(), client=injected).complete("prompt")
    assert result.state is ProviderState.UNSUPPORTED
    assert result.text == ""
    assert "secret text" not in json.dumps(result.to_dict())


def test_empty_prompt_and_bad_texts_send_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    injected = _client(lambda _request: pytest.fail("request sent"))
    _forbid_network(monkeypatch)
    adapter = OllamaAdapter(_config(), client=injected)
    assert adapter.complete("   ").state is ProviderState.UNSUPPORTED
    assert adapter.embed(()).state is ProviderState.UNSUPPORTED
    assert adapter.embed(("ok", "")).state is ProviderState.UNSUPPORTED


def test_embedding_dimension_mismatch_discards_the_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "nomic-embed-text", "embeddings": [[0.1, 0.2]]})

    injected = _client(handler)
    _forbid_network(monkeypatch)
    result = OllamaAdapter(_config(embedding_dimensions=3), client=injected).embed(("mud",))
    assert result.state is ProviderState.UNSUPPORTED
    assert result.vectors == ()
    assert "3" in result.reason and "2" in result.reason


def test_embedding_success_keeps_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        body = json.loads(request.content)
        assert body["model"] == "nomic-embed-text"
        assert body["input"] == ["mud"]
        return httpx.Response(200, json={"model": "nomic-embed-text", "embeddings": [[1.0, 2.0]]})

    injected = _client(handler)
    _forbid_network(monkeypatch)
    result = OllamaAdapter(_config(embedding_dimensions=2), client=injected).embed(("mud",))
    assert result.state is ProviderState.SUCCESSFUL
    assert result.dimensions == 2
    assert result.vectors == ((1.0, 2.0),)
    assert result.authoritative is False


def test_summary_and_vector_store_do_not_open_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    summary = Settings.load().summary()["ai"]
    assert summary["implemented"] is True
    assert summary["available"] is False
    assert summary["probed"] is False
    assert summary["availability"] == "not_probed"
    store = UnavailableVectorStore().status()
    assert store.state is ProviderState.UNSUPPORTED
    assert store.available is False
    assert "SQLite" in store.reason


def test_engineering_paths_do_not_import_the_adapter() -> None:
    banned = ("integrations.ai", "OllamaAdapter", "probe_provider")
    roots = (
        "extraction",
        "knowledge",
        "operations",
        "reporting",
        "retrieval",
        "search",
        "engineering",
        "intelligence",
        "documents",
        "review",
        "ingestion",
    )
    offenders: list[str] = []
    for name in roots:
        for path in (ROOT / "src" / "drilling_intelligence" / name).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(token in text for token in banned):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_ai_status_cli_with_provider_none_does_not_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _forbid_network(monkeypatch)
    config = tmp_path / "config.toml"
    config.write_text('[ai]\nprovider = "none"\ncapability_probe = true\n', encoding="utf-8")
    root = tmp_path / "workspace"
    assert main(["workspace", "create", str(root), "--config", str(config)]) == 0
    capsys.readouterr()
    code = main(["ai", "status", "--json", "--workspace", str(root), "--config", str(config)])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["state"] == "unconfigured"
    assert payload["available"] is False
    assert payload["authoritative"] is False
    assert SENTINEL not in out


def test_live_probe_refuses_without_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    monkeypatch.delenv("DRILLINTEL_AI_LIVE", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ai_live_probe", ROOT / "tools" / "ai_live_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 2


def test_configured_status_matches_the_summary_contract() -> None:
    status = configured_status(_config())
    assert status.state is ProviderState.NOT_PROBED
    assert status.available is False
    assert "drillintel ai status" in status.reason
