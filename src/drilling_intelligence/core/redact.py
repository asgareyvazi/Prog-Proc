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
_CREDENTIAL_ASSIGN = re.compile(
    rf"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_-])*(?:{_CRED_TERM}|{_CONN_TERM})\s*[=:]\s*\S+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
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


def _redact_assignment(match: re.Match[str]) -> str:
    head, sep, _value = re.split(r"(\s*[=:]\s*)", match.group(0), maxsplit=1)
    return f"{head}{sep}[redacted]"


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
    raw = _CREDENTIAL_ASSIGN.sub(_redact_assignment, raw)
    raw = _URL_USERINFO.sub(r"\1[redacted]@", raw)
    raw = _BEARER.sub("Bearer [redacted]", raw)
    raw = _BASIC.sub(r"\1[redacted]", raw)
    raw = _QUOTED_PATH.sub(lambda match: _path_leaf(match.group(0)), raw)
    return _redact_paths(raw)


def visible_diagnostic(exc: BaseException) -> str:
    """The operator-facing form of an exception. No traceback and no host path."""
    text = " ".join(safe_detail(str(exc) or "").split())
    return text or type(exc).__name__
