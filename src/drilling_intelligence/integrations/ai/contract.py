"""AI provider contract (ADR-0005, ADR-51).

The protocols are the seam. A configured name is not availability, and a
completion is not an engineering record. Nothing in this module opens a
socket or writes a database row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse


class ProviderState(StrEnum):
    """What a probe, or a deliberate refusal to probe, established."""

    UNCONFIGURED = "unconfigured"
    NOT_PROBED = "not_probed"
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    UNSUPPORTED = "unsupported"
    SUCCESSFUL = "successful"


_OUTPUT_LIMITATION = "adapter output is not an engineering record, a citation, or a registry write"
_STREAM_LIMITATION = "[ai].stream is accepted and not used; responses are one JSON document"
_STRUCTURE_LIMITATION = "[ai].structured_output is accepted and not enforced"
_CONTEXT_LIMITATION = "[ai].max_context_tokens is accepted and not enforced"


@dataclass(frozen=True)
class ProviderIdentity:
    """The provider and model the operator configured. Never a secret."""

    provider: str
    endpoint: str
    model: str
    embedding_model: str

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "embedding_model": self.embedding_model,
        }


@dataclass(frozen=True)
class ProviderStatus:
    """A probe result. ``available`` is true only after a successful probe."""

    state: ProviderState
    identity: ProviderIdentity
    available: bool = False
    probed: bool = False
    reason: str = ""
    model_present: bool | None = None
    embedding_model_listed: bool | None = None
    models: tuple[str, ...] = ()
    limitations: tuple[str, ...] = field(default_factory=tuple)
    authoritative: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "available": self.available,
            "probed": self.probed,
            "reason": self.reason,
            "identity": self.identity.to_dict(),
            "model_present": self.model_present,
            "embedding_model_listed": self.embedding_model_listed,
            "models": list(self.models),
            "limitations": list(self.limitations),
            "authoritative": False,
        }


@dataclass(frozen=True)
class Completion:
    """Text from a provider. ``authoritative`` is always false."""

    state: ProviderState
    identity: ProviderIdentity
    model: str
    text: str = ""
    reason: str = ""
    authoritative: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "provider": self.identity.provider,
            "endpoint": self.identity.endpoint,
            "model": self.model,
            "text": self.text,
            "reason": self.reason,
            "authoritative": False,
        }


@dataclass(frozen=True)
class EmbeddingResult:
    """Vectors from a provider. They are not a search index and not a record."""

    state: ProviderState
    identity: ProviderIdentity
    model: str
    dimensions: int = 0
    vectors: tuple[tuple[float, ...], ...] = ()
    reason: str = ""
    authoritative: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "provider": self.identity.provider,
            "endpoint": self.identity.endpoint,
            "model": self.model,
            "dimensions": self.dimensions,
            "count": len(self.vectors),
            "reason": self.reason,
            "authoritative": False,
        }


class LLMProvider(Protocol):
    """A completion provider. Implementations must not write domain rows."""

    def probe(self) -> ProviderStatus:
        """Ask the endpoint what it is. Configuration alone cannot succeed."""

    def complete(self, prompt: str, *, model: str | None = None) -> Completion:
        """Return non-authoritative text, or a named failure state."""


class EmbeddingProvider(Protocol):
    """An embedding provider. Implementations must not store the vectors."""

    def embed(
        self, texts: tuple[str, ...] | list[str], *, model: str | None = None
    ) -> EmbeddingResult:
        """Return vectors, or a named failure state. Never a similarity score."""


class VectorStore(Protocol):
    """Reserved by ADR-0005. This build has no implementation that stores vectors."""

    def status(self) -> ProviderStatus:
        """Report that retrieval still uses SQLite keyword and structured search."""


def _text(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    return default


def identity_for(config: object) -> ProviderIdentity:
    """Read the configured names. An OpenAI-compatible slot uses its own fields."""
    provider = _text(getattr(config, "provider", "")).lower()
    if provider == "openai-compatible":
        endpoint = _text(getattr(config, "openai_endpoint", ""))
        model = _text(getattr(config, "openai_model", ""))
    else:
        endpoint = _text(getattr(config, "endpoint", ""))
        model = _text(getattr(config, "model", ""))
    return ProviderIdentity(
        provider=provider,
        endpoint=public_endpoint(endpoint),
        model=model,
        embedding_model=_text(getattr(config, "embedding_model", "")),
    )


def public_endpoint(raw: str) -> str:
    """Origin only. Userinfo, query, fragment, and a rejected URL are not returned."""
    parsed = _parsed_endpoint(raw)
    if parsed is None:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def endpoint_problem(raw: str) -> str | None:
    """Why this endpoint must not be requested, or None when it may be.

    A usable Ollama base is an http(s) origin. A path, query, or fragment is
    refused so the adapter cannot append ``/api/tags`` onto a different URL.
    """
    text = raw.strip()
    if not text:
        return "endpoint is empty"
    if any(character.isspace() for character in text):
        return "endpoint must not contain whitespace"
    parsed = urlparse(text)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return "endpoint must not carry credentials"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "endpoint must be an http or https URL"
    if parsed.query or parsed.fragment:
        return "endpoint must not include a query or fragment"
    if parsed.path not in {"", "/"}:
        return "endpoint must be an origin, not a path"
    return None


def request_origin(raw: str) -> str | None:
    """Canonical origin for a request, or None when the endpoint is refused."""
    if endpoint_problem(raw) is not None:
        return None
    parsed = urlparse(raw.strip())
    return f"{parsed.scheme}://{parsed.netloc}"


def _parsed_endpoint(raw: str):
    text = raw.strip()
    if not text or any(character.isspace() for character in text):
        return None
    parsed = urlparse(text)
    if (
        parsed.username
        or parsed.password
        or "@" in parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
    ):
        return None
    return parsed


def configured_status(config: object) -> ProviderStatus:
    """Describe configuration without opening a socket.

    ``available`` is false. A later probe is the only way to become successful.
    """
    identity = identity_for(config)
    provider = identity.provider
    limitations = (
        _OUTPUT_LIMITATION,
        _STREAM_LIMITATION,
        _STRUCTURE_LIMITATION,
        _CONTEXT_LIMITATION,
    )
    if provider in {"", "none"}:
        return ProviderStatus(
            state=ProviderState.UNCONFIGURED,
            identity=identity,
            reason="provider is none; no request is sent",
            limitations=limitations,
        )
    if provider == "openai-compatible":
        return ProviderStatus(
            state=ProviderState.UNSUPPORTED,
            identity=identity,
            reason="openai-compatible has no adapter in this build; no request is sent",
            limitations=(*limitations, "only the Ollama adapter is implemented"),
        )
    if provider != "ollama":
        return ProviderStatus(
            state=ProviderState.UNSUPPORTED,
            identity=identity,
            reason=f"provider {provider!r} is not implemented; no request is sent",
            limitations=limitations,
        )
    problem = endpoint_problem(_text(getattr(config, "endpoint", "")))
    if problem is not None:
        return ProviderStatus(
            state=ProviderState.UNCONFIGURED
            if problem == "endpoint is empty"
            else ProviderState.UNSUPPORTED,
            identity=identity,
            reason=f"{problem}; no request is sent",
            limitations=limitations,
        )
    if not bool(getattr(config, "capability_probe", True)):
        return ProviderStatus(
            state=ProviderState.NOT_PROBED,
            identity=identity,
            reason="[ai].capability_probe is false; configuration is not availability",
            limitations=limitations,
        )
    return ProviderStatus(
        state=ProviderState.NOT_PROBED,
        identity=identity,
        reason="configuration is not availability; run `drillintel ai status` to probe",
        limitations=limitations,
    )


class UnavailableVectorStore:
    """The ADR-0005 vector-store seam. Keyword search remains the retrieval path."""

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            state=ProviderState.UNSUPPORTED,
            identity=ProviderIdentity(provider="none", endpoint="", model="", embedding_model=""),
            reason=(
                "no vector store is implemented; retrieval stays on SQLite keyword and structured search"
            ),
            limitations=("vectors are not stored", _OUTPUT_LIMITATION),
        )


__all__ = [
    "Completion",
    "EmbeddingProvider",
    "EmbeddingResult",
    "LLMProvider",
    "ProviderIdentity",
    "ProviderState",
    "ProviderStatus",
    "UnavailableVectorStore",
    "VectorStore",
    "configured_status",
    "endpoint_problem",
    "identity_for",
    "public_endpoint",
    "request_origin",
]
