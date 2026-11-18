"""The package-level coverage view, and what it refuses to hide.

A single percentage is the number that gets quoted and the number that answers
nothing.  ``tools/coverage_report.py`` exists to answer the questions a total
cannot: which group is thin, which group was *not measured* because an optional
dependency was absent, and how the measurement compares with the recorded
baseline.

These tests build the XML by hand, so that the arithmetic and the honesty are
checked without running the suite a second time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SPEC = importlib.util.spec_from_file_location(
    "coverage_report", ROOT / "tools" / "coverage_report.py"
)
assert _SPEC is not None and _SPEC.loader is not None
coverage_report = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = coverage_report
_SPEC.loader.exec_module(coverage_report)


COBERTURA = """<?xml version="1.0" ?>
<coverage version="7.17.0">
  <packages>
    <package name="drilling_intelligence.core">
      <classes>
        <class name="units.py" filename="src/drilling_intelligence/core/units.py">
          <lines>
            <line number="1" hits="3"/>
            <line number="2" hits="0"/>
            <line number="3" hits="2" branch="true" condition-coverage="50% (1/2)"/>
          </lines>
        </class>
        <class name="errors.py" filename="src/drilling_intelligence/core/errors.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
    <package name="drilling_intelligence.ui">
      <classes>
        <class name="app.py" filename="src/drilling_intelligence/ui/app.py">
          <lines>
            <line number="1" hits="0"/>
            <line number="2" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
    <package name="drilling_intelligence.wells">
      <classes>
        <class name="repo.py" filename="src/drilling_intelligence/wells/repository.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
    <package name="elsewhere">
      <classes>
        <class name="other.py" filename="other_package/other.py">
          <lines>
            <line number="1" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
    <package name="windows">
      <classes>
        <class name="repo.py" filename="src\\drilling_intelligence\\review\\service.py">
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


def _coverage(tmp_path: Path):
    path = tmp_path / "coverage.xml"
    path.write_text(COBERTURA, encoding="utf-8")
    return coverage_report.read_cobertura(path)


def test_a_file_belongs_to_the_group_that_names_its_package() -> None:
    assert coverage_report.group_of("src/drilling_intelligence/core/units.py") == "core"
    assert coverage_report.group_of("src/drilling_intelligence/ui/app.py") == "qt/ui"
    assert coverage_report.group_of("src/drilling_intelligence/documents/pdf.py") == (
        "document/knowledge/promotion"
    )
    assert coverage_report.group_of("src/drilling_intelligence/wells/repository.py") == (
        "other packages"
    )


def test_a_windows_path_is_the_same_file_as_a_posix_path() -> None:
    """The CI measures on both platforms, and the report is compared across them."""
    windows = "src\\drilling_intelligence\\review\\service.py"
    posix = "src/drilling_intelligence/review/service.py"
    assert coverage_report.package_of(windows) == coverage_report.package_of(posix) == "review"
    assert coverage_report.group_of(windows) == coverage_report.group_of(posix) == "review"


def test_the_package_relative_form_names_the_same_file() -> None:
    """``pytest --cov`` writes the path it measured; a package-rooted report
    writes ``review/service.py`` and names the root once, in ``<sources>``.
    Reading the second shape as "no product files" produced 100% of nothing,
    which is exactly the number this tool exists to prevent."""
    assert coverage_report.package_of("review/service.py") == "review"
    assert coverage_report.group_of("core/units.py") == "core"
    assert coverage_report.group_of("operations/promote.py") == ("document/knowledge/promotion")
    assert coverage_report.group_of("__init__.py") == "other packages"
    # A file that is not ours is still refused in both shapes.
    assert coverage_report.relative_to_package("/usr/lib/python3/other.py") is None
    assert coverage_report.relative_to_package("../outside/mod.py") is None


def test_the_real_report_shape_is_read_from_a_package_rooted_file(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?>
<coverage version="7.17.0">
  <sources><source>/repo/src/drilling_intelligence</source></sources>
  <packages>
    <package name="." line-rate="0" branch-rate="1">
      <classes>
        <class name="main_window.py" filename="ui/main_window.py">
          <lines><line number="1" hits="0"/><line number="2" hits="0"/></lines>
        </class>
        <class name="units.py" filename="core/units.py">
          <lines><line number="1" hits="1"/><line number="2" hits="1"/></lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
        encoding="utf-8",
    )
    coverage = coverage_report.read_cobertura(xml)
    assert len(coverage.files) == 2
    assert coverage.groups["qt/ui"].line_percent == 0.0
    assert coverage.groups["core"].line_percent == 100.0
    assert coverage.overall.covered_statements == 2


def test_a_package_outside_the_product_is_not_counted_in_the_product(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    measured = {item.path for item in coverage.files}
    assert "other.py" not in measured
    assert all(not path.startswith("other_package/") for path in measured)
    # What is measured is named relative to the package, which is the same in
    # both report shapes.
    assert "core/units.py" in measured


def test_the_groups_add_up_and_nothing_is_silently_averaged(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    total_statements = sum(group.statements for group in coverage.groups.values())
    assert total_statements == sum(item.statements for item in coverage.files)
    # "other packages" exists so that a package which is not in a named group
    # cannot disappear into an average.
    assert "other packages" in coverage.groups
    assert coverage.groups["other packages"].statements == 2


def test_the_percentages_are_computed_from_the_counts(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    core = coverage.groups["core"]
    assert core.statements == 5
    assert core.covered_statements == 4
    assert round(core.line_percent, 2) == 80.0
    assert core.branches == 2
    assert core.covered_branches == 1
    assert round(core.branch_percent, 2) == 50.0
    qt = coverage.groups["qt/ui"]
    assert qt.covered_statements == 0
    assert qt.line_percent == 0.0


def test_a_group_with_no_measurement_is_not_reported_as_a_percentage() -> None:
    """An empty group is 100% of nothing, and saying so is less misleading than 0%."""
    empty = coverage_report.GroupCoverage(name="empty")
    assert empty.statements == 0
    assert empty.line_percent is None
    assert empty.branch_percent is None
    assert empty.present is False
    assert empty.measurement_state == "not_measured"
    assert empty.as_dict()["line_percent"] is None


def test_the_comparison_names_every_group_and_the_total(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    view = coverage_report.build_view(coverage)
    baseline = {
        "measurement_scope": view["measurement_scope"],
        "test_selection": view["test_selection"],
        "schema_version": view["schema_version"],
        "optional_dependencies": view["optional_dependencies"],
        "runtime": view["runtime"],
        "overall": {"line_percent": 50.0},
        "groups": {"core": {"line_percent": 50.0}, "qt/ui": {"line_percent": 0.0}},
    }
    lines = coverage_report.compare(view, baseline)
    joined = "\n".join(lines)
    assert "core: 50.00% -> 80.00% (+30.00 points)" in joined
    assert "qt/ui" in joined
    assert "no threshold is enforced" in joined
    # A group that did not exist in the baseline is still named.
    assert "other packages" in joined


def test_a_missing_baseline_is_said_out_loud_not_guessed(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    view = coverage_report.build_view(coverage)
    lines = coverage_report.compare(view, None)
    assert lines == ["no baseline is recorded; this measurement is the first one"]


def test_the_report_states_the_optional_dependencies_that_were_absent(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    view = coverage_report.build_view(coverage)
    facts = {fact["name"]: fact for fact in view["optional_dependencies"]}
    assert set(facts) == {"ui", "mineru", "ollama"}
    for fact in facts.values():
        assert isinstance(fact["present"], bool)
        assert fact["note"]
        assert fact["groups"]
    markdown = coverage_report.render_markdown(view, None)
    assert "optional dependencies" in markdown.lower()
    assert "absent" in markdown or "present" in markdown
    assert "not the same as being uncovered" in markdown


def test_the_report_carries_the_commit_and_the_runtime(tmp_path: Path) -> None:
    coverage = _coverage(tmp_path)
    view = coverage_report.build_view(coverage)
    assert view["runtime"].startswith("CPython ")
    assert len(str(view["commit"])) >= 7
    markdown = coverage_report.render_markdown(view, None)
    assert view["commit"] in markdown
    assert view["runtime"] in markdown


def test_a_missing_xml_is_not_a_failure() -> None:
    """The tool is a record, not a gate: no measurement is not a defect in diagnostic mode."""
    # Strict (default) must fail for required measurement
    assert coverage_report.main(["--xml", str(ROOT / "does-not-exist.xml")]) == 2
    # Diagnostic mode with --allow-missing succeeds
    assert coverage_report.main(["--xml", str(ROOT / "does-not-exist.xml"), "--allow-missing"]) == 0


def test_the_tool_writes_the_markdown_and_the_json(tmp_path: Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(COBERTURA, encoding="utf-8")
    markdown = tmp_path / "report.md"
    payload = tmp_path / "report.json"
    baseline = tmp_path / "baseline.json"
    assert (
        coverage_report.main(
            [
                "--xml",
                str(xml),
                "--output",
                str(markdown),
                "--json",
                str(payload),
                "--baseline",
                str(baseline),
                "--write-baseline",
                "--label",
                "unit test",
            ]
        )
        == 0
    )
    import json

    assert markdown.exists()
    data = json.loads(payload.read_text(encoding="utf-8"))
    assert data["groups"]["core"]["line_percent"] == 80.0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert recorded["label"] == "unit test"
