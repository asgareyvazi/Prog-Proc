"""V8.18 coverage semantics — the metric must mean what its label says."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "coverage_report_v18", ROOT / "tools" / "coverage_report.py"
)
assert SPEC is not None and SPEC.loader is not None
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod  # type: ignore[attr-defined]
SPEC.loader.exec_module(mod)  # type: ignore[union-attr]


def test_package_relative_paths_posix_and_windows_are_equivalent() -> None:
    assert mod.relative_to_package("src/drilling_intelligence/core/units.py") == "core/units.py"
    assert mod.relative_to_package("src\\drilling_intelligence\\core\\units.py") == "core/units.py"
    assert mod.package_of("src/drilling_intelligence/ui/app.py") == "ui"
    assert mod.package_of("src\\drilling_intelligence\\ui\\app.py") == "ui"


def test_missing_optional_dependencies_are_seen_as_not_measured(tmp_path: pathlib.Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?><coverage><packages>
        <package name="drilling_intelligence.core"><classes>
        <class filename="src/drilling_intelligence/core/units.py"><lines><line number="1" hits="1"/></lines></class>
        </classes></package></packages></coverage>""",
        encoding="utf-8",
    )
    cov = mod.read_cobertura(xml)
    view = mod.build_view(cov)
    facts = {f["name"]: f for f in view["optional_dependencies"]}  # type: ignore[index]
    # In this headless environment PySide6 is installed via [dev,ui] but we can still check shape
    assert "ui" in facts
    assert "mineru" in facts
    # The markdown must say “not the same as being uncovered”
    md = mod.render_markdown(view, None)
    assert "not the same as being uncovered" in md


def test_empty_package_group_is_not_described_as_100_without_qualification(
    tmp_path: pathlib.Path,
) -> None:
    empty = mod.GroupCoverage(name="empty")
    assert empty.statements == 0
    assert empty.line_percent == 100.0
    # Rendering must qualify it
    view = {
        "source": "coverage.xml",
        "commit": "abc",
        "runtime": "CPython 3.11",
        "optional_dependencies": [],
        "overall": {
            "covered_statements": 0,
            "statements": 0,
            "covered_branches": 0,
            "branches": 0,
            "line_percent": 100.0,
            "branch_percent": 100.0,
        },
        "groups": {
            "empty": empty.as_dict(),
            "core": {
                "files": 1,
                "statements": 2,
                "covered_statements": 1,
                "line_percent": 50.0,
                "branches": 0,
                "covered_branches": 0,
                "branch_percent": 100.0,
            },
        },
    }
    md = mod.render_markdown(view, None)  # type: ignore[arg-type]
    assert "— (not measured)" in md
    assert "not measured" in md.lower()


def test_zero_branches_is_not_100_without_qualification() -> None:
    group = mod.GroupCoverage(name="core")
    # Add a file with 0 branches
    group.files = [
        mod.FileCoverage(
            path="core/units.py", statements=2, covered_statements=2, branches=0, covered_branches=0
        )
    ]
    assert group.branch_percent == 100.0
    view = {
        "source": "coverage.xml",
        "commit": "abc",
        "runtime": "CPython 3.11",
        "optional_dependencies": [],
        "overall": {
            "covered_statements": 2,
            "statements": 2,
            "covered_branches": 0,
            "branches": 0,
            "line_percent": 100.0,
            "branch_percent": 100.0,
        },
        "groups": {"core": group.as_dict()},
    }
    md = mod.render_markdown(view, None)  # type: ignore[arg-type]
    assert "— (not measured)" in md


def test_coverage_xml_with_sources_root_is_read(tmp_path: pathlib.Path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?><coverage><sources><source>/repo/src/drilling_intelligence</source></sources>
        <packages><package name="."><classes>
        <class filename="ui/main_window.py"><lines><line number="1" hits="0"/><line number="2" hits="0"/></lines></class>
        <class filename="core/units.py"><lines><line number="1" hits="1"/><line number="2" hits="1"/></lines></class>
        </classes></package></packages></coverage>""",
        encoding="utf-8",
    )
    cov = mod.read_cobertura(xml)
    assert len(cov.files) == 2
    assert cov.groups["qt/ui"].line_percent == 0.0
    assert cov.groups["core"].line_percent == 100.0


def test_headless_full_suite_xml_separates_ui_package(tmp_path: pathlib.Path) -> None:
    # Headless: ui files exist but hits=0
    xml = tmp_path / "coverage.xml"
    xml.write_text(
        """<?xml version="1.0" ?><coverage><packages>
        <package name="drilling_intelligence.core"><classes>
        <class filename="src/drilling_intelligence/core/units.py"><lines><line number="1" hits="1"/></lines></class>
        </classes></package>
        <package name="drilling_intelligence.ui"><classes>
        <class filename="src/drilling_intelligence/ui/app.py"><lines><line number="1" hits="0"/></lines></class>
        </classes></package>
        </packages></coverage>""",
        encoding="utf-8",
    )
    cov = mod.read_cobertura(xml)
    view = mod.build_view(cov)
    # whole-source total is 50% (1/2), but ui package is 0%
    assert cov.overall.line_percent == 50.0
    assert view["groups"]["qt/ui"]["line_percent"] == 0.0  # type: ignore[index]
    assert view["groups"]["core"]["line_percent"] == 100.0  # type: ignore[index]


def test_qt_enabled_workbench_xml_separates_ui_package(tmp_path: pathlib.Path) -> None:
    # UI-enabled: ui files covered
    xml = tmp_path / "coverage-ui.xml"
    xml.write_text(
        """<?xml version="1.0" ?><coverage><packages>
        <package name="drilling_intelligence.core"><classes>
        <class filename="src/drilling_intelligence/core/units.py"><lines><line number="1" hits="1"/></lines></class>
        </classes></package>
        <package name="drilling_intelligence.ui"><classes>
        <class filename="src/drilling_intelligence/ui/app.py"><lines><line number="1" hits="1"/><line number="2" hits="1"/></lines></class>
        </classes></package>
        </packages></coverage>""",
        encoding="utf-8",
    )
    cov = mod.read_cobertura(xml)
    view = mod.build_view(cov)
    assert view["groups"]["qt/ui"]["line_percent"] == 100.0  # type: ignore[index]
    # overall is not the same as ui package
    assert (
        cov.overall.line_percent != view["groups"]["qt/ui"]["line_percent"]
        or cov.overall.statements != view["groups"]["qt/ui"]["statements"]
    )  # type: ignore[index]


def test_correct_separation_of_ui_package_from_whole_source(tmp_path: pathlib.Path) -> None:
    xml = tmp_path / "coverage-ui.xml"
    xml.write_text(
        """<?xml version="1.0" ?><coverage><packages>
        <package name="drilling_intelligence.ui"><classes>
        <class filename="src/drilling_intelligence/ui/lifetime.py"><lines><line number="1" hits="1"/><line number="2" hits="0"/></lines></class>
        </classes></package>
        <package name="drilling_intelligence.core"><classes>
        <class filename="src/drilling_intelligence/core/units.py"><lines><line number="1" hits="1"/></lines></class>
        </classes></package>
        </packages></coverage>""",
        encoding="utf-8",
    )
    cov = mod.read_cobertura(xml)
    view = mod.build_view(cov)
    overall = float(view["overall"]["line_percent"])  # type: ignore[index]
    ui_pkg = float(view["groups"]["qt/ui"]["line_percent"])  # type: ignore[index]
    # whole-source 66% (2/3), ui package 50% (1/2) — they are distinct numbers
    assert overall == 66.67 or abs(overall - 66.67) < 0.01
    assert ui_pkg == 50.0
    # The CLI must not infer ui package from whole-source total
    assert overall != ui_pkg


def test_windows_and_posix_forms_of_same_file(tmp_path: pathlib.Path) -> None:
    assert mod.group_of("src/drilling_intelligence/review/service.py") == "review"
    assert mod.group_of("src\\drilling_intelligence\\review\\service.py") == "review"
    assert mod.group_of("review/service.py") == "review"
