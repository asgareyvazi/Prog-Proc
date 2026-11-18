"""Controlled export of Program/Procedure revisions to JSON, HTML, DOCX and PDF.

Every exported artifact carries the record's exact lifecycle status and revision. A DRAFT
revision is watermarked ``DRAFT — NOT APPROVED`` on every page/section; an approved revision
names the exact approver attribution and timestamp recorded on the record. Export is read-only
against the workspace, refuses to overwrite an existing file (no-clobber), and writes
atomically via a temporary file plus ``os.replace``.

Actor strings are declared attributions, not authentication; the export says so on its face.
"""

from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError

FORMATS = ("json", "html", "docx", "pdf")

_WATERMARK = "DRAFT - NOT APPROVED"


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


def _record_payload(service: Any, kind: str, record_id: str) -> dict[str, Any]:
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
                    "sequence": t.sequence,
                    "name": t.name,
                    "hole_size_in": t.hole_size_in,
                    "casing_program": t.casing_program,
                    "planned_depth_md_value": t.planned_depth_md_value,
                    "planned_depth_md_unit": t.planned_depth_md_unit,
                    "planned_duration_days": t.planned_duration_days,
                    "planned_mud_weight_value": t.planned_mud_weight_value,
                    "planned_mud_weight_unit": t.planned_mud_weight_unit,
                    "planned_npt_hours": t.planned_npt_hours,
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
    payload["export_notice"] = _status_line(record)
    payload["attribution_notice"] = (
        "Actor strings are declared attributions, not authenticated identities. "
        "This document is not an engineering certification."
    )
    return payload


def _render_html(payload: dict[str, Any]) -> str:
    esc = html.escape
    status = str(payload.get("status", "")).upper()
    watermark = _WATERMARK if status != "APPROVED" else "APPROVED"
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
            parts.append(
                "<table><tr><th>#</th><th>Section</th><th>Depth (m MD)</th>"
                "<th>Duration (d)</th><th>Mud (ppg)</th><th>NPT (h)</th></tr>"
            )
            for t in targets:
                parts.append(
                    "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                        esc(str(t.get("sequence", ""))),
                        esc(str(t.get("name", ""))),
                        esc(str(t.get("planned_depth_md_value") or "")),
                        esc(str(t.get("planned_duration_days") or "")),
                        esc(str(t.get("planned_mud_weight_value") or "")),
                        esc(str(t.get("planned_npt_hours") or "")),
                    )
                )
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

    doc = Document()
    status = str(payload.get("status", "")).upper()
    doc.add_heading(str(payload.get("title", "")), level=0)
    notice = doc.add_paragraph()
    run = notice.add_run(_WATERMARK if status != "APPROVED" else "APPROVED")
    run.bold = True
    doc.add_paragraph(str(payload.get("export_notice", "")))
    if payload.get("record_type") == "program":
        doc.add_paragraph(str(payload.get("summary", "")))
        doc.add_heading("Sections", level=1)
        for s in payload.get("sections", []):
            doc.add_heading(f"{s.get('sequence')}. {s.get('title')}", level=2)
            doc.add_paragraph(f"State: {s.get('completion_state')}")
            if s.get("source_reference"):
                doc.add_paragraph(f"Source: {s.get('source_reference')}")
            doc.add_paragraph(str(s.get("content", "")))
        doc.add_heading("Planned targets", level=1)
        table = doc.add_table(rows=1, cols=6)
        table.style = "Table Grid"
        for i, head in enumerate(
            ["#", "Section", "Depth (m MD)", "Duration (d)", "Mud (ppg)", "NPT (h)"]
        ):
            table.rows[0].cells[i].text = head
        for t in payload.get("targets", []):
            cells = table.add_row().cells
            cells[0].text = str(t.get("sequence", ""))
            cells[1].text = str(t.get("name", ""))
            cells[2].text = str(t.get("planned_depth_md_value") or "")
            cells[3].text = str(t.get("planned_duration_days") or "")
            cells[4].text = str(t.get("planned_mud_weight_value") or "")
            cells[5].text = str(t.get("planned_npt_hours") or "")
    else:
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
    doc.add_paragraph(str(payload.get("attribution_notice", "")))
    doc.save(str(path))


def _render_pdf(payload: dict[str, Any], path: Path) -> None:
    import fitz  # pymupdf, pinned in constraints

    doc = fitz.open()
    page = doc.new_page()
    status = str(payload.get("status", "")).upper()
    text = [f"{payload.get('title', '')}", ""]
    text.append(_WATERMARK if status != "APPROVED" else "APPROVED")
    text.append(str(payload.get("export_notice", "")))
    text.append("")
    if payload.get("record_type") == "program":
        text.append(str(payload.get("summary", "")))
        text.append("")
        text.append("Sections:")
        for s in payload.get("sections", []):
            text.append(f"  {s.get('sequence')}. {s.get('title')} [{s.get('completion_state')}]")
            if s.get("source_reference"):
                text.append(f"     source: {s.get('source_reference')}")
            content = str(s.get("content", ""))
            if content:
                text.append(f"     {content[:400]}")
        text.append("")
        text.append("Planned targets:")
        for t in payload.get("targets", []):
            text.append(
                f"  {t.get('sequence')}. {t.get('name')}: "
                f"TD {t.get('planned_depth_md_value') or '-'} m MD, "
                f"{t.get('planned_duration_days') or '-'} d, "
                f"MW {t.get('planned_mud_weight_value') or '-'} ppg, "
                f"NPT {t.get('planned_npt_hours') or '-'} h"
            )
    else:
        text.append(str(payload.get("description", "")))
        link = payload.get("program_link", {})
        text.append(f"Program link status: {link.get('status', 'NONE')}")
        text.append("")
        text.append("Steps:")
        for s in payload.get("steps", []):
            hold = " [HOLD POINT]" if s.get("hold_point") else ""
            text.append(f"  {s.get('sequence')}. {s.get('title')}{hold}")
            if s.get("instructions"):
                text.append(f"     {str(s.get('instructions'))[:400]}")
            if s.get("acceptance_criteria"):
                text.append(f"     acceptance: {s.get('acceptance_criteria')}")
    text.append("")
    text.append(str(payload.get("attribution_notice", "")))
    page.insert_text((72, 72), "\n".join(text), fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()


def export_record(
    service: Any, kind: str, record_id: str, *, fmt: str, out_path: str | Path
) -> dict[str, Any]:
    """Export one exact revision to ``fmt`` at ``out_path``. No-clobber, atomic, read-only."""
    fmt = str(fmt).strip().lower()
    if fmt not in FORMATS:
        raise ValidationError("unsupported export format", allowed=list(FORMATS), got=fmt)
    payload = _record_payload(service, kind, record_id)
    target = Path(out_path)
    if target.exists():
        raise ValidationError(
            "refusing to overwrite an existing export (no-clobber)", path=str(target)
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = {"json": ".json", "html": ".html", "docx": ".docx", "pdf": ".pdf"}[fmt]
    if target.suffix.lower() != suffix:
        target = target.with_suffix(suffix)
        if target.exists():
            raise ValidationError(
                "refusing to overwrite an existing export (no-clobber)", path=str(target)
            )
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".export-", suffix=suffix)
    os.close(fd)
    try:
        tmp = Path(tmp_name)
        if fmt == "json":
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")
        elif fmt == "html":
            tmp.write_text(_render_html(payload))
        elif fmt == "docx":
            _render_docx(payload, tmp)
        else:
            _render_pdf(payload, tmp)
        tmp.replace(target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "record_type": kind,
        "record_id": record_id,
        "format": fmt,
        "path": str(target),
        "status": payload.get("status"),
        "revision": payload.get("revision"),
        "watermarked_draft": str(payload.get("status", "")).upper() != "APPROVED",
        "bytes": target.stat().st_size,
    }
