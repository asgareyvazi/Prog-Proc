"""Standalone HTML for an engineering report.

The file is self-contained: CSS and SVG are inline, and nothing is loaded from
the network. Every source-derived string is escaped. The output path and the
clock are not written into the document.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from .contract import RENDERER_VERSION, limitation_text
from .format import format_value, plain_text
from .svg import render_svg

_CSS = """
:root { color-scheme: light; }
body {
  margin: 0;
  padding: 28px 32px 64px;
  background: #f7f6f3;
  color: #1c2430;
  font: 15px/1.45 "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif;
}
header, section, footer { max-width: 1100px; margin: 0 auto 28px; }
h1 { font-size: 1.6rem; font-weight: 600; margin: 0 0 8px; }
h2 { font-size: 1.15rem; font-weight: 600; margin: 0 0 8px; }
h3 { font-size: 1rem; font-weight: 600; margin: 16px 0 6px; }
p, li { margin: 0 0 8px; }
.meta, .state, .caption, .note { color: #3d4754; }
nav ol { display: flex; flex-wrap: wrap; gap: 8px 14px; padding: 0; list-style: none; }
nav a { color: #1c2430; }
.table-wrap { overflow-x: auto; margin: 8px 0 16px; }
table { border-collapse: collapse; min-width: 100%; background: #fff; }
th, td {
  border: 1px solid #d5d1c8;
  padding: 6px 8px;
  text-align: left;
  vertical-align: top;
  white-space: nowrap;
}
th { background: #eceae4; font-weight: 600; }
figure {
  margin: 0 0 22px;
  padding: 12px;
  background: #fff;
  border: 1px solid #d5d1c8;
}
svg { max-width: 100%; height: auto; }
footer { border-top: 1px solid #d5d1c8; padding-top: 12px; color: #3d4754; }
@media (max-width: 720px) {
  body { padding: 12px; }
}
@media print {
  nav { display: none; }
  body { background: #fff; padding: 0; }
  section, figure, table { break-inside: avoid; }
}
"""


def _esc(value: Any) -> str:
    return plain_text("" if value is None else value)


def _anchor(section_id: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(section_id))
    return f"sec-{slug}"


def _cell_text(value: Any) -> str:
    if (
        isinstance(value, Mapping)
        and "value" in value
        and ("value_state" in value or "unit" in value)
    ):
        rendered = format_value(value.get("value"), value.get("unit"))
        state = value.get("value_state")
        if state:
            return f"{rendered} ({state})"
        return rendered
    if isinstance(value, Mapping):
        parts = [
            f"{key}={format_value(item) if not isinstance(item, Mapping) else item}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        ]
        return "; ".join(str(part) for part in parts)
    if isinstance(value, (list, tuple)):
        return ", ".join(
            format_value(item) if not isinstance(item, (list, tuple, Mapping)) else str(item)
            for item in value
        )
    return format_value(value)


def _table_html(table: Mapping[str, Any]) -> str:
    columns = list(table.get("columns") or [])
    labels = table.get("column_labels") or {}
    head = "".join(f"<th>{_esc(labels.get(column, column))}</th>" for column in columns)
    body = []
    for row in table.get("rows") or []:
        cells = "".join(f"<td>{_esc(_cell_text(row.get(column)))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    note = f'<p class="note">{_esc(table.get("note"))}</p>' if table.get("note") else ""
    return (
        f"<h3>{_esc(table.get('title'))}</h3>\n"
        f"{note}\n"
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def _fallback_table(exhibit: Mapping[str, Any]) -> str:
    rows = []
    for series in exhibit.get("series") or []:
        for point in series.get("points") or []:
            rows.append(
                {
                    "subject": point.get("name"),
                    "series": series.get("label"),
                    "value": format_value(point.get("raw_value"), point.get("unit")),
                    "value_state": point.get("value_state"),
                    "comparability": point.get("comparability"),
                    "missing": point.get("missing"),
                    "plottable": point.get("plottable"),
                }
            )
    if not rows:
        rows.append(
            {
                "subject": "—",
                "series": "—",
                "value": "—",
                "value_state": exhibit.get("state"),
                "comparability": "",
                "missing": True,
                "plottable": False,
            }
        )
    return _table_html(
        {
            "title": "Table fallback",
            "note": "The same exhibit values, as text. A chart does not replace this table.",
            "columns": (
                "subject",
                "series",
                "value",
                "value_state",
                "comparability",
                "missing",
                "plottable",
            ),
            "rows": rows,
        }
    )


def _exhibit_html(exhibit: Mapping[str, Any]) -> str:
    svg = render_svg(exhibit).rstrip("\n")
    exhibit_id = "".join(
        ch if ch.isalnum() or ch in "-_" else "-"
        for ch in str(exhibit.get("exhibit_id") or "exhibit")
    )
    return (
        f'<figure id="fig-{exhibit_id}">\n'
        f"<figcaption>{_esc(exhibit.get('title'))} — {_esc(exhibit.get('state'))}</figcaption>\n"
        f"{svg}\n"
        f'<p class="caption">{_esc(exhibit.get("caption"))}</p>\n'
        f'<p class="state">Chart state: {_esc(exhibit.get("state"))}. {_esc(exhibit.get("reason"))}</p>\n'
        f"{_fallback_table(exhibit)}\n"
        f"</figure>"
    )


def _section_html(section: Mapping[str, Any], exhibits: Mapping[str, Mapping[str, Any]]) -> str:
    parts = [
        f'<section id="{_anchor(section.get("section_id"))}">',
        f"<h2>{_esc(section.get('title'))}</h2>",
        f'<p class="state">State: {_esc(section.get("state"))}</p>',
    ]
    if section.get("note"):
        parts.append(f'<p class="note">{_esc(section.get("note"))}</p>')
    for table in section.get("tables") or []:
        parts.append(_table_html(table))
    for exhibit_id in section.get("exhibits") or []:
        exhibit = exhibits.get(str(exhibit_id))
        if exhibit is None:
            parts.append(f'<p class="state">Exhibit {_esc(exhibit_id)}: MISSING</p>')
            continue
        parts.append(_exhibit_html(exhibit))
    parts.append("</section>")
    return "\n".join(parts)


def render_html(pack: Mapping[str, Any] | Any) -> str:
    """HTML document for one report. Byte-stable for the same pack and renderer version."""
    payload = pack.to_dict() if hasattr(pack, "to_dict") else dict(pack)
    exhibits = {str(item.get("exhibit_id")): item for item in payload.get("exhibits") or []}
    subject = payload.get("subject") or {}
    window = subject.get("window") or {}
    nav = []
    body = []
    for section in payload.get("sections") or []:
        section_id = str(section.get("section_id") or "")
        nav.append(f'<li><a href="#{_anchor(section_id)}">{_esc(section.get("title"))}</a></li>')
        if section_id == "limitations":
            items = "".join(
                f"<li><code>{_esc(code)}</code> — {_esc(limitation_text(str(code)))}</li>"
                for code in payload.get("limitations") or []
            )
            body.append(
                "\n".join(
                    [
                        f'<section id="{_anchor(section_id)}">',
                        "<h2>Limitations</h2>",
                        f'<p class="note">{_esc(section.get("note"))}</p>',
                        f"<ul>{items}</ul>",
                        "</section>",
                    ]
                )
            )
            continue
        body.append(_section_html(section, exhibits))
    observations = "".join(f"<li>{_esc(line)}</li>" for line in payload.get("observations") or [])
    source = ", ".join(
        f"{item.get('schema')} {item.get('identity')}" for item in payload.get("source_packs") or []
    )
    window_line = ""
    if window:
        if window.get("applied"):
            window_line = (
                f"Date window: {_esc(window.get('since') or '...')} to "
                f"{_esc(window.get('until') or '...')}. "
                f"Windowed domains: {_esc(', '.join(window.get('windowed_domains') or []))}. "
                f"{_esc(window.get('undated_behavior'))}"
            )
        else:
            window_line = "Date window: none (current state)."
    document = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(payload.get('title'))}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<header>",
        f"<h1>{_esc(payload.get('title'))}</h1>",
        f'<p class="meta">Schema: {_esc(payload.get("schema"))}</p>',
        f'<p class="meta">Report identity: {_esc(payload.get("identity"))}</p>',
        f'<p class="meta">Mode: {_esc(payload.get("mode"))}. Basis: {_esc(subject.get("basis_kind") or subject.get("kind"))}.</p>',
        f'<p class="meta">{window_line}</p>' if window_line else "",
        f'<p class="meta">Source pack: {_esc(source)}</p>',
        '<p class="note">This document does not require JavaScript and does not load remote resources.</p>',
        "</header>",
        f"<nav><ol>{''.join(nav)}</ol></nav>",
        "<main>",
        *body,
        "<section>",
        "<h2>Observations</h2>",
        f"<ul>{observations or '<li>None recorded by the source pack.</li>'}</ul>",
        '<p class="note">Observations are the source pack\'s sentences, copied, not a second summary.</p>',
        "</section>",
        "</main>",
        "<footer>",
        f"<p>Schema {_esc(payload.get('schema'))}. Identity {_esc(payload.get('identity'))}.</p>",
        f"<p>Renderer {RENDERER_VERSION}. Freshness: {_esc(_cell_text(payload.get('freshness') or {}))}.</p>",
        "<p>No generation timestamp is part of this document. The output path is not part of the identity.</p>",
        "</footer>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(line for line in document if line is not None)


def write_report_html(path: str | Path, pack: Mapping[str, Any] | Any) -> None:
    """Write UTF-8 HTML to exactly ``path``. A missing directory is an error, not a fallback."""
    target = Path(path)
    if not str(path).strip():
        raise ValidationError("an output path is required", hint="pass --output report.html")
    if target.exists() and target.is_dir():
        raise ValidationError(
            f"output path is a directory: {target}",
            hint="pass a file path, not a directory",
        )
    parent = target.parent
    if str(parent) not in ("", ".") and not parent.exists():
        raise ValidationError(
            f"output directory does not exist: {parent}",
            hint="create the directory, or choose a path that exists",
        )
    text = render_html(pack)
    target.write_text(text, encoding="utf-8", newline="\n")
