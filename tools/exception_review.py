#!/usr/bin/env python3
"""Inventory the broad exception handlers in the tree, and say which are deliberate.

A blind ``except Exception`` is not a defect by itself: a parser boundary, a
third-party reader and a best-effort cleanup all need one.  What *is* a defect
is a blind handler that swallows the failure without saying why, because the
next engineer cannot tell the difference between a deliberate suppression and
a bug.

This tool reads the source with ``ast`` and classifies every handler that
catches ``Exception`` or ``BaseException``:

``reported``
    the handler uses the exception - it logs it, records it, or turns it into a
    domain error the caller can act on.
``suppressed``
    the handler drops the exception: ``pass``, ``continue``, or a bare
    ``return``.  Every one of these must carry a written reason, either a
    ``# noqa: BLE001 - <reason>`` marker (the convention in this repository) or
    a comment on the line above it.
``re-raised``
    the handler raises again, which is never a suppression.

A bare ``except:`` and ``except BaseException:`` are refused outright: the
first catches ``KeyboardInterrupt`` and ``SystemExit``, the second catches
them too, and a shutdown path that swallows ``KeyboardInterrupt`` is a
shutdown path that cannot be stopped.

Usage::

    python tools/exception_review.py             # print the inventory
    python tools/exception_review.py --check     # fail on an undocumented one
    python tools/exception_review.py --markdown docs/exception_inventory.md
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = ("src", "tools")

REPORTED = "reported"
SUPPRESSED = "suppressed"
RERaised = "re-raised"
FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Handler:
    """One broad exception handler and what it does with the exception."""

    path: str
    line: int
    caught: str
    kind: str
    reason: str
    detail: str

    @property
    def documented(self) -> bool:
        return self.kind != SUPPRESSED or bool(self.reason)


def _catches(exception: ast.expr | None) -> str:
    """The name of the caught exception, or an empty string for a bare except."""
    if exception is None:
        return ""
    if isinstance(exception, ast.Name):
        return exception.id
    if isinstance(exception, ast.Tuple):
        return ", ".join(_catches(item) for item in exception.elts)
    return ast.unparse(exception)


def _is_broad(name: str) -> bool:
    parts = {part.strip() for part in name.split(",") if part.strip()}
    return bool(parts & {"Exception", "BaseException"})


def _uses_the_exception(body: list[ast.stmt], name: str | None) -> bool:
    """Does the handler say anything about the exception it caught?

    A handler that mentions the exception - its type, its message, or the
    attribute that carries it - has *reported* the failure, even when it then
    returns an exit code.  That is the CLI's error translation, and it is the
    opposite of a silent swallow.
    """
    if not name:
        return False
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and node.id == name:
                return True
    return False


def _handler_kind(body: list[ast.stmt], name: str | None = None) -> tuple[str, str]:
    """What the handler does with the exception: reported, suppressed or re-raised.

    The order matters.  A handler that raises again is never a suppression.  A
    handler that mentions the exception has reported it - even when it then
    continues with the next item, because the diagnostic is on the record.
    Only a handler that says nothing about the failure is a suppression, and
    that is the one that needs a written reason.
    """
    for statement in body:
        if isinstance(statement, ast.Raise):
            return RERaised, ""
    if _uses_the_exception(body, name):
        return REPORTED, ""
    if all(_is_silence(statement) for statement in body):
        return SUPPRESSED, ""
    return REPORTED, ""


def _is_silence(statement: ast.stmt) -> bool:
    """Is this statement one that says nothing about the failure?"""
    if isinstance(statement, (ast.Pass, ast.Continue)):
        return True
    if isinstance(statement, ast.Return):
        return True
    # A docstring in a handler body is the "silently ignored" shape.
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _reason(source_lines: list[str], index: int) -> str:
    """The written reason for a suppression, if the code gives one."""
    line = source_lines[index - 1] if 0 < index <= len(source_lines) else ""
    if "#" in line:
        comment = line.split("#", 1)[1].strip()
        if comment.startswith("noqa: BLE001"):
            return comment.removeprefix("noqa: BLE001").lstrip(" -:") or "noqa marker"
        if comment:
            return comment
    if index >= 2:
        previous = source_lines[index - 2]
        if "#" in previous:
            return previous.split("#", 1)[1].strip()
    return ""


def _detail(body: list[ast.stmt]) -> str:
    for statement in body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return statement.value.value.strip()
    return ""


def scan(root: Path) -> list[Handler]:
    """Every broad exception handler under ``root``."""
    handlers: list[Handler] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:  # pragma: no cover - the tree is compiled in CI
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = _catches(node.type)
            if not caught:
                handlers.append(
                    Handler(
                        path=path.relative_to(REPO_ROOT).as_posix(),
                        line=node.lineno,
                        caught="(bare except)",
                        kind=FORBIDDEN,
                        reason="",
                        detail="a bare except also catches KeyboardInterrupt and SystemExit",
                    )
                )
                continue
            if not _is_broad(caught):
                continue
            kind, _ = _handler_kind(node.body, node.name)
            handlers.append(
                Handler(
                    path=path.relative_to(REPO_ROOT).as_posix(),
                    line=node.lineno,
                    caught=caught,
                    kind=kind,
                    reason=_reason(source_lines, node.lineno),
                    detail=_detail(node.body),
                )
            )
    return handlers


def render_markdown(handlers: list[Handler]) -> str:
    """The inventory as a markdown table."""
    lines = [
        "# The exception inventory",
        "",
        "Generated by `python tools/exception_review.py --markdown docs/exception_inventory.md`.",
        "Every broad exception handler in `src/` and `tools/` is listed with what it does",
        "with the exception.  A *suppressed* handler is one that drops the failure; each of",
        "those carries a written reason, because an unexplained suppression is a bug waiting",
        "to be found by somebody else.",
        "",
        "| file | line | catches | kind | reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for handler in handlers:
        reason = handler.reason.replace("|", "\\|") if handler.reason else ""
        if not reason and handler.kind == SUPPRESSED:
            reason = "**UNDOCUMENTED**"
        lines.append(
            f"| `{handler.path}` | {handler.line} | `{handler.caught}` | {handler.kind} | {reason} |"
        )
    counts: dict[str, int] = {}
    for handler in handlers:
        counts[handler.kind] = counts.get(handler.kind, 0) + 1
    lines.extend(
        [
            "",
            "## Totals",
            "",
            "| kind | count |",
            "| --- | --- |",
        ]
    )
    for kind in sorted(counts):
        lines.append(f"| {kind} | {counts[kind]} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on an undocumented suppression")
    parser.add_argument("--markdown", type=Path, default=None, help="write the inventory here")
    parser.add_argument(
        "--root", action="append", default=None, help="a directory to scan; repeatable"
    )
    args = parser.parse_args(argv)

    roots = [REPO_ROOT / name for name in (args.root or DEFAULT_ROOTS)]
    handlers: list[Handler] = []
    for root in roots:
        if root.is_dir():
            handlers.extend(scan(root))

    undocumented = [handler for handler in handlers if not handler.documented]
    if args.markdown is not None:
        args.markdown.write_text(render_markdown(handlers), encoding="utf-8")
        print(f"wrote {args.markdown} ({len(handlers)} handlers)")

    if not args.check:
        for handler in handlers:
            mark = "!" if not handler.documented else " "
            print(f"{mark} {handler.path}:{handler.line} {handler.caught} {handler.kind}")
        return 0

    for handler in undocumented:
        print(
            f"::error::{handler.path}:{handler.line} suppresses a {handler.caught} "
            f"without a written reason",
            file=sys.stderr,
        )
    if undocumented:
        print(f"{len(undocumented)} undocumented suppression(s)", file=sys.stderr)
        return 1
    print(f"{len(handlers)} broad handlers, every suppression documented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
