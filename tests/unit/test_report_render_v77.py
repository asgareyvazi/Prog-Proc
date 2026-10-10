"""Renderer and exhibit tests that do not need a workspace.

The HTML and SVG modules must be safe and deterministic on their own. A chart
that cannot be plotted honestly must say so, and must not draw a null as zero.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from drilling_intelligence.reporting.contract import (
    RENDERER_VERSION,
    REPORT_SCHEMA,
    ReportPack,
    ReportRequest,
    content_identity,
    with_identity,
)
from drilling_intelligence.reporting.exhibits import exhibit_from_metric, unsupported_depth_exhibit
from drilling_intelligence.reporting.html import render_html, write_report_html
from drilling_intelligence.reporting.layout import layout_exhibit
from drilling_intelligence.reporting.svg import render_svg

_ROOT = Path(__file__).resolve().parents[2]
_RENDER_MODULES = (
    "format.py",
    "contract.py",
    "layout.py",
    "exhibits.py",
    "compose.py",
    "svg.py",
    "html.py",
)
_HOSTILE = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '" onclick="alert(1)',
    "../../evil",
    "<'&\">",
)


def _subjects(*names: str) -> list[dict[str, str]]:
    return [{"well_id": f"id-{name}", "name": name} for name in names]


def _cell(value, unit, state):
    return {"value": value, "unit": unit, "value_state": state, "comparability": state}


def _row(metric, label, cells, comparability, **extra):
    return {
        "metric": metric,
        "label": label,
        "comparability": comparability,
        "values": cells,
        "units": extra.get("units"),
        "note": extra.get("note", ""),
        "limitations": extra.get("limitations", []),
    }


def _pack_dict(**overrides) -> dict:
    base = {
        "schema": REPORT_SCHEMA,
        "request": {
            "mode": "single_well",
            "well_ids": ["w-1"],
            "anchor": "",
            "offsets": [],
            "since": None,
            "until": None,
            "detail": 1,
            "evidence_limit": 10,
            "offset_limit": 10,
        },
        "mode": "single_well",
        "title": "Engineering report — empty",
        "subject": {
            "kind": "well",
            "basis_kind": "single_well",
            "name": "empty",
            "window": {
                "applied": False,
                "since": None,
                "until": None,
                "windowed_domains": [],
                "undated_behavior": "",
            },
        },
        "source_packs": [{"role": "primary", "schema": "decision-pack/1", "identity": "src"}],
        "sections": [
            {
                "section_id": "cover",
                "title": "Cover",
                "state": "PRESENT",
                "claim_kind": "FACT",
                "note": "limited",
                "tables": [],
                "exhibits": [],
                "limitations": [],
            }
        ],
        "exhibits": [],
        "evidence": [],
        "limitations": ["logging_not_ready"],
        "freshness": {"operations": "CURRENT"},
        "observations": [],
        "identity": "abc",
    }
    base.update(overrides)
    return base


def test_renderer_modules_do_not_import_the_database() -> None:
    banned = {"sqlalchemy", "Session", "IntelligenceService", "create_engine"}
    root = _ROOT / "src" / "drilling_intelligence" / "reporting"
    for name in _RENDER_MODULES:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                imported.update(alias.name for alias in node.names)
        assert not (imported & banned), (name, imported & banned)


def test_report_identity_ignores_nothing_but_is_stable_and_content_sensitive() -> None:
    request = ReportRequest(mode="single_well", well_ids=("w-1",))
    first = with_identity(
        ReportPack(
            schema=REPORT_SCHEMA,
            request=request,
            mode="single_well",
            title="Engineering report — A",
            subject={"name": "A"},
            source_packs=({"schema": "decision-pack/1", "identity": "src", "role": "primary"},),
            sections=(),
            exhibits=(),
            evidence=(),
            limitations=("logging_not_ready",),
            freshness={"operations": "CURRENT"},
            observations=(),
            identity="",
        )
    )
    again = with_identity(
        ReportPack(
            schema=REPORT_SCHEMA,
            request=request,
            mode="single_well",
            title="Engineering report — A",
            subject={"name": "A"},
            source_packs=({"schema": "decision-pack/1", "identity": "src", "role": "primary"},),
            sections=(),
            exhibits=(),
            evidence=(),
            limitations=("logging_not_ready",),
            freshness={"operations": "CURRENT"},
            observations=(),
            identity="this-is-not-hashed",
        )
    )
    changed = with_identity(
        ReportPack(
            schema=REPORT_SCHEMA,
            request=request,
            mode="single_well",
            title="Engineering report — B",
            subject={"name": "A"},
            source_packs=({"schema": "decision-pack/1", "identity": "src", "role": "primary"},),
            sections=(),
            exhibits=(),
            evidence=(),
            limitations=("logging_not_ready",),
            freshness={"operations": "CURRENT"},
            observations=(),
            identity="",
        )
    )
    assert first.identity == again.identity
    assert first.identity == content_identity(first.to_dict())
    assert changed.identity != first.identity
    json.dumps(first.to_dict())
    assert "path" not in first.to_dict()


def test_comparable_numbers_plot_in_subject_order_and_null_is_not_zero() -> None:
    subjects = _subjects("A-3", "B-11", "C-17")
    row = _row(
        "npt.hours",
        "NPT hours recorded",
        {
            "id-A-3": _cell(28.75, "h", "STATED"),
            "id-B-11": _cell(12.0, "h", "STATED"),
            "id-C-17": _cell(None, "h", "NO_RECORDS"),
        },
        "COMPARABLE",
    )
    exhibit = exhibit_from_metric(row, subjects)
    assert exhibit.state == "RENDERED"
    assert exhibit.exhibit_type == "horizontal_bar"
    points = exhibit.series[0]["points"]
    assert [point["name"] for point in points] == ["A-3", "B-11", "C-17"]
    assert points[0]["value"] == 28.75
    assert points[1]["value"] == 12.0
    assert points[2]["value"] is None
    assert points[2]["missing"] is True
    assert points[2]["plottable"] is False
    layout = layout_exhibit(exhibit.payload())
    plotted = {bar["subject_id"]: bar["value"] for bar in layout["bars"]}
    assert plotted == {"id-A-3": 28.75, "id-B-11": 12.0}
    assert "id-C-17" not in plotted
    assert any(
        marker["subject_id"] == "id-C-17" and marker["kind"] == "missing"
        for marker in layout["markers"]
    )
    svg = render_svg(exhibit.payload())
    assert 'class="bar"' in svg
    assert svg.index("A-3") < svg.index("B-11") < svg.index("C-17")
    assert "28.75" in svg and "12" in svg


def test_zero_count_is_drawn_as_zero_and_null_duration_is_not() -> None:
    subjects = _subjects("A-3", "B-11")
    zeros = exhibit_from_metric(
        _row(
            "well_control.events",
            "Well-control rows",
            {
                "id-A-3": _cell(7, None, "COUNTED"),
                "id-B-11": _cell(0, None, "COUNTED"),
            },
            "COMPARABLE",
        ),
        subjects,
    )
    assert zeros.state == "RENDERED"
    layout = layout_exhibit(zeros.payload())
    zero_bar = next(bar for bar in layout["bars"] if bar["subject_id"] == "id-B-11")
    assert zero_bar["value"] == 0
    assert zero_bar["zero"] is True
    missing = exhibit_from_metric(
        _row(
            "npt.hours",
            "NPT hours recorded",
            {
                "id-A-3": _cell(28.75, "h", "STATED"),
                "id-B-11": _cell(None, "h", "NO_RECORDS"),
            },
            "MISSING",
        ),
        subjects,
    )
    assert missing.state == "MISSING"
    assert layout_exhibit(missing.payload())["bars"] == []


def test_incomparable_units_and_unresolved_scales_are_not_plotted() -> None:
    subjects = _subjects("A-3", "B-11")
    mixed = exhibit_from_metric(
        _row(
            "cost.planned",
            "Total planned cost",
            {
                "id-A-3": _cell(100000.0, "USD", "STATED"),
                "id-B-11": _cell(50000.0, "NOK", "STATED"),
            },
            "INCOMPARABLE",
            units={"id-A-3": "USD", "id-B-11": "NOK"},
        ),
        subjects,
    )
    assert mixed.state == "INCOMPARABLE"
    layout = layout_exhibit(mixed.payload())
    assert layout["bars"] == []
    svg = render_svg(mixed.payload())
    assert "INCOMPARABLE" in svg
    assert "USD" in svg and "NOK" in svg
    assert 'class="bar"' not in svg
    unresolved = exhibit_from_metric(
        _row(
            "risk.severity.high",
            "Current risks in severity band high",
            {
                "id-A-3": _cell(2, None, "COUNTED"),
                "id-B-11": _cell(1, None, "COUNTED"),
            },
            "UNRESOLVED",
        ),
        subjects,
    )
    assert unresolved.state == "UNRESOLVED"
    assert layout_exhibit(unresolved.payload())["bars"] == []
    depth = unsupported_depth_exhibit(subjects)
    assert depth.state == "UNSUPPORTED"
    assert "log" in depth.reason
    assert layout_exhibit(depth.payload())["bars"] == []


def test_svg_and_html_escape_hostile_source_strings_and_stay_offline() -> None:
    hostile_name = _HOSTILE[0]
    exhibit = {
        "exhibit_id": "exhibit-hostile",
        "exhibit_type": "state",
        "title": hostile_name,
        "metric": "npt.hours",
        "unit": "h",
        "state": "INCOMPARABLE",
        "subjects": [{"id": "1", "name": _HOSTILE[1]}],
        "series": [
            {
                "series_id": "value",
                "label": _HOSTILE[2],
                "unit": "h",
                "points": [
                    {
                        "subject_id": "1",
                        "name": hostile_name,
                        "value": None,
                        "raw_value": _HOSTILE[3],
                        "unit": _HOSTILE[4],
                        "value_state": "STATED",
                        "comparability": "INCOMPARABLE",
                        "missing": False,
                        "plottable": False,
                    }
                ],
            }
        ],
        "categories": ["1"],
        "data": {},
        "caption": hostile_name,
        "evidence_refs": [],
        "limitations": [],
        "reason": _HOSTILE[1],
    }
    svg = render_svg(exhibit)
    html = render_html(
        _pack_dict(
            title=hostile_name,
            sections=[
                {
                    "section_id": "charts",
                    "title": _HOSTILE[2],
                    "state": "PRESENT",
                    "claim_kind": "FACT",
                    "note": hostile_name,
                    "tables": [
                        {
                            "table_id": "t",
                            "title": "Cells",
                            "columns": ["name"],
                            "column_labels": {},
                            "rows": [{"name": _HOSTILE[3]}],
                            "note": _HOSTILE[1],
                        }
                    ],
                    "exhibits": ["exhibit-hostile"],
                    "limitations": [],
                }
            ],
            exhibits=[exhibit],
            observations=[hostile_name],
            identity="id-1",
        )
    )
    for document in (svg, html):
        lowered = document.lower()
        assert "<script" not in lowered
        assert "<iframe" not in lowered
        assert "<img" not in lowered
        assert "javascript:" not in lowered
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", document, re.I)
        assert "https://" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in svg
    assert html.startswith("<!DOCTYPE html>")
    assert 'charset="utf-8"' in html
    assert "engineering-report/1" in html
    assert "id-1" in html
    assert "@media print" in html
    assert "Table fallback" in html
    assert RENDERER_VERSION in html
    assert svg.startswith("<svg ")
    assert 'viewBox="0 0 ' in svg
    allowed = svg.replace("http://www.w3.org/2000/svg", "")
    assert "http://" not in allowed
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")


def test_html_is_byte_stable_and_the_path_is_not_part_of_the_document(tmp_path) -> None:
    payload = _pack_dict()
    first = render_html(payload)
    second = render_html(payload)
    assert first == second
    left = tmp_path / "a" / "report.html"
    right = tmp_path / "b" / "other.html"
    left.parent.mkdir()
    right.parent.mkdir()
    write_report_html(left, payload)
    write_report_html(right, payload)
    assert left.read_bytes() == right.read_bytes()
    text = left.read_text(encoding="utf-8")
    assert str(left) not in text
    assert str(right) not in text
    assert "tmp" not in text


def test_empty_report_still_renders() -> None:
    html = render_html(_pack_dict())
    assert "<!DOCTYPE html>" in html
    assert "State: PRESENT" in html
    svg = render_svg(
        {
            "exhibit_id": "exhibit-empty",
            "title": "Nothing recorded",
            "state": "NO_DATA",
            "exhibit_type": "state",
            "subjects": [],
            "series": [],
            "unit": None,
            "reason": "no comparable number is stated",
        }
    )
    assert "NO_DATA" in svg
    assert 'class="bar"' not in svg
