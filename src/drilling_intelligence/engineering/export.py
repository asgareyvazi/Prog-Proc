"""Controlled export of Program/Procedure revisions to JSON, HTML, DOCX and PDF.

Every exported artifact carries the record's exact lifecycle status and revision. A DRAFT
revision is watermarked ``DRAFT - NOT APPROVED`` on every format; an approved revision names
the exact approver attribution and timestamp recorded on the record. Export is read-only
against the workspace and publishes race-safe: the destination is claimed atomically with
``O_CREAT|O_EXCL`` semantics (hard-link publication), so a concurrent creator can never be
silently overwritten.

Full fidelity is the default. An opt-in ``redact=True`` produces a copy in which only
schema-defined local path values (Windows drive paths, UNC paths, POSIX absolute paths,
``file://`` URIs) are masked under a recorded policy version; canonical records are never
mutated. Values are always rendered with their explicitly stored unit - never a hard-coded
display unit.

Actor strings are declared attributions, not authentication; the export says so on its face.
"""

from __future__ import annotations

import errno
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError

FORMATS = ("json", "html", "docx", "pdf")

_WATERMARK = "DRAFT - NOT APPROVED"

#: Version of the opt-in source-path redaction policy applied to redacted exports.
REDACTION_POLICY_VERSION = "path-redaction-v1"

_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]"),  # Windows drive path
    re.compile(r"^\\\\[^\\]+\\"),  # UNC path
    re.compile(r"^file://", re.IGNORECASE),  # file URI
    re.compile(r"^/(?:[^\s/]+/)*[^\s]*$"),  # POSIX absolute path
)

_REDACTED = "[REDACTED: local path]"


def _is_local_path(value: str) -> bool:
    """True only for strings that are unambiguously local filesystem paths."""
    stripped = value.strip()
    if not stripped or len(stripped) > 4096:
        return False
    return any(pattern.match(stripped) for pattern in _PATH_PATTERNS)


def _redact_paths(value: Any) -> Any:
    """Recursively mask local path values inside provenance-style structures.

    Only strings that are unambiguous local paths are masked; ordinary engineering text
    containing a slash or colon is preserved verbatim.
    """
    if isinstance(value, str):
        return _REDACTED if _is_local_path(value) else value
    if isinstance(value, list):
        return [_redact_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_paths(item) for key, item in value.items()}
    return value


def _fmt(value: Any, unit: Any) -> str:
    """Render a value with its explicitly stored unit; never invent or convert one."""
    if value is None or value == "":
        return "-"
    if unit is None or str(unit).strip() == "":
        return f"{value} [unit missing]"
    return f"{value} {unit}"


def _status_line(record: Any) -> str:
    status = str(getattr(record, "status", "") or "").upper()
    revision = getattr(record, "revision", None)
    label = f"Revision {revision}" if revision is not None else "Revision"
    if status == "APPROVED":
        approver = getattr(record, "approver", None) or getattr(record, "approved_by", None)
        approved_at = getattr(record, "approved_at", None)
        who = f" by {approver}" if approver else ""
        when = f" at {approved_at.isoformat()}" if approved_at else ""
        return f"{label} — APPROVED{who}{when} (declared attribution, not authentication)"
    return f"{label} — {_WATERMARK}"


def _record_payload(service: Any, kind: str, record_id: str, *, redact: bool) -> dict[str, Any]:
    """Build the complete, structured payload for one exact revision."""
    if kind == "program":
        record = service.repository.get_program(record_id)
        sections = service.repository.list_program_sections(record_id)
        target_rows = service.repository.list_targets(record_id)
        payload: dict[str, Any] = {
            "record_type": "program",
            "id": record.id,
            "code": record.code,
            "title": record.title,
            "revision": record.revision,
            "revision_label": record.revision_label,
            "is_current": record.is_current,
            "status": record.status,
            "summary": record.summary,
            "well_id": record.well_id,
            "author": record.author,
            "created_by": record.created_by,
            "status_note": record.status_note,
            "sections": [
                {
                    "id": s.id,
                    "sequence": s.sequence,
                    "section_key": s.section_key,
                    "title": s.title,
                    "content": s.content,
                    "completion_state": s.completion_state,
                    "source_reference": s.source_reference,
                    "provenance": s.provenance,
                    "origin": s.origin,
                }
                for s in sections
            ],
            "targets": [
                {
                    "id": t.id,
                    "sequence": t.sequence,
                    "name": t.name,
                    "hole_size_in": t.hole_size_in,
                    "casing_program": t.casing_program,
                    "formation_top": t.formation_top,
                    "planned_depth_md_value": t.planned_depth_md_value,
                    "planned_depth_md_unit": t.planned_depth_md_unit,
                    "planned_duration_days": t.planned_duration_days,
                    "planned_mud_weight_value": t.planned_mud_weight_value,
                    "planned_mud_weight_unit": t.planned_mud_weight_unit,
                    "planned_npt_hours": t.planned_npt_hours,
                    "planned_cost_value": t.planned_cost_value,
                    "planned_cost_unit": t.planned_cost_unit,
                    "provenance": t.provenance,
                    "origin": t.origin,
                }
                for t in target_rows
            ],
        }
    elif kind == "procedure":
        record = service.repository.get_procedure(record_id)
        steps = service.repository.list_procedure_steps(record_id)
        payload = {
            "record_type": "procedure",
            "id": record.id,
            "code": record.code,
            "title": record.title,
            "procedure_type": record.procedure_type,
            "revision": record.revision,
            "revision_label": record.revision_label,
            "is_current": record.is_current,
            "status": record.status,
            "description": record.description,
            "well_id": record.well_id,
            "created_by": record.created_by,
            "source_reference": record.source_reference,
            "status_note": record.status_note,
            "program_link": service.procedure_program_link(record_id),
            "steps": [
                {
                    "id": s.id,
                    "sequence": s.sequence,
                    "title": s.title,
                    "instructions": s.instructions,
                    "responsible_role": s.responsible_role,
                    "equipment": s.equipment,
                    "preconditions": s.preconditions,
                    "parameters": s.parameters,
                    "expected_result": s.expected_result,
                    "acceptance_criteria": s.acceptance_criteria,
                    "evidence_required": s.evidence_required,
                    "hold_point": s.hold_point,
                    "safety_controls": s.safety_controls,
                    "contingency": s.contingency,
                    "source_reference": s.source_reference,
                    "provenance": s.provenance,
                }
                for s in steps
            ],
        }
    else:
        raise ValidationError("record_type must be 'program' or 'procedure'")
    if redact:
        # Redact only schema-defined path-bearing structures; engineering text is untouched.
        payload = _redact_paths(payload)
        payload["redaction"] = {"applied": True, "policy": REDACTION_POLICY_VERSION}
    else:
        payload["redaction"] = {"applied": False, "policy": REDACTION_POLICY_VERSION}
    payload["export_notice"] = _status_line(record)
    payload["attribution_notice"] = (
        "Actor strings are declared attributions, not authenticated identities. "
        "This document is not an engineering certification."
    )
    return payload


_TARGET_COLUMNS = (
    ("name", "Section"),
    ("hole_size_in", "Hole size (in)"),
    ("casing_program", "Casing program"),
    ("formation_top", "Formation top"),
    ("planned_depth_md_value", "Planned depth"),
    ("planned_depth_md_unit", "Depth unit"),
    ("planned_duration_days", "Duration (days)"),
    ("planned_mud_weight_value", "Mud weight"),
    ("planned_mud_weight_unit", "MW unit"),
    ("planned_npt_hours", "NPT allowance (h)"),
    ("planned_cost_value", "Planned cost"),
    ("planned_cost_unit", "Currency/unit"),
)


def _render_html(payload: dict[str, Any]) -> str:
    esc = html.escape
    status = str(payload.get("status", "")).upper()
    watermark = _WATERMARK if status != "APPROVED" else "APPROVED"
    redaction = payload.get("redaction", {})
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{esc(str(payload.get('title', '')))}</title>",
        "<style>body{font-family:sans-serif;margin:2em}"
        ".watermark{color:#b00;font-weight:bold;border:2px solid #b00;"
        "padding:.4em;display:inline-block;margin:.6em 0}"
        "table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px 8px}"
        "h2{margin-top:1.4em}</style></head><body>",
        f"<h1>{esc(str(payload.get('title', '')))}</h1>",
        f"<p class='watermark'>{esc(watermark)}</p>",
        f"<p>{esc(str(payload.get('export_notice', '')))}</p>",
    ]
    if redaction.get("applied"):
        parts.append(
            f"<p><i>Redacted export (policy {esc(str(redaction.get('policy', '')))}): "
            "local source paths masked.</i></p>"
        )
    if payload.get("record_type") == "program":
        parts.append(f"<p>{esc(str(payload.get('summary', '')))}</p>")
        parts.append("<h2>Sections</h2><ol>")
        for s in payload.get("sections", []):
            parts.append(
                f"<li><b>{esc(str(s.get('title', '')))}</b> "
                f"[{esc(str(s.get('completion_state', '')))}]"
                + (
                    f" <i>source: {esc(str(s.get('source_reference') or ''))}</i>"
                    if s.get("source_reference")
                    else ""
                )
                + f"<p>{esc(str(s.get('content', '')))}</p></li>"
            )
        parts.append("</ol><h2>Planned targets</h2>")
        targets = payload.get("targets", [])
        if targets:
            head = "".join(f"<th>{esc(label)}</th>" for _key, label in _TARGET_COLUMNS)
            parts.append(f"<table><tr><th>#</th>{head}</tr>")
            for t in targets:
                cells = "".join(
                    f"<td>{esc(str(t.get(key) if t.get(key) is not None else '-'))}</td>"
                    for key, _label in _TARGET_COLUMNS
                )
                parts.append(f"<tr><td>{esc(str(t.get('sequence', '')))}</td>{cells}</tr>")
            parts.append("</table>")
        else:
            parts.append("<p>No planned targets recorded.</p>")
    else:
        parts.append(f"<p>{esc(str(payload.get('description', '')))}</p>")
        link = payload.get("program_link", {})
        parts.append(
            f"<p>Program link: {esc(str(link.get('status', 'NONE')))}"
            + (
                f" (Program revision {esc(str(link.get('program_revision_id', '')))})"
                if link.get("program_revision_id")
                else ""
            )
            + "</p>"
        )
        parts.append("<h2>Steps</h2><ol>")
        for s in payload.get("steps", []):
            hold = " <b>[HOLD POINT]</b>" if s.get("hold_point") else ""
            parts.append(f"<li><b>{esc(str(s.get('title', '')))}</b>{hold}")
            if s.get("instructions"):
                parts.append(f"<p>{esc(str(s.get('instructions')))}</p>")
            if s.get("responsible_role"):
                parts.append(f"<p>Responsible: {esc(str(s.get('responsible_role')))}</p>")
            if s.get("acceptance_criteria"):
                parts.append(f"<p>Acceptance: {esc(str(s.get('acceptance_criteria')))}</p>")
            if s.get("source_reference"):
                parts.append(f"<p><i>source: {esc(str(s.get('source_reference')))}</i></p>")
            parts.append("</li>")
        parts.append("</ol>")
    parts.append(f"<p><small>{esc(str(payload.get('attribution_notice', '')))}</small></p>")
    parts.append("</body></html>")
    return "".join(parts)


def _render_docx(payload: dict[str, Any], path: Path) -> None:
    from docx import Document  # python-docx, pinned in constraints
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = Document()
    for style_name in ("Normal",):
        style = doc.styles[style_name]
        style.font.name = "Calibri"
        style.font.size = Pt(10.5)
    status = str(payload.get("status", "")).upper()
    watermark = _WATERMARK if status != "APPROVED" else "APPROVED"

    # Cover / document control
    doc.add_heading(str(payload.get("title", "")), level=0)
    notice = doc.add_paragraph()
    run = notice.add_run(watermark)
    run.bold = True
    run.font.color.rgb = (
        RGBColor(0xB0, 0x00, 0x00) if status != "APPROVED" else RGBColor(0x00, 0x60, 0x00)
    )
    doc.add_paragraph(str(payload.get("export_notice", "")))
    control = doc.add_table(rows=0, cols=2)
    control.style = "Table Grid"
    for key, label in (
        ("id", "Record ID"),
        ("code", "Code"),
        ("revision", "Revision"),
        ("status", "Status"),
        ("is_current", "Current"),
        ("well_id", "Well ID"),
    ):
        row = control.add_row().cells
        row[0].text = label
        row[1].text = str(payload.get(key, "") or "-")
    redaction = payload.get("redaction", {})
    if redaction.get("applied"):
        doc.add_paragraph(
            f"Redacted export (policy {redaction.get('policy', '')}): local source paths masked."
        )

    if payload.get("record_type") == "program":
        doc.add_heading("Summary", level=1)
        doc.add_paragraph(str(payload.get("summary", "")))
        doc.add_heading("Sections", level=1)
        for s in payload.get("sections", []):
            doc.add_heading(f"{s.get('sequence')}. {s.get('title')}", level=2)
            doc.add_paragraph(f"State: {s.get('completion_state')}")
            if s.get("source_reference"):
                doc.add_paragraph(f"Source: {s.get('source_reference')}")
            doc.add_paragraph(str(s.get("content", "")))
        doc.add_heading("Planned targets", level=1)
        targets = payload.get("targets", [])
        if targets:
            table = doc.add_table(rows=1, cols=1 + len(_TARGET_COLUMNS))
            table.style = "Table Grid"
            header = table.rows[0].cells
            header[0].text = "#"
            for i, (_key, label) in enumerate(_TARGET_COLUMNS, start=1):
                header[i].text = label
            for t in targets:
                cells = table.add_row().cells
                cells[0].text = str(t.get("sequence", ""))
                for i, (key, _label) in enumerate(_TARGET_COLUMNS, start=1):
                    value = t.get(key)
                    cells[i].text = str(value) if value is not None else "-"
        else:
            doc.add_paragraph("No planned targets recorded.")
    else:
        doc.add_heading("Description", level=1)
        doc.add_paragraph(str(payload.get("description", "")))
        link = payload.get("program_link", {})
        doc.add_paragraph(f"Program link status: {link.get('status', 'NONE')}")
        doc.add_heading("Steps", level=1)
        for s in payload.get("steps", []):
            title = f"{s.get('sequence')}. {s.get('title')}"
            if s.get("hold_point"):
                title += " [HOLD POINT]"
            doc.add_heading(title, level=2)
            if s.get("instructions"):
                doc.add_paragraph(str(s.get("instructions")))
            if s.get("responsible_role"):
                doc.add_paragraph(f"Responsible: {s.get('responsible_role')}")
            if s.get("acceptance_criteria"):
                doc.add_paragraph(f"Acceptance: {s.get('acceptance_criteria')}")
            if s.get("source_reference"):
                doc.add_paragraph(f"Source: {s.get('source_reference')}")
    doc.add_heading("Attribution notice", level=1)
    notice_para = doc.add_paragraph(str(payload.get("attribution_notice", "")))
    notice_para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    doc.save(str(path))


_PDF_PAGE_W, _PDF_PAGE_H = 595, 842  # A4 points
_PDF_MARGIN = 56
_PDF_LINE = 13


def _pdf_flow_lines(payload: dict[str, Any]) -> list[str]:
    """Build the full logical line flow for the PDF; pagination happens at render time."""
    status = str(payload.get("status", "")).upper()
    watermark = _WATERMARK if status != "APPROVED" else "APPROVED"
    lines: list[str] = [
        f"{payload.get('title', '')}",
        "",
        watermark,
        "",
        str(payload.get("export_notice", "")),
        "",
    ]
    redaction = payload.get("redaction", {})
    if redaction.get("applied"):
        lines += [
            f"Redacted export (policy {redaction.get('policy', '')}): local source paths masked.",
            "",
        ]
    if payload.get("record_type") == "program":
        lines += [str(payload.get("summary", "")), "", "Sections:"]
        for s in payload.get("sections", []):
            lines.append(f"  {s.get('sequence')}. {s.get('title')} [{s.get('completion_state')}]")
            if s.get("source_reference"):
                lines.append(f"     source: {s.get('source_reference')}")
            content = str(s.get("content", ""))
            if content:
                lines.append(f"     {content}")
        lines += ["", "Planned targets:"]
        for t in payload.get("targets", []):
            lines.append(
                f"  {t.get('sequence')}. {t.get('name')}: "
                f"hole {t.get('hole_size_in') if t.get('hole_size_in') is not None else '-'} in; "
                f"depth {_fmt(t.get('planned_depth_md_value'), t.get('planned_depth_md_unit'))}; "
                f"duration {t.get('planned_duration_days') if t.get('planned_duration_days') is not None else '-'} d; "
                f"mud {_fmt(t.get('planned_mud_weight_value'), t.get('planned_mud_weight_unit'))}; "
                f"NPT {t.get('planned_npt_hours') if t.get('planned_npt_hours') is not None else '-'} h; "
                f"cost {_fmt(t.get('planned_cost_value'), t.get('planned_cost_unit'))}"
            )
            if t.get("formation_top"):
                lines.append(f"     formation top: {t.get('formation_top')}")
            if t.get("casing_program"):
                lines.append(f"     casing: {t.get('casing_program')}")
    else:
        lines += [str(payload.get("description", ""))]
        link = payload.get("program_link", {})
        lines.append(f"Program link status: {link.get('status', 'NONE')}")
        if link.get("program_revision_id"):
            lines.append(f"Linked Program revision: {link.get('program_revision_id')}")
        lines += ["", "Steps:"]
        for s in payload.get("steps", []):
            hold = " [HOLD POINT]" if s.get("hold_point") else ""
            lines.append(f"  {s.get('sequence')}. {s.get('title')}{hold}")
            for label, key in (
                ("instructions", "instructions"),
                ("responsible", "responsible_role"),
                ("equipment", "equipment"),
                ("preconditions", "preconditions"),
                ("expected", "expected_result"),
                ("acceptance", "acceptance_criteria"),
                ("evidence", "evidence_required"),
                ("safety", "safety_controls"),
                ("contingency", "contingency"),
                ("source", "source_reference"),
            ):
                if s.get(key):
                    lines.append(f"     {label}: {s.get(key)}")
    lines += ["", str(payload.get("attribution_notice", ""))]
    return lines


def _wrap(text: str, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    out: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) > width:
            out.append(current)
            current = word
        else:
            current += " " + word
    out.append(current)
    return out


def _render_pdf(payload: dict[str, Any], path: Path) -> None:
    import pymupdf  # pinned in constraints

    status = str(payload.get("status", "")).upper()
    watermark = _WATERMARK if status != "APPROVED" else "APPROVED"
    doc = pymupdf.open()
    char_width = 5.0  # approx for 9pt helv
    max_chars = int((_PDF_PAGE_W - 2 * _PDF_MARGIN) / char_width)

    def new_page(page_number: int) -> pymupdf.Page:
        page = doc.new_page(width=_PDF_PAGE_W, height=_PDF_PAGE_H)
        # Header
        page.insert_text(
            (_PDF_MARGIN, 30),
            f"{payload.get('title', '')} — Rev {payload.get('revision', '')}",
            fontsize=8,
            fontname="helv",
        )
        # Footer with page number
        page.insert_text(
            (_PDF_MARGIN, _PDF_PAGE_H - 24),
            f"Page {page_number} — {watermark}",
            fontsize=8,
            fontname="helv",
        )
        # Watermark diagonal
        page.insert_textbox(
            pymupdf.Rect(0, _PDF_PAGE_H / 2 - 20, _PDF_PAGE_W, _PDF_PAGE_H / 2 + 20),
            watermark,
            fontsize=28,
            fontname="hebo",
            align=1,
            color=(0.85, 0.6, 0.6) if status != "APPROVED" else (0.6, 0.8, 0.6),
        )
        return page

    page = new_page(1)
    y = _PDF_MARGIN
    page_number = 1
    for raw in _pdf_flow_lines(payload):
        for line in _wrap(raw, max_chars):
            if y > _PDF_PAGE_H - _PDF_MARGIN:
                page_number += 1
                page = new_page(page_number)
                y = _PDF_MARGIN
            page.insert_text((_PDF_MARGIN, y), line, fontsize=9, fontname="helv")
            y += _PDF_LINE
    doc.save(str(path))
    doc.close()


def _publish_atomic(tmp: Path, target: Path) -> None:
    """Publish ``tmp`` at ``target`` without ever replacing an existing file.

    A hard link is created at the destination: ``os.link`` fails atomically with EEXIST if
    anything (file, symlink, directory) already occupies the path, so a concurrent creator
    can never be clobbered. If hard links are unsupported, fall back to claiming the
    destination with ``O_CREAT|O_EXCL`` before moving the content into place.
    """
    try:
        os.link(tmp, target)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise ValidationError(
                "refusing to overwrite an existing export (no-clobber)", path=str(target)
            ) from exc
        if exc.errno not in (errno.EPERM, errno.EXDEV, errno.ENOSYS, errno.EOPNOTSUPP):
            raise
        # Fallback: claim the destination exclusively, then write into the file this call
        # itself created. The placeholder is removed only if this call created it.
        data = tmp.read_bytes()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, data)
        except Exception:
            os.close(fd)
            target.unlink(missing_ok=True)
            raise
        os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


def export_record(
    service: Any,
    kind: str,
    record_id: str,
    *,
    fmt: str,
    out_path: str | Path,
    redact: bool = False,
) -> dict[str, Any]:
    """Export one exact revision to ``fmt`` at ``out_path``.

    Read-only against the workspace; race-safe no-clobber atomic publication; full fidelity
    by default with opt-in source-path redaction.
    """
    fmt = str(fmt).strip().lower()
    if fmt not in FORMATS:
        raise ValidationError("unsupported export format", allowed=list(FORMATS), got=fmt)
    payload = _record_payload(service, kind, record_id, redact=bool(redact))
    target = Path(out_path)
    if target.exists() or target.is_symlink():
        raise ValidationError(
            "refusing to overwrite an existing export (no-clobber)", path=str(target)
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = {"json": ".json", "html": ".html", "docx": ".docx", "pdf": ".pdf"}[fmt]
    if target.suffix.lower() != suffix:
        target = target.with_suffix(suffix)
        if target.exists() or target.is_symlink():
            raise ValidationError(
                "refusing to overwrite an existing export (no-clobber)", path=str(target)
            )
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".export-", suffix=suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if fmt == "json":
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
        elif fmt == "html":
            tmp.write_text(_render_html(payload))
        elif fmt == "docx":
            _render_docx(payload, tmp)
        else:
            _render_pdf(payload, tmp)
        _publish_atomic(tmp, target)
    except Exception:
        # Cleanup-only handler: remove the partial temp file, then re-raise unchanged.
        tmp.unlink(missing_ok=True)
        raise
    return {
        "record_type": kind,
        "record_id": record_id,
        "format": fmt,
        "path": str(target),
        "status": payload.get("status"),
        "revision": payload.get("revision"),
        "watermarked_draft": str(payload.get("status", "")).upper() != "APPROVED",
        "redacted": bool(payload.get("redaction", {}).get("applied")),
        "redaction_policy": payload.get("redaction", {}).get("policy"),
        "bytes": target.stat().st_size,
    }
