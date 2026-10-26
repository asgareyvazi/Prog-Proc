"""V8.19 scope-correct coverage report regression tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "coverage_report_v19", ROOT / "tools" / "coverage_report.py"
)
assert _SPEC is not None and _SPEC.loader is not None
cr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cr
_SPEC.loader.exec_module(cr)


COBERTURA_UI = """<?xml version="1.0" ?>
<coverage version="7.17.0">
  <packages>
    <package name="drilling_intelligence.ui">
      <classes>
        <class name="app.py" filename="src/drilling_intelligence/ui/app.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
    <package name="drilling_intelligence.core">
      <classes>
        <class name="units.py" filename="src/drilling_intelligence/core/units.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""

COBERTURA_FULL = """<?xml version="1.0" ?>
<coverage version="7.17.0">
  <packages>
    <package name="drilling_intelligence.core">
      <classes>
        <class name="units.py" filename="src/drilling_intelligence/core/units.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def _write_xml(tmp_path: Path, content: str, name: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_input_path_rendered_correctly(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov = cr.read_cobertura(xml)
    view = cr.build_view(cov, scope="full-suite", xml_path=str(xml))
    md = cr.render_markdown(view, None)
    assert f"--xml {xml}" in md
    assert "full-suite" in md
    assert cr._infer_scope(str(xml)) == "full-suite"

    xml2 = _write_xml(tmp_path, COBERTURA_UI, "coverage-ui.xml")
    cov2 = cr.read_cobertura(xml2)
    view2 = cr.build_view(cov2, scope="ui-test-selection", xml_path=str(xml2))
    md2 = cr.render_markdown(view2, None)
    assert f"--xml {xml2}" in md2
    assert "ui-test-selection" in md2
    assert cr._infer_scope(str(xml2)) == "ui-test-selection"


def test_labeling_full_suite_vs_ui(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov = cr.read_cobertura(xml)
    view = cr.build_view(cov, xml_path=str(xml))
    assert view["measurement_scope"] == "full-suite"
    assert "pytest" in str(view["test_selection"])

    xml2 = _write_xml(tmp_path, COBERTURA_UI, "coverage-ui.xml")
    cov2 = cr.read_cobertura(xml2)
    view2 = cr.build_view(cov2, xml_path=str(xml2))
    assert view2["measurement_scope"] == "ui-test-selection"


def test_no_misleading_delta_when_scopes_differ(tmp_path: Path) -> None:
    xml_full = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov_full = cr.read_cobertura(xml_full)
    view_full = cr.build_view(cov_full, scope="full-suite", xml_path=str(xml_full))

    xml_ui = _write_xml(tmp_path, COBERTURA_UI, "coverage-ui.xml")
    cov_ui = cr.read_cobertura(xml_ui)
    view_ui = cr.build_view(cov_ui, scope="ui-test-selection", xml_path=str(xml_ui))

    # full-suite baseline vs ui measurement -> not comparable
    lines = cr.compare(view_ui, view_full)
    assert any("not comparable" in line for line in lines)
    assert not any(
        "->" in line and "points" in line and "core:" in line
        for line in lines
        if "not comparable" not in line
    )


def test_comparable_same_scope_produces_delta(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov = cr.read_cobertura(xml)
    v1 = cr.build_view(cov, scope="full-suite", xml_path=str(xml))
    # modify baseline to have lower coverage
    baseline = dict(v1)
    baseline["overall"] = dict(v1["overall"])  # type: ignore[arg-type]
    baseline["overall"]["line_percent"] = 50.0  # type: ignore[arg-type]
    baseline["groups"] = {k: dict(v) for k, v in v1["groups"].items()}  # type: ignore[arg-type]
    for g in baseline["groups"].values():
        g["line_percent"] = 10.0
    lines = cr.compare(v1, baseline)
    assert any("->" in line and "points" in line for line in lines)


def test_optional_ui_dependency_absent_handling(tmp_path: Path) -> None:
    # When ui not present, qt/ui group should be not measured, not 0% misread

    xml = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov = cr.read_cobertura(xml)
    view = cr.build_view(cov, scope="full-suite", xml_path=str(xml))
    md = cr.render_markdown(view, None)
    # Should contain "not measured" for empty groups
    assert "not measured" in md


def test_historical_headless_baseline_not_comparable_for_ui(tmp_path: Path) -> None:
    xml_ui = _write_xml(tmp_path, COBERTURA_UI, "coverage-ui.xml")
    cov_ui = cr.read_cobertura(xml_ui)
    view_ui = cr.build_view(cov_ui, scope="ui-test-selection", xml_path=str(xml_ui))
    # Simulate historical baseline: full-suite headless without ui
    xml_full = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov_full = cr.read_cobertura(xml_full)
    view_full = cr.build_view(cov_full, scope="full-suite", xml_path=str(xml_full))
    lines = cr.compare(view_ui, view_full)
    assert any("not comparable" in line for line in lines)


def test_zero_statements_and_branches(tmp_path: Path) -> None:
    assert cr._format_percent(100.0, 0) == "— (not measured)"
    assert cr._format_percent(0.0, 0) == "— (not measured)"
    assert cr._format_percent(None, 0) == "— (not measured)"
    assert cr._format_percent(61.9, 10) == "61.9%"
    # empty group: not measured, not 100%
    empty = cr.GroupCoverage(name="empty")
    assert empty.statements == 0
    assert empty.line_percent is None
    assert empty.branch_percent is None
    assert empty.present is False
    assert empty.measurement_state == "not_measured"
    assert empty.as_dict()["line_percent"] is None
    assert empty.as_dict()["present"] is False
    assert cr._format_percent(empty.line_percent, empty.statements) == "— (not measured)"


def test_branch_totals_and_percent(tmp_path: Path) -> None:
    gc = cr.GroupCoverage(name="x")
    gc.files = [
        cr.FileCoverage(
            path="a", statements=10, covered_statements=5, branches=4, covered_branches=2
        )
    ]
    assert gc.statements == 10
    assert gc.covered_statements == 5
    assert gc.branches == 4
    assert gc.covered_branches == 2
    assert gc.branch_percent == 50.0


def test_covered_total_counts(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_UI, "coverage.xml")
    cov = cr.read_cobertura(xml)
    assert cov.overall.statements > 0
    assert cov.overall.covered_statements >= 0
    assert cov.overall.statements >= cov.overall.covered_statements


def test_sha_scope_metadata(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov = cr.read_cobertura(xml)
    view = cr.build_view(cov, scope="full-suite", xml_path=str(xml), test_selection="pytest -k foo")
    assert "commit" in view
    assert "measurement_scope" in view
    assert view["measurement_scope"] == "full-suite"
    assert view["test_selection"] == "pytest -k foo"
    assert view["xml_path"] == str(xml)
    assert "schema_version" in view


def test_json_markdown_consistency(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_UI, "coverage-ui.xml")
    cov = cr.read_cobertura(xml)
    view = cr.build_view(cov, scope="ui-test-selection", xml_path=str(xml))
    md = cr.render_markdown(view, None)
    # JSON and Markdown should both contain commit and scope
    assert str(view["commit"]) in md
    assert "ui-test-selection" in md
    j = json.dumps(view)
    assert "measurement_scope" in j
    assert "xml_path" in j


def test_no_nan_infinity(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_FULL, "coverage.xml")
    cov = cr.read_cobertura(xml)
    view = cr.build_view(cov, scope="full-suite", xml_path=str(xml))
    for g in view["groups"].values():  # type: ignore[attr-defined]
        assert g["line_percent"] != float("inf")  # type: ignore[index]
        assert g["line_percent"] == g["line_percent"]  # not NaN
        assert g["branch_percent"] != float("inf")  # type: ignore[index]


def test_cli_exit_when_xml_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.xml"
    # Strict mode (default) must fail when required XML is missing
    assert cr.main(["--xml", str(missing)]) == 2
    # Diagnostic mode with --allow-missing succeeds
    assert cr.main(["--xml", str(missing), "--allow-missing"]) == 0


def test_integration_small_xml_fixture(tmp_path: Path) -> None:
    xml = _write_xml(tmp_path, COBERTURA_UI, "coverage.xml")
    md = tmp_path / "report.md"
    js = tmp_path / "report.json"
    baseline = tmp_path / "baseline.json"
    rc = cr.main(
        [
            "--xml",
            str(xml),
            "--output",
            str(md),
            "--json",
            str(js),
            "--baseline",
            str(baseline),
            "--scope",
            "full-suite",
        ]
    )
    assert rc == 0
    assert md.exists()
    assert js.exists()
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["measurement_scope"] == "full-suite"
    assert "overall" in data
    content = md.read_text(encoding="utf-8")
    assert "Generated by" in content
    assert str(xml) in content
    assert "full-suite" in content
