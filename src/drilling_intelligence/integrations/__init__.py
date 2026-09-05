"""Adapters to external software - the only place third-party services are wrapped.

Every integration is *optional at runtime*: capabilities are probed, and an
absent dependency degrades the feature and reports the reason instead of
breaking the application or faking a result (master spec sections 61 and 92).
"""

from .base import IntegrationStatus, http_json, parse_json_loose, run_command, which

__all__ = ["IntegrationStatus", "http_json", "parse_json_loose", "run_command", "which"]
