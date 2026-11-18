"""Path and secret redaction for operator-facing diagnostics.

Stdlib only. The workbench imports this module, not the citation auditor, so a
status message does not load retrieval while a window is alive.
"""

from __future__ import annotations

import re

# A path segment may contain spaces when another separator follows. The final
# segment does not swallow the rest of the sentence.
_SEGMENT = r"[^\s/\\'\"<>|]+(?: [^\s/\\'\"<>|]+)*"
# A final filename may contain spaces. Requiring an extension stops the match
# before the next sentence word. A single token still matches a path with no extension.
_LEAF = (
    r"(?:(?:[^\s/\\'\"<>|,;:)]+ )*[^\s/\\'\"<>|,;:)]+\.[A-Za-z0-9]{1,8}"
    r"|[^\s/\\'\"<>|,;:)]+)"
)
_PATH = re.compile(
    r"(?i)"
    r"(?:file://+[^\s\"'<>|]+"
    rf"|\\\\(?:{_SEGMENT}\\)+{_LEAF}"
    rf"|[A-Za-z]:[\\/](?:{_SEGMENT}[\\/])+{_LEAF}"
    rf"|/(?:{_SEGMENT}/)+{_LEAF}"
    rf"|(?:\.\.[\\/])+(?:{_SEGMENT}[\\/])*{_LEAF})"
)
_QUOTED_PATH = re.compile(
    r"""(?x)
    ["']
    (?:[A-Za-z]:[\\/]|\\\\|\.\.[\\/]|/)
    [^"'\n]+
    ["']
    """
)
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
# A credential term may stand alone or be the suffix of an env-style name
# (OPENAI_API_KEY, refresh_token, POSTGRES_PASSWORD). A letter glued to the
# term, as in notasecret, is not an assignment.
_CRED_TERM = r"(?:password|passwd|pwd|api[_-]?key|token|secret|access[_-]?key)"
_CONN_TERM = r"(?:database[_-]url|connection[_-](?:string|url)|[a-z0-9]+[_-]dsn)"
# An optional quote covers ``"password": "value"`` without treating the quote as
# the start of the value. Quoted values may contain spaces. Unquoted values stop
# at whitespace, ``&``, and ``;`` so the next sentence is not swallowed. A
# single-line JSON object or array is consumed only up to its closer.
_ASSIGN_HEAD = re.compile(
    rf"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-])*(?:{_CRED_TERM}|{_CONN_TERM})"
    rf"\s*[\"']?\s*[=:]\s*)"
)
_VALUE_LIMIT = 400
_BEARER = re.compile(r"(?i)\bBearer\s+[\"']?[A-Za-z0-9._\-]{8,}[\"']?")
_BASIC = re.compile(r"(?i)\b(authorization\s*:\s*basic\s+)\S+")
# A path token can begin at the second slash of scheme://, so the prefix is
# "scheme:/" rather than "scheme:". file: remains a path, not a connection URL.
_SCHEME_BEFORE = re.compile(r"(?i)([a-z][a-z0-9+.-]*):/?$")
_FILE_LINE = re.compile(r'^File ".+", line \d+')


def _strip_traceback(text: str) -> str:
    """Drop traceback frames. The exception line, if any, is kept for redaction."""
    lines: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Traceback (most recent call last)"):
            lines.append("[traceback omitted]")
            skipping = True
            continue
        if _FILE_LINE.match(stripped):
            lines.append("[traceback omitted]")
            skipping = True
            continue
        if skipping and (line.startswith((" ", "\t")) or stripped.startswith("File ")):
            continue
        skipping = False
        lines.append(line)
    return "\n".join(lines)


def _path_leaf(token: str) -> str:
    cleaned = token.strip("\"'").replace("\\", "/")
    if cleaned.lower().startswith("file:"):
        cleaned = cleaned.split("://", 1)[-1]
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    leaf = cleaned.rstrip("/").split("/")[-1]
    return leaf or "[path]"


def _quoted_value_end(text: str, start: int) -> int | None:
    quote = text[start]
    if quote not in "\"'":
        return None
    index = start + 1
    limit = min(len(text), start + _VALUE_LIMIT)
    while index < limit:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            return None
        if char == quote:
            return index + 1
        index += 1
    return None


def _container_value_end(text: str, start: int) -> int | None:
    """A single-line JSON object or array, including one level of nesting.

    The scan stops at a newline or at ``_VALUE_LIMIT`` so a missing closer
    cannot consume the next sentence. An unbalanced value is left for the
    unquoted rule, which stops at whitespace.
    """
    opener = text[start]
    if opener not in "{[":
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    index = start
    limit = min(len(text), start + _VALUE_LIMIT)
    while index < limit:
        char = text[index]
        if char == "\n":
            return None
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index + 1 if char == closer else None
            if depth < 0:
                return None
        index += 1
    return None


def _unquoted_value_end(text: str, start: int) -> int:
    index = start
    limit = min(len(text), start + _VALUE_LIMIT)
    while index < limit and text[index] not in " \t\r\n&;":
        index += 1
    return index


def _assignment_value_end(text: str, start: int) -> int:
    if start >= len(text):
        return start
    quoted = _quoted_value_end(text, start)
    if quoted is not None:
        return quoted
    container = _container_value_end(text, start)
    if container is not None:
        return container
    return _unquoted_value_end(text, start)


def _redact_assignments(text: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _ASSIGN_HEAD.finditer(text):
        if match.start() < cursor:
            continue
        value_end = _assignment_value_end(text, match.end())
        if value_end <= match.end():
            continue
        pieces.append(text[cursor : match.start()])
        pieces.append(match.group(1) + "[redacted]")
        cursor = value_end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _redact_paths(text: str) -> str:
    """Replace host paths, but do not consume the remainder of a scheme:// URL.

    Userinfo is removed before this runs. Eating ``postgresql://[redacted]@host/db``
    as a filesystem path would hide the host as well as the password.
    """

    def repl(match: re.Match[str]) -> str:
        prefix = text[max(0, match.start() - 24) : match.start()]
        scheme = _SCHEME_BEFORE.search(prefix)
        if scheme is not None and scheme.group(1).lower() != "file":
            return match.group(0)
        return _path_leaf(match.group(0))

    return _PATH.sub(repl, text)


def safe_detail(text: str) -> str:
    """Keep a useful diagnostic and remove host paths, tracebacks, and secrets.

    A filename may remain. A directory, connection password, token, or traceback
    frame may not. Prefixed environment names such as ``OPENAI_API_KEY`` are
    assignments, not ordinary words. Relative paths and paths that contain
    spaces are included.
    """
    raw = _strip_traceback(str(text or ""))
    raw = _redact_assignments(raw)
    raw = _URL_USERINFO.sub(r"\1[redacted]@", raw)
    raw = _BEARER.sub("Bearer [redacted]", raw)
    raw = _BASIC.sub(r"\1[redacted]", raw)
    raw = _QUOTED_PATH.sub(lambda match: _path_leaf(match.group(0)), raw)
    return _redact_paths(raw)


def visible_diagnostic(exc: BaseException) -> str:
    """The operator-facing form of an exception. No traceback and no host path.

    A diagnostic that raises is worse than a diagnostic that says less: this
    runs inside error handling, where the caller has no second chance.  An
    exception whose ``__str__`` itself fails - a third-party type, a broken
    ``__repr__``, an object holding a closed stream - reports its type name
    instead.
    """
    try:
        detail = str(exc) or ""
    except Exception:  # noqa: BLE001 - a diagnostic may not raise, by definition
        detail = ""
    text = " ".join(safe_detail(detail).split())
    return text or type(exc).__name__
