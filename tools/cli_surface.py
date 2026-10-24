"""Render the ``drillintel`` command-line surface as a deterministic snapshot.

The CLI is the product's public contract: a command, a flag, a default or an
exit code that changes is a change a user can feel.  This tool prints that
surface in a stable, diffable form so a refactor can be reviewed against the
contract it promised to keep.

Usage::

    python tools/cli_surface.py            # print the surface
    python tools/cli_surface.py --check    # compare against the committed snapshot

The snapshot lives at ``tests/snapshots/cli_surface.txt``.  ``--check`` exits
non-zero when the surface and the snapshot differ, which is what the test
uses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "snapshots" / "cli_surface.txt"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _type_name(action: argparse.Action) -> str:
    kind = action.type
    if kind is None:
        return "-"
    return getattr(kind, "__name__", str(kind))


def _format_default(action: argparse.Action) -> str:
    default = action.default
    if default is argparse.SUPPRESS:
        return "<suppress>"
    if callable(default) and not isinstance(default, (str, int, float, bool, type(None))):
        return f"<callable {getattr(default, '__name__', default)}>"
    return repr(default)


def _choices(action: argparse.Action) -> str:
    choices = action.choices
    if not choices:
        return "-"
    if isinstance(choices, dict):
        return "|".join(sorted(str(key) for key in choices))
    try:
        return "|".join(sorted(str(choice) for choice in choices))
    except TypeError:  # pragma: no cover - argparse always gives an iterable
        return "-"


def _argument_lines(parser: argparse.ArgumentParser, path: str) -> list[str]:
    lines: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        flags = ",".join(action.option_strings) or f"({action.dest})"
        lines.append(
            "    arg {flags} dest={dest} type={type} nargs={nargs} default={default} "
            "required={required} choices={choices} action={action}".format(
                flags=flags,
                dest=action.dest,
                type=_type_name(action),
                nargs=action.nargs,
                default=_format_default(action),
                required=bool(action.required),
                choices=_choices(action),
                action=action.__class__.__name__,
            )
        )
    return lines


def _handler_name(parser: argparse.ArgumentParser) -> str:
    handler = parser.get_default("handler")
    if handler is None:
        return "-"
    return getattr(handler, "__name__", str(handler))


def _render(parser: argparse.ArgumentParser, path: str, out: list[str]) -> None:
    out.append(f"command {path}")
    out.append(f"  prog {parser.prog}")
    out.append(f"  description {parser.description or '-'}")
    out.append(f"  handler {_handler_name(parser)}")
    out.extend(_argument_lines(parser, path))
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            _render(child, f"{path} {name}", out)


def render_surface() -> str:
    """Return the whole CLI surface as text, one line per fact."""
    from drilling_intelligence.cli.app import build_parser

    out: list[str] = []
    _render(build_parser(), "", out)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare against the snapshot")
    args = parser.parse_args(argv)

    surface = render_surface()
    if not args.check:
        sys.stdout.write(surface)
        return 0

    if not SNAPSHOT.exists():
        sys.stderr.write(f"the snapshot is missing: {SNAPSHOT}\n")
        return 1
    expected = SNAPSHOT.read_text(encoding="utf-8")
    if expected != surface:
        sys.stderr.write("the CLI surface differs from the committed snapshot\n")
        _print_difference(expected, surface)
        return 1
    return 0


def _print_difference(expected: str, actual: str) -> None:
    import difflib

    diff = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=str(SNAPSHOT),
        tofile="the surface as built",
        lineterm="",
    )
    for line in diff:
        sys.stderr.write(line + "\n")


def _unused(*_args: Any) -> None:  # pragma: no cover - keeps linters quiet
    return None


if __name__ == "__main__":
    raise SystemExit(main())
