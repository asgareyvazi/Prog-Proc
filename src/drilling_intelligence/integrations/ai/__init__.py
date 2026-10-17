"""Optional AI providers. Importing this package does not open a socket."""

from .contract import (
    Completion,
    EmbeddingResult,
    ProviderState,
    ProviderStatus,
    UnavailableVectorStore,
    configured_status,
)

__all__ = [
    "Completion",
    "EmbeddingResult",
    "ProviderState",
    "ProviderStatus",
    "UnavailableVectorStore",
    "configured_status",
]
