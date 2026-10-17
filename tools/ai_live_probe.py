"""Opt-in probe of a real Ollama. Ordinary tests and CI do not run this.

Usage: ``DRILLINTEL_AI_LIVE=1 python tools/ai_live_probe.py``

The process exits 2 without that variable and does not open a socket. A live
run prints the probe state. It does not write a workspace or an engineering record.
"""

from __future__ import annotations

import os
import sys

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.integrations.ai.ollama import probe_provider


def main() -> int:
    if os.environ.get("DRILLINTEL_AI_LIVE") != "1":
        print(
            "refusing: set DRILLINTEL_AI_LIVE=1 to probe a real Ollama endpoint",
            file=sys.stderr,
        )
        return 2
    status = probe_provider(Settings.load().ai)
    print(status.state.value)
    print(status.reason)
    return 0 if status.available else 1


if __name__ == "__main__":
    raise SystemExit(main())
