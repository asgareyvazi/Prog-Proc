"""The ``drillintel`` command line: the core's own front end, headless and scriptable.

Deliberately thin - it opens a workspace, calls the service that owns the behaviour, and prints
what came back.  :mod:`drilling_intelligence.cli.app` documents the three reasons it exists.
"""

from .app import build_parser, main

__all__ = ["build_parser", "main"]
