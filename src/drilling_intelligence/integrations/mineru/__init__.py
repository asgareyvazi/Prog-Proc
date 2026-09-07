"""MinerU integration (document parsing for scanned/complex PDFs).

MinerU is treated as an external capability, never a requirement: the adapter
probes the CLI/HTTP surface, normalises MinerU's own output schema into this
platform's :class:`NormalizedDocument`, and records which artefact supplied each
field.  When MinerU is absent the built-in extractors serve the request and the
fallback is visible in the routing decision.
"""

from .adapter import MinerUExtractor
from .client import MinerUClient, MinerURun
from .discovery import MinerUProber
from .normalize import (
    MinerURawOutput,
    load_mineru_outputs,
    normalize_markdown,
    normalize_middle_json,
)

__all__ = [
    "MinerUClient",
    "MinerUExtractor",
    "MinerUProber",
    "MinerURawOutput",
    "MinerURun",
    "load_mineru_outputs",
    "normalize_content_list",
    "normalize_markdown",
    "normalize_middle_json",
    "parse_table_html",
]
