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
_CREDENTIAL_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|token|secret|access[_-]?key)\s*[=:]\s*\S+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
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


def safe_detail(text: str) -> str:
    """Keep a useful diagnostic and remove host paths, tracebacks, and secrets.

    A filename may remain. A directory, connection password, token, or traceback
    frame may not. Relative paths and paths that contain spaces are included.
    """
    raw = _strip_traceback(str(text or ""))
    raw = _URL_USERINFO.sub(r"\1[redacted]@", raw)
    raw = _CREDENTIAL_ASSIGN.sub(r"\1=[redacted]", raw)
    raw = _BEARER.sub("Bearer [redacted]", raw)
    raw = _QUOTED_PATH.sub(lambda match: _path_leaf(match.group(0)), raw)
    return _PATH.sub(lambda match: _path_leaf(match.group(0)), raw)


def visible_diagnostic(exc: BaseException) -> str:
    """The operator-facing form of an exception. No traceback and no host path."""
    text = " ".join(safe_detail(str(exc) or "").split())
    return text or type(exc).__name__
