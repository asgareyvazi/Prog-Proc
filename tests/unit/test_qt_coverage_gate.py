"""Unit tests for qt_coverage_gate — adversarial cases."""

from __future__ import annotations

import json
from pathlib import Path

from tools.qt_coverage_gate import EXPECTED_SELECTION, _fingerprint, validate

# Helpers

EXPECTED_FP = _fingerprint(EXPECTED_SELECTION)


def _make_view(
    *,
    commit: str = "a" * 40,
    scope: str = "ui-test-selection",
    test_selection: str = EXPECTED_SELECTION,
    fingerprint: str | None = None,
    statements: int = 2447,
    covered: int = 1739,
    branches: int = 512,
    covered_branches: int = 229,
    line_percent: float | None = None,
    branch_percent: float | None = None,
    present: bool = True,
) -> dict:
    if fingerprint is None:
        fingerprint = _fingerprint(test_selection)
    if line_percent is None:
        line_percent = round(100.0 * covered / statements, 2) if statements else None
    if branch_percent is None:
        branch_percent = round(100.0 * covered_branches / branches, 2) if branches else None
    return {
        "commit": commit,
        "measurement_scope": scope,
        "test_selection": test_selection,
        "test_selection_fingerprint": fingerprint,
        "platform": "Linux-6.8",
        "python_version": "3.11.0",
        "coverage_version": "7.0",
        "optional_dependencies": [{"name": "ui", "present": present, "groups": ["qt/ui"]}],
        "overall": {
            "statements": 10000,
            "covered_statements": 5000,
            "branches": 2000,
            "covered_branches": 1000,
            "line_percent": 50.0,
            "branch_percent": 50.0,
        },
        "groups": {
            "qt/ui": {
                "statements": statements,
                "covered_statements": covered,
                "branches": branches,
                "covered_branches": covered_branches,
                "line_percent": line_percent,
                "branch_percent": branch_percent,
                "files": 10,
                "present": True,
                "measurement_state": "measured",
            },
            "core": {
                "statements": 100,
                "covered_statements": 50,
                "branches": 20,
                "covered_branches": 10,
                "line_percent": 50.0,
                "branch_percent": 50.0,
                "files": 1,
                "present": True,
                "measurement_state": "measured",
            },
        },
    }


def _make_xml(tmp_path: Path, xml_content: str = "<coverage></coverage>") -> Path:
    p = tmp_path / "coverage-ui.xml"
    # Minimal valid coverage XML with one class
    if xml_content == "<coverage></coverage>":
        xml_content = """<coverage><sources><source>src</source></sources><packages><package><classes><class filename="src/drilling_intelligence/ui/app.py"><lines><line number="1" hits="1" branch="false"/><line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/></lines></class></classes></package></packages></coverage>"""
    p.write_text(xml_content, encoding="utf-8")
    return p


def test_valid_gate_passes(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)  # type: ignore[attr-defined]
    # Mock git HEAD to match view commit
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )  # type: ignore[attr-defined]
    view = _make_view(commit="a" * 40)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="test_foo" name="test_bar"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert errs == []


def test_wrong_sha_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "b" * 40})()
    )
    view = _make_view(commit="a" * 40)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="b" * 40)
    assert any("5:" in e for e in errs)


def test_empty_fingerprint_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view(fingerprint="")
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("8:" in e for e in errs)


def test_wrong_selector_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view(test_selection="some other selection")
    view["test_selection_fingerprint"] = _fingerprint(
        "some other selection"
    )  # make fingerprint match wrong selection
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("7:" in e for e in errs)


def test_pyside_absent_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: False)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view(present=False)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("9:" in e for e in errs)


def test_empty_coverage_group_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view(statements=0, covered=0, branches=0, covered_branches=0)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("10:" in e for e in errs)


def test_invalid_percentages_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view(line_percent=999, branch_percent=-5)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("11:" in e for e in errs)


def test_below_threshold_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    # 69% line, below 70 and 71
    view = _make_view(covered=1688, statements=2447, covered_branches=229, branches=512)  # ~69%
    # force line_percent to 69
    view["groups"]["qt/ui"]["line_percent"] = 69.0
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("15" in e for e in errs)


def test_missing_junit_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view()
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    missing = tmp_path / "missing.xml"
    errs = validate(xml_path=xml, view_path=view_path, junit_path=missing, expected_sha="a" * 40)
    assert any("13:" in e for e in errs)


def test_unapproved_skips_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view()
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    # Qt-enabled skip with QtWidgets unavailable message — should be flagged when Qt present
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="tests.unit.test_ui_coverage_boost_v23" name="test_main_window_offscreen_init_v23"><skipped message="QtWidgets unavailable: foo"/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    # Our gate's skip check uses unexpected_skips_qt_enabled which will flag this
    # But we mock _check_pyside_present to True, and junit contains that skip, so it should fail
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("13:" in e for e in errs)


def test_xml_json_disagreement_missing_xml(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "a" * 40})()
    )
    view = _make_view()
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    missing_xml = tmp_path / "missing.xml"
    errs = validate(
        xml_path=missing_xml, view_path=view_path, junit_path=None, expected_sha="a" * 40
    )
    assert any("1:" in e for e in errs)


def test_stale_report_wrong_sha(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "b" * 40})()
    )
    view = _make_view(commit="a" * 40)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="c" * 40)
    assert any("5:" in e for e in errs)
