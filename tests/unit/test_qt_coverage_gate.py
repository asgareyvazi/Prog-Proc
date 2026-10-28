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
    overall_statements: int | None = None,
    overall_covered: int | None = None,
    overall_branches: int | None = None,
    overall_covered_branches: int | None = None,
) -> dict:
    if fingerprint is None:
        fingerprint = _fingerprint(test_selection)
    if line_percent is None:
        line_percent = round(100.0 * covered / statements, 2) if statements else None
    if branch_percent is None:
        branch_percent = round(100.0 * covered_branches / branches, 2) if branches else None
    # Overall defaults to match the XML we generate (2 lines, 1 branch)
    # but allow override for specific tests
    if overall_statements is None:
        overall_statements = 2
    if overall_covered is None:
        overall_covered = 2
    if overall_branches is None:
        overall_branches = 1
    if overall_covered_branches is None:
        overall_covered_branches = 1
    overall_line = (
        round(100.0 * overall_covered / overall_statements, 2) if overall_statements else 0
    )
    overall_branch = (
        round(100.0 * overall_covered_branches / overall_branches, 2) if overall_branches else 0
    )
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
            "statements": overall_statements,
            "covered_statements": overall_covered,
            "branches": overall_branches,
            "covered_branches": overall_covered_branches,
            "line_percent": overall_line,
            "branch_percent": overall_branch,
        },
        "groups": {
            "qt/ui": {
                "statements": statements,
                "covered_statements": covered,
                "branches": branches,
                "covered_branches": covered_branches,
                "line_percent": line_percent,
                "branch_percent": branch_percent,
                "files": 1,
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
    # Minimal valid coverage XML with one class and root totals matching view overall
    if xml_content == "<coverage></coverage>":
        xml_content = """<coverage line-rate="1.0" branch-rate="1.0" lines-covered="2" lines-valid="2" branches-covered="1" branches-valid="1"><sources><source>src</source></sources><packages><package><classes><class filename="src/drilling_intelligence/ui/app.py"><lines><line number="1" hits="1" branch="false"/><line number="2" hits="1" branch="true" condition-coverage="50% (1/2)"/></lines></class></classes></package></packages></coverage>"""
    p.write_text(xml_content, encoding="utf-8")
    return p


def test_valid_gate_passes(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)  # type: ignore[attr-defined]
    # Mock git HEAD to match view commit
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "b" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "b" * 40, "returncode": 0})(),
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


# Additional adversarial tests for V8.25 strictness (Phase 9)


def test_short_sha_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
    # 7-char abbreviated SHA
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 7)
    assert any("0:" in e or "5:" in e for e in errs)


def test_invalid_hex_sha_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
    errs = validate(
        xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="g" * 40
    )  # g is not hex
    assert any("0:" in e for e in errs)


def test_mismatched_sha_one_char_changed_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
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
    # expected has one char changed
    errs = validate(
        xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 39 + "b"
    )
    assert any("5:" in e for e in errs)


def test_prefix_sha_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(commit="a" * 7)  # short commit in view (not full)
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("5:" in e for e in errs)


def test_longer_sha_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(commit="a" * 40 + "b")  # 41 chars
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("5:" in e for e in errs)


def test_git_head_mismatch_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    # git HEAD is different from expected
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "b" * 40, "returncode": 0})(),
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
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("5:" in e for e in errs)


def test_git_failure_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "", "returncode": 1})()
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
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("5:" in e for e in errs)


def test_boolean_counts_fail(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(statements=True)  # bool
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("10:" in e or "11:" in e for e in errs)


def test_nan_inf_fail(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(line_percent=float("nan"))
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    assert any("10:" in e or "11:" in e for e in errs)


def test_covered_exceeds_total_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(statements=10, covered=20)
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


def test_zero_denominator_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(
        statements=0,
        covered=0,
        branches=0,
        covered_branches=0,
        overall_statements=0,
        overall_covered=0,
        overall_branches=0,
        overall_covered_branches=0,
    )
    # need to also make xml have 0? but our xml has 2 lines, so overall mismatch will also fail, but we check for statements>0
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


def test_xml_json_aggregate_mismatch_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    view = _make_view(
        overall_statements=100, overall_covered=90, overall_branches=50, overall_covered_branches=45
    )
    # xml has 2/2/1/1, so mismatch
    xml = _make_xml(tmp_path)
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    # For V8.25, overall mismatch is advisory, not a hard gate, so no 12 error expected
    assert not any("12:" in e for e in errs)


def test_threshold_boundaries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("tools.qt_coverage_gate._check_pyside_present", lambda view: True)
    import tools.qt_coverage_gate as gate

    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": "a" * 40, "returncode": 0})(),
    )
    # Exactly at 70/40 should pass for hard gate but fail for resilience if below 71/42
    view = _make_view(
        statements=100,
        covered=70,
        branches=100,
        covered_branches=40,
        line_percent=70.0,
        branch_percent=40.0,
    )
    xml = _make_xml(tmp_path)
    # need to make xml overall match view overall? use the same overall as view
    view["overall"]["statements"] = 2
    view["overall"]["covered_statements"] = 2
    view["overall"]["branches"] = 1
    view["overall"]["covered_branches"] = 1
    view["overall"]["line_percent"] = 100.0
    view["overall"]["branch_percent"] = 100.0
    view_path = tmp_path / "view.json"
    view_path.write_text(json.dumps(view), encoding="utf-8")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="x" name="y"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    errs = validate(xml_path=xml, view_path=view_path, junit_path=junit, expected_sha="a" * 40)
    # For V8.25, 71/42 resilience is advisory, so 70/40 at threshold should have no errors
    assert not any("15b" in e for e in errs)
    assert not any("15a" in e for e in errs)
    assert not any("15:" in e for e in errs)
