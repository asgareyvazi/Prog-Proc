"""The exception policy, and the other quiet failures the review looked for.

This file is the durable half of the V8.17 exception and security review.  The
review itself is a reading of the tree; the rules it ended with are here, so
that a later change cannot quietly break one of them:

* a blind ``except Exception`` that drops the failure must say why;
* a bare ``except:`` or ``except BaseException:`` is refused outright;
* every ``type: ignore`` names the error it silences;
* the redaction of a secret still happens where a message is written;
* no operational data - a workspace, a database, a log - is tracked in Git.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs" / "exception_inventory.md"

_SPEC = importlib.util.spec_from_file_location(
    "exception_review", ROOT / "tools" / "exception_review.py"
)
assert _SPEC is not None and _SPEC.loader is not None
exception_review = importlib.util.module_from_spec(_SPEC)
# A dataclass resolves its own module through sys.modules, so the module has
# to be registered before it is executed.
sys.modules[_SPEC.name] = exception_review
_SPEC.loader.exec_module(exception_review)


def _sources() -> list[Path]:
    paths: list[Path] = []
    for name in ("src", "tools"):
        paths.extend(
            path for path in (ROOT / name).rglob("*.py") if "__pycache__" not in path.parts
        )
    return sorted(paths)


def _handlers() -> list[exception_review.Handler]:
    handlers: list[exception_review.Handler] = []
    for name in ("src", "tools"):
        handlers.extend(exception_review.scan(ROOT / name))
    return handlers


def test_no_broad_handler_drops_a_failure_without_saying_why() -> None:
    undocumented = [handler for handler in _handlers() if not handler.documented]
    assert not undocumented, [
        f"{handler.path}:{handler.line} suppresses {handler.caught} with no reason"
        for handler in undocumented
    ]


def test_a_bare_except_or_a_base_exception_is_refused() -> None:
    """``except:`` also catches KeyboardInterrupt and SystemExit.

    A shutdown path that swallows those cannot be stopped, so the repository
    does not allow one at all.
    """
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                offenders.append(f"{path}:{node.lineno} bare except")
                continue
            names = (
                [node.type.id]
                if isinstance(node.type, ast.Name)
                else [ast.unparse(node.type)]
                if isinstance(node.type, ast.Tuple)
                else []
            )
            if any("BaseException" in name for name in names):
                offenders.append(f"{path}:{node.lineno} except BaseException")
    assert not offenders, offenders


def test_the_inventory_on_disk_matches_the_tree() -> None:
    """The document is generated, and a generated document that lies is worse
    than no document."""
    assert INVENTORY.exists(), "the exception inventory has not been generated"
    expected = exception_review.render_markdown(_handlers())
    assert INVENTORY.read_text(encoding="utf-8") == expected


def test_every_type_ignore_names_the_error_it_silences() -> None:
    """A blanket ``# type: ignore`` silences every future error as well."""
    offenders: list[str] = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "type: ignore" not in line:
                continue
            marker = line.split("type: ignore", 1)[1]
            if not marker.startswith("["):
                offenders.append(f"{path}:{number} {line.strip()}")
    assert not offenders, offenders


def test_a_secret_is_still_redacted_where_a_message_is_written() -> None:
    from drilling_intelligence.core.redact import safe_detail

    message = safe_detail("calling https://user:secret@example.invalid/x?api_key=abc123")
    assert "secret" not in message
    assert "abc123" not in message


def test_the_structured_logger_does_not_write_a_secret(tmp_path: Path) -> None:
    """The redaction happens where the record is written, not where it is made."""
    from drilling_intelligence.core.logging import REDACTED, configure_logging, get_logger

    target = tmp_path / "events.jsonl"
    configure_logging(file=target, format="json", force=True)
    get_logger("test").event("provider.checked", api_key="sk-not-a-real-key")
    written = target.read_text(encoding="utf-8")
    assert written.strip(), "nothing was written"
    assert "sk-not-a-real-key" not in written
    assert REDACTED in written


def test_a_token_looking_value_is_masked_wherever_it_appears() -> None:
    """The token patterns must not re-emit the token they matched."""
    import logging

    from drilling_intelligence.core.logging import REDACTED, _JsonFormatter

    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "calling with sk-abcdefgh1234", (), None
    )
    line = _JsonFormatter(("api_key", "token", "password", "secret")).format(record)
    assert "sk-abcdefgh1234" not in line
    assert REDACTED in line


def test_no_operational_data_is_tracked_in_git() -> None:
    """A workspace, a database or a log must never be committed."""
    import shutil

    git = shutil.which("git")
    if git is None:  # pragma: no cover - no git in this environment
        import pytest

        pytest.skip("git is not available in this environment")
    try:
        tracked = subprocess.run(
            [git, "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git here
        import pytest

        pytest.skip("git is not available in this environment")
    offenders = [
        path
        for path in tracked.split("\0")
        if path
        and (
            path.endswith((".db", ".sqlite", ".sqlite3", ".log"))
            or ".drillintel/" in path
            or path.startswith("data/")
        )
    ]
    assert not offenders, offenders
