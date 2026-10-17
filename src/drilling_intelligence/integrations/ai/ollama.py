"""Ollama adapter over httpx. Local, optional, and not an engineering writer.

A probe is one GET of ``/api/tags``. Completion is one non-streaming
``/api/generate``. Embeddings are one ``/api/embed``. Redirects are not
followed. Transport failures retry at most three times; HTTP errors do not.
The response body is never copied into an error string.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from ...core.redact import safe_detail
from .contract import (
    Completion,
    EmbeddingResult,
    ProviderState,
    ProviderStatus,
    configured_status,
    endpoint_problem,
    identity_for,
    request_origin,
)

_PROBE_TIMEOUT_CAP_S = 5.0
_RETRY_CAP = 3
_TAGS = "/api/tags"
_GENERATE = "/api/generate"
_EMBED = "/api/embed"


def probe_timeout(config: object) -> float:
    """A probe must not inherit a multi-minute or non-finite generation timeout."""
    timeout = _finite_timeout(getattr(config, "timeout_seconds", _PROBE_TIMEOUT_CAP_S))
    if timeout is None:
        return _PROBE_TIMEOUT_CAP_S
    return min(timeout, _PROBE_TIMEOUT_CAP_S)


def _finite_timeout(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        timeout = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        return None
    return timeout


def _retry_limit(config: object) -> int:
    raw = getattr(config, "retries", 0)
    if isinstance(raw, bool):
        return 0
    try:
        retries = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(retries, 0), _RETRY_CAP)


def _backoff(config: object) -> float:
    raw = getattr(config, "retry_backoff_seconds", 0.0)
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


class OllamaAdapter:
    """Talk to a local Ollama. The caller owns an injected client; we do not."""

    def __init__(
        self,
        config: object,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._sleeper = sleeper or time.sleep

    def probe(self) -> ProviderStatus:
        """List models. Success requires the configured model to be listed."""
        refused = configured_status(self._config)
        if refused.state is not ProviderState.NOT_PROBED or not bool(
            getattr(self._config, "capability_probe", True)
        ):
            return refused
        if _text(getattr(self._config, "provider", "")).lower() != "ollama":
            return refused
        origin = request_origin(_text(getattr(self._config, "endpoint", "")))
        if origin is None:
            return refused
        response = self._send(
            "GET",
            origin + _TAGS,
            json_body=None,
            timeout=probe_timeout(self._config),
            retries=0,
        )
        return self._status_from_tags(response)

    def complete(self, prompt: str, *, model: str | None = None) -> Completion:
        """Non-streaming generation. The text is not stored and not authoritative."""
        identity = identity_for(self._config)
        refused = self._call_refused()
        if refused is not None:
            return Completion(
                state=refused.state, identity=identity, model=identity.model, reason=refused.reason
            )
        if not isinstance(prompt, str) or not prompt.strip():
            return Completion(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                model=identity.model,
                reason="prompt is empty; no request is sent",
            )
        requested = model.strip() if isinstance(model, str) and model.strip() else identity.model
        if not requested:
            return Completion(
                state=ProviderState.UNCONFIGURED,
                identity=identity,
                model="",
                reason="model is empty; no request is sent",
            )
        body = {
            "model": requested,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": float(getattr(self._config, "temperature", 0.0) or 0.0),
                "num_predict": int(getattr(self._config, "max_output_tokens", 0) or 0),
            },
        }
        origin = request_origin(_text(getattr(self._config, "endpoint", "")))
        timeout = _finite_timeout(getattr(self._config, "timeout_seconds", 30.0))
        if origin is None or timeout is None:
            return Completion(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                model=requested,
                reason="endpoint or timeout is not usable; no request is sent",
            )
        response = self._send(
            "POST",
            origin + _GENERATE,
            json_body=body,
            timeout=timeout,
            retries=_retry_limit(self._config),
        )
        if response.state is not None:
            return Completion(
                state=response.state, identity=identity, model=requested, reason=response.reason
            )
        payload = response.payload
        answered = payload.get("model") if isinstance(payload, dict) else None
        text = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(answered, str) or answered != requested or not isinstance(text, str):
            return Completion(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                model=requested,
                reason="provider response did not identify the requested model",
            )
        return Completion(
            state=ProviderState.SUCCESSFUL, identity=identity, model=answered, text=text
        )

    def embed(self, texts: Sequence[str], *, model: str | None = None) -> EmbeddingResult:
        """Request vectors. A dimension mismatch is unsupported, not coerced."""
        identity = identity_for(self._config)
        requested = (
            model.strip() if isinstance(model, str) and model.strip() else identity.embedding_model
        )
        refused = self._call_refused()
        if refused is not None:
            return EmbeddingResult(
                state=refused.state, identity=identity, model=requested, reason=refused.reason
            )
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)) or not texts:
            return EmbeddingResult(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                model=requested,
                reason="texts must be a non-empty sequence of strings; no request is sent",
            )
        if any(not isinstance(item, str) or not item.strip() for item in texts):
            return EmbeddingResult(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                model=requested,
                reason="every text must be a non-empty string; no request is sent",
            )
        if not requested:
            return EmbeddingResult(
                state=ProviderState.UNCONFIGURED,
                identity=identity,
                model="",
                reason="embedding model is empty; no request is sent",
            )
        origin = request_origin(_text(getattr(self._config, "endpoint", "")))
        timeout = _finite_timeout(getattr(self._config, "timeout_seconds", 30.0))
        if origin is None or timeout is None:
            return EmbeddingResult(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                model=requested,
                reason="endpoint or timeout is not usable; no request is sent",
            )
        accepted = tuple(texts)
        response = self._send(
            "POST",
            origin + _EMBED,
            json_body={"model": requested, "input": list(accepted)},
            timeout=timeout,
            retries=_retry_limit(self._config),
        )
        if response.state is not None:
            return EmbeddingResult(
                state=response.state, identity=identity, model=requested, reason=response.reason
            )
        return self._vectors_from(response.payload, requested, len(accepted))

    def _call_refused(self) -> ProviderStatus | None:
        provider = _text(getattr(self._config, "provider", "")).lower()
        if provider != "ollama":
            return configured_status(self._config)
        if endpoint_problem(_text(getattr(self._config, "endpoint", ""))) is not None:
            return configured_status(self._config)
        return None

    def _status_from_tags(self, response: _Reply) -> ProviderStatus:
        identity = identity_for(self._config)
        base = configured_status(self._config)
        if response.state is not None:
            return ProviderStatus(
                state=response.state,
                identity=identity,
                probed=True,
                reason=response.reason,
                limitations=base.limitations,
            )
        payload = response.payload
        models_raw = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models_raw, list):
            return ProviderStatus(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                probed=True,
                reason="provider response did not list models",
                limitations=base.limitations,
            )
        names: list[str] = []
        for item in models_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") if isinstance(item.get("name"), str) else item.get("model")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        present = identity.model in names
        listed = identity.embedding_model in names if identity.embedding_model else None
        if not present:
            return ProviderStatus(
                state=ProviderState.UNSUPPORTED,
                identity=identity,
                probed=True,
                reason=f"model {identity.model or '(none)'} is not listed by the provider",
                model_present=False,
                embedding_model_listed=listed,
                models=tuple(names),
                limitations=base.limitations,
            )
        return ProviderStatus(
            state=ProviderState.SUCCESSFUL,
            identity=identity,
            available=True,
            probed=True,
            reason="configured model is listed by the provider",
            model_present=True,
            embedding_model_listed=listed,
            models=tuple(names),
            limitations=base.limitations,
        )

    def _vectors_from(
        self, payload: object, requested: str, expected_count: int
    ) -> EmbeddingResult:
        identity = identity_for(self._config)
        rows = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return self._rejected_embedding(
                requested, "provider response did not contain embeddings"
            )
        vectors: list[tuple[float, ...]] = []
        width: int | None = None
        for row in rows:
            parsed = finite_vector(row)
            if parsed is None:
                return self._rejected_embedding(
                    requested, "provider embeddings were not finite numeric vectors"
                )
            if width is None:
                width = len(parsed)
            elif len(parsed) != width:
                return self._rejected_embedding(
                    requested, "provider embeddings did not share one dimension"
                )
            vectors.append(parsed)
        if len(vectors) != expected_count:
            return self._rejected_embedding(
                requested, "embedding count does not match the input count"
            )
        expected = getattr(self._config, "embedding_dimensions", 0)
        try:
            expected_width = int(expected or 0)
        except (TypeError, ValueError, OverflowError):
            expected_width = -1
        if isinstance(expected, bool) or expected_width < 0:
            return self._rejected_embedding(
                requested, "configured embedding dimension is not usable"
            )
        if expected_width and width != expected_width:
            return self._rejected_embedding(
                requested,
                f"embedding dimension {width} does not match configured {expected_width}",
            )
        answered = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(answered, str) or not answered.strip() or answered != requested:
            return self._rejected_embedding(
                requested, "provider response did not identify the requested embedding model"
            )
        return EmbeddingResult(
            state=ProviderState.SUCCESSFUL,
            identity=identity,
            model=answered,
            dimensions=width or 0,
            vectors=tuple(vectors),
        )

    def _rejected_embedding(self, requested: str, reason: str) -> EmbeddingResult:
        return EmbeddingResult(
            state=ProviderState.UNSUPPORTED,
            identity=identity_for(self._config),
            model=requested,
            vectors=(),
            reason=reason,
        )

    def _send(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        timeout: float,
        retries: int,
    ) -> _Reply:
        attempts = retries + 1
        delay = _backoff(self._config)
        last = "request failed"
        for attempt in range(attempts):
            try:
                response = self._request(method, url, json_body=json_body, timeout=timeout)
            except httpx.TimeoutException:
                last = "timed out"
            except httpx.TransportError:
                last = "transport failed"
            except httpx.HTTPError:
                last = "request failed"
            else:
                return _classify(response)
            if attempt + 1 < attempts and delay:
                self._sleeper(delay * (attempt + 1))
        return _Reply(state=ProviderState.UNREACHABLE, reason=safe_detail(last) or "unreachable")

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        timeout: float,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "timeout": timeout,
            "follow_redirects": False,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if self._client is not None:
            return self._client.request(**kwargs)
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            return client.request(**kwargs)


class _Reply:
    def __init__(
        self,
        *,
        state: ProviderState | None = None,
        reason: str = "",
        payload: object = None,
    ) -> None:
        self.state = state
        self.reason = reason
        self.payload = payload


def _classify(response: httpx.Response) -> _Reply:
    code = response.status_code
    if code in {301, 302, 303, 307, 308}:
        return _Reply(
            state=ProviderState.UNSUPPORTED, reason=f"HTTP {code}; redirects are not followed"
        )
    if code in {401, 403}:
        return _Reply(state=ProviderState.UNAUTHORIZED, reason=f"HTTP {code}")
    if code < 200 or code >= 300:
        return _Reply(state=ProviderState.UNSUPPORTED, reason=f"HTTP {code}")
    try:
        payload = response.json()
    except ValueError:
        return _Reply(state=ProviderState.UNSUPPORTED, reason="response was not JSON")
    if not isinstance(payload, dict):
        return _Reply(state=ProviderState.UNSUPPORTED, reason="response was not a JSON object")
    return _Reply(payload=payload)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def finite_vector(row: object) -> tuple[float, ...] | None:
    """One finite numeric row, or None. Booleans, NaN, and infinities are rejected."""
    if not isinstance(row, list) or not row:
        return None
    values: list[float] = []
    for item in row:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        values.append(number)
    return tuple(values)


def probe_provider(
    config: object,
    *,
    client: httpx.Client | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> ProviderStatus:
    """Probe when the configuration allows it. ``none`` never builds a request."""
    provider = _text(getattr(config, "provider", "")).lower()
    if provider != "ollama":
        return configured_status(config)
    return OllamaAdapter(config, client=client, sleeper=sleeper).probe()


__all__ = ["OllamaAdapter", "finite_vector", "probe_provider", "probe_timeout"]
