"""CI test totals stay numeric when a suite fails midway."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tools.pytest_totals import (
    ExitRecordError,
    disagreements,
    format_totals,
    main,
    notice_lines,
    publish,
    read_exit_file,
    skip_is_expected,
    totals_from_junit,
    totals_from_stats,
)

_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="1" failures="2" skipped="3" tests="8" time="1.500">
  <testcase classname="a" name="ok"/>
  <testcase classname="a" name="bad"><failure message="nope"/></testcase>
  <testcase classname="a" name="bad2"><failure message="nope"/></testcase>
  <testcase classname="a" name="boom"><error message="setup"/></testcase>
  <testcase classname="a" name="skip"><skipped type="pytest.skip" message="later"/></testcase>
  <testcase classname="a" name="xfail"><skipped type="pytest.xfail" message="known"/></testcase>
  <testcase classname="a" name="xpass">
    <skipped message="xfail-marked test passes unexpectedly"/>
  </testcase>
</testsuite>
"""


def _stats_file(path: Path, **overrides: int | float) -> None:
    payload = {
        "collected": 8,
        "passed": 2,
        "failed": 2,
        "errors": 1,
        "skipped": 1,
        "xfailed": 1,
        "xpassed": 1,
        "exit": 1,
        "runtime": 1.5,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_junit_totals_split_skips_from_xfail_and_xpass(tmp_path: Path) -> None:
    path = tmp_path / "pytest-junit.xml"
    path.write_text(_JUNIT, encoding="utf-8")
    totals = totals_from_junit(path, exit_code=1)
    assert totals["collected"] == 8
    assert totals["failed"] == 2
    assert totals["errors"] == 1
    assert totals["skipped"] == 1
    assert totals["xfailed"] == 1
    assert totals["xpassed"] == 1
    assert "collected: 8" in format_totals(totals)


def test_session_stats_count_xpass_that_junit_folds_into_passed() -> None:
    totals = totals_from_stats(
        {"passed": [object()], "xpassed": [object()], "skipped": [object()]},
        collected=3,
        exit_code=0,
        runtime=0.2,
    )
    junit = {
        "collected": 3,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "xfailed": 0,
        "xpassed": 0,
    }
    assert totals["xpassed"] == 1
    assert disagreements(totals, junit) == []


def test_a_missing_record_fails_even_if_the_other_file_exists(tmp_path: Path) -> None:
    junit = tmp_path / "pytest-junit.xml"
    junit.write_text(_JUNIT, encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = publish(tmp_path / "missing.json", junit, None, summary)
    assert code == 1
    text = summary.read_text(encoding="utf-8")
    assert "stats: missing" in text
    assert "junit: present" in text


def _clean_junit(tests: int = 2, failed: int = 0, skipped: int = 0) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuite name="pytest" errors="0" failures="{failed}" skipped="{skipped}" '
        f'tests="{tests}" time="0.100">'
        '<testcase classname="tests.unit.demo" name="ok"/>'
        "</testsuite>"
    )


def test_a_supplied_missing_exit_file_is_not_replaced_by_session_exit(tmp_path: Path) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    stats.write_text(
        json.dumps(
            {
                "collected": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 0.1,
            }
        ),
        encoding="utf-8",
    )
    junit.write_text(_clean_junit(tests=1), encoding="utf-8")
    summary = tmp_path / "summary.md"
    code = publish(stats, junit, tmp_path / "missing-exit.txt", summary)
    assert code == 1
    assert "exit file missing" in summary.read_text(encoding="utf-8")


def test_shell_and_session_exits_are_compared(tmp_path: Path) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats, collected=2, passed=2, failed=0, errors=0, skipped=0, xfailed=0, xpassed=0, exit=1
    )
    junit.write_text(_clean_junit(), encoding="utf-8")
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert publish(stats, junit, exit_file, summary) == 1
    text = summary.read_text(encoding="utf-8")
    assert "shell 0 != session 1" in text
    exit_file.write_text("\n", encoding="utf-8")
    try:
        read_exit_file(exit_file)
    except ExitRecordError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("empty exit file must fail")
    exit_file.write_text("nope\n", encoding="utf-8")
    try:
        read_exit_file(exit_file)
    except ExitRecordError as exc:
        assert "integer" in str(exc)
    else:
        raise AssertionError("malformed exit file must fail")


def test_unexpected_xpass_and_skip_fail_an_otherwise_green_record(tmp_path: Path) -> None:
    assert skip_is_expected(
        "tests.ui.test_workbench",
        "test_real",
        "QtWidgets unavailable for UI tests: libGL",
    )
    assert not skip_is_expected("tests.integration.test_x", "test_y", "symlinks")
    assert skip_is_expected(
        "tests.integration.test_path_and_process_boundary",
        "test_symlinks_pointing_outside_the_scan_root_are_never_followed",
        "this platform does not support symlinks",
    )
    assert skip_is_expected(
        "tests.integration.test_workspace_relocation_safety_v46",
        "test_a_stale_connection_after_a_move_is_refused_and_recreates_nothing",
        "this platform will not move an open database file: [WinError 32]",
    )
    assert not skip_is_expected(
        "tests.integration.test_workspace_relocation_safety_v46",
        "test_a_stale_connection_after_a_move_is_refused_and_recreates_nothing",
        "permission denied",
    )
    assert not skip_is_expected(
        "tests.ui.test_workbench", "test_real", "skipped for another reason"
    )
    assert not skip_is_expected(
        "tests.unit.test_ui_helper", "test_x", "QtWidgets unavailable for UI tests: libGL"
    )
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats,
        collected=1,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        xfailed=0,
        xpassed=1,
        exit=0,
    )
    junit.write_text(_clean_junit(tests=1), encoding="utf-8")
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 1


def test_a_known_ui_skip_can_still_publish_a_green_record(tmp_path: Path) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats,
        collected=2,
        passed=1,
        failed=0,
        errors=0,
        skipped=1,
        xfailed=0,
        xpassed=0,
        exit=0,
    )
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
        <testsuite name="pytest" errors="0" failures="0" skipped="1" tests="2" time="0.1">
          <testcase classname="tests.unit.demo" name="ok"/>
          <testcase classname="tests.ui.test_workbench" name="test_window">
            <skipped type="pytest.skip" message="QtWidgets unavailable for UI tests: libGL"/>
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 0


def test_real_pytest_accounts_for_pass_fail_skip_xfail_and_xpass(tmp_path: Path) -> None:
    suite = tmp_path / "test_outcomes.py"
    suite.write_text(
        "import pytest\n"
        "def test_ok():\n"
        "    assert True\n"
        "def test_bad():\n"
        "    assert False\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_skip():\n"
        "    pass\n"
        "@pytest.mark.xfail(reason='known')\n"
        "def test_xfail():\n"
        "    assert False\n"
        "@pytest.mark.xfail(reason='surprise')\n"
        "def test_xpass():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    totals = tmp_path / "totals.json"
    junit = tmp_path / "junit.xml"
    env = os.environ.copy()
    env["PYTEST_TOTALS_PATH"] = str(totals)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-p",
            "tools.pytest_totals",
            "--noconftest",
            "-o",
            "addopts=",
            "--junitxml",
            str(junit),
            "-q",
            "--tb=no",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 1
    payload = json.loads(totals.read_text(encoding="utf-8"))
    assert payload["collected"] == 5
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["skipped"] == 1
    assert payload["xfailed"] == 1
    assert payload["xpassed"] == 1
    assert (
        payload["passed"]
        + payload["failed"]
        + payload["errors"]
        + payload["skipped"]
        + payload["xfailed"]
        + payload["xpassed"]
        == payload["collected"]
    )
    parsed = totals_from_junit(junit, exit_code=1)
    assert disagreements(payload, parsed) == []


def test_zero_collected_malformed_exit_and_unreadable_records_fail_closed(
    tmp_path: Path, capsys: object
) -> None:
    stats = tmp_path / "pytest-totals.json"
    junit = tmp_path / "pytest-junit.xml"
    _stats_file(
        stats,
        collected=0,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        xfailed=0,
        xpassed=0,
        exit=0,
    )
    junit.write_text(_clean_junit(tests=0), encoding="utf-8")
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("0\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 1
    exit_file.write_text("0\nextra\n", encoding="utf-8")
    try:
        read_exit_file(exit_file)
    except ExitRecordError as exc:
        assert "integer" in str(exc)
    else:
        raise AssertionError("extra exit-file content must fail")
    _stats_file(
        stats,
        collected=1,
        passed=0,
        failed=1,
        errors=0,
        skipped=0,
        xfailed=0,
        xpassed=0,
        exit=1,
    )
    junit.write_text(_clean_junit(tests=1, failed=1), encoding="utf-8")
    exit_file.write_text("1\n", encoding="utf-8")
    summary = tmp_path / "summary.md"
    assert publish(stats, junit, exit_file, summary) == 1
    assert "shell 1 != session" not in summary.read_text(encoding="utf-8")
    sentinel = "quoted-dsn-marker"
    (tmp_path / "broken.json").write_text('{"leak":"' + sentinel, encoding="utf-8")
    (tmp_path / "broken.xml").write_text("<not-xml " + sentinel, encoding="utf-8")
    code = main(["--stats", str(tmp_path / "broken.json"), "--junit", str(tmp_path / "broken.xml")])
    assert code == 1
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "unreadable" in captured.out
    assert sentinel not in captured.out
    assert notice_lines("collected: 4\npassed: 3\nshell_exit: 0\n") == [
        "collected: 4",
        "passed: 3",
        "shell_exit: 0",
    ]


def test_a_fixture_error_and_a_strict_xpass_are_real_pytest_outcomes(tmp_path: Path) -> None:
    suite = tmp_path / "test_edges.py"
    suite.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    raise RuntimeError('fixture-setup')\n"
        "def test_setup(broken):\n"
        "    pass\n"
        "@pytest.mark.xfail(strict=True, reason='stale')\n"
        "def test_strict_xpass():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    totals = tmp_path / "totals.json"
    env = os.environ.copy()
    env["PYTEST_TOTALS_PATH"] = str(totals)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-p",
            "tools.pytest_totals",
            "--noconftest",
            "-o",
            "addopts=",
            "-q",
            "--tb=no",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode != 0
    payload = json.loads(totals.read_text(encoding="utf-8"))
    assert payload["errors"] >= 1
    assert payload["failed"] >= 1
    accounted = (
        payload["passed"]
        + payload["failed"]
        + payload["errors"]
        + payload["skipped"]
        + payload["xfailed"]
        + payload["xpassed"]
    )
    assert accounted == payload["collected"]


def test_a_real_shell_exit_is_not_replaced_when_the_session_disagrees(tmp_path: Path) -> None:
    suite = tmp_path / "test_real_exit.py"
    suite.write_text("def test_bad():\n    assert False\n", encoding="utf-8", newline="\n")
    stats = tmp_path / "totals.json"
    junit = tmp_path / "junit.xml"
    exit_file = tmp_path / "pytest-exit.txt"
    summary = tmp_path / "summary.md"
    runner = tmp_path / "record_exit.py"
    runner.write_text(
        "\n".join(
            [
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "exe, suite, junit, exit_file = sys.argv[1:]",
                "completed = subprocess.run(",
                "    [exe, '-m', 'pytest', suite, '-p', 'tools.pytest_totals',",
                "     '--noconftest', '-o', 'addopts=', '-q', '--tb=no', f'--junitxml={junit}'],",
                "    check=False,",
                ")",
                "Path(exit_file).write_text(f'{completed.returncode}\\n', encoding='utf-8', newline='\\n')",
                "raise SystemExit(completed.returncode)",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    env = os.environ.copy()
    env["PYTEST_TOTALS_PATH"] = str(stats)
    env.pop("PYTEST_TOTALS_DISABLE", None)
    completed = subprocess.run(
        [sys.executable, str(runner), sys.executable, str(suite), str(junit), str(exit_file)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )
    recorded = exit_file.read_text(encoding="utf-8")
    assert recorded == f"{completed.returncode}\n"
    assert completed.returncode != 0
    payload = json.loads(stats.read_text(encoding="utf-8"))
    assert payload["exit"] == completed.returncode
    assert payload["failed"] >= 1
    before = (stats.read_bytes(), junit.read_bytes(), exit_file.read_bytes())
    assert (
        main(
            [
                "--stats",
                str(stats),
                "--junit",
                str(junit),
                "--exit-file",
                str(exit_file),
                "--summary",
                str(summary),
            ]
        )
        == 1
    )
    assert (stats.read_bytes(), junit.read_bytes(), exit_file.read_bytes()) == before
    agreed = summary.read_text(encoding="utf-8")
    assert "shell_exit: 1" in agreed or f"shell_exit: {completed.returncode}" in agreed
    assert "session_exit:" in agreed
    assert f"shell {completed.returncode} != session" not in agreed

    exit_file.write_text("0\n", encoding="utf-8")
    assert (
        main(
            [
                "--stats",
                str(stats),
                "--junit",
                str(junit),
                "--exit-file",
                str(exit_file),
                "--summary",
                str(summary),
            ]
        )
        == 1
    )
    mismatch = summary.read_text(encoding="utf-8")
    assert "shell 0 != session 1" in mismatch
    assert exit_file.read_text(encoding="utf-8") == "0\n"
    assert json.loads(stats.read_text(encoding="utf-8"))["exit"] == completed.returncode
    assert "session_exit: 1" in mismatch

    for contents, marker in (("\n", "empty"), ("0\n1\n", "integer"), ("nope\n", "integer")):
        exit_file.write_text(contents, encoding="utf-8")
        summary.write_text("", encoding="utf-8")
        assert (
            main(
                [
                    "--stats",
                    str(stats),
                    "--junit",
                    str(junit),
                    "--exit-file",
                    str(exit_file),
                    "--summary",
                    str(summary),
                ]
            )
            == 1
        )
        hidden = summary.read_text(encoding="utf-8")
        assert marker in hidden
        assert "passed:" not in hidden
        assert exit_file.read_text(encoding="utf-8") == contents

    missing = tmp_path / "missing-exit.txt"
    summary.write_text("", encoding="utf-8")
    assert (
        main(
            [
                "--stats",
                str(stats),
                "--junit",
                str(junit),
                "--exit-file",
                str(missing),
                "--summary",
                str(summary),
            ]
        )
        == 1
    )
    concealed = summary.read_text(encoding="utf-8")
    assert "exit file missing" in concealed
    assert "passed:" not in concealed
    assert "junit: present" in concealed
    assert "stats: present" in concealed


def test_standalone_publish_without_an_exit_file_keeps_its_contract(tmp_path: Path) -> None:
    suite = tmp_path / "test_ok.py"
    suite.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    stats = tmp_path / "totals.json"
    junit = tmp_path / "junit.xml"
    env = os.environ.copy()
    env["PYTEST_TOTALS_PATH"] = str(stats)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-p",
            "tools.pytest_totals",
            "--noconftest",
            "-o",
            "addopts=",
            "-q",
            "--tb=no",
            f"--junitxml={junit}",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert completed.returncode == 0
    assert main(["--stats", str(stats), "--junit", str(junit)]) == 0


def test_malformed_records_fail_closed_without_the_body_or_a_traceback(
    tmp_path: Path, capsys: object
) -> None:
    junit = tmp_path / "pytest-junit.xml"
    junit.write_text(_JUNIT, encoding="utf-8")
    summary = tmp_path / "summary.md"
    sentinel = "SECRET_SENTINEL"
    cases = {
        "array": "[" + json.dumps(sentinel) + "]",
        "null": "null",
        "string-count": json.dumps(
            {
                "collected": "1\n::error::" + sentinel,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 0,
            }
        ),
        "negative": json.dumps(
            {
                "collected": -1,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 0,
            }
        ),
        "bool": json.dumps(
            {
                "collected": True,
                "passed": 0,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "exit": 0,
                "runtime": 0,
            }
        ),
    }
    expected = {
        "array": "stats root is not an object",
        "null": "stats root is not an object",
        "string-count": "stats collected is not a non-negative integer",
        "negative": "stats collected is not a non-negative integer",
        "bool": "stats collected is not a non-negative integer",
    }
    for name, body in cases.items():
        stats = tmp_path / f"{name}.json"
        stats.write_text(body, encoding="utf-8")
        assert publish(stats, junit, None, summary) == 1
        captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        assert sentinel not in captured
        assert "Traceback" not in captured
        assert expected[name] in captured
        assert "record: unreadable" in captured
    broken = tmp_path / "broken.json"
    broken.write_text('{"collected": ' + sentinel, encoding="utf-8")
    assert publish(broken, junit, None, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert sentinel not in captured
    assert "stats is not json" in captured
    html = tmp_path / "page.xml"
    html.write_text("<html>" + sentinel + "</html>", encoding="utf-8")
    _stats_file(tmp_path / "ok.json")
    assert publish(tmp_path / "ok.json", html, None, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert sentinel not in captured
    assert "junit root is not a testsuite" in captured
    assert "Traceback" not in captured
    not_xml = tmp_path / "not.xml"
    not_xml.write_text(sentinel + " <not xml", encoding="utf-8")
    assert publish(tmp_path / "ok.json", not_xml, None, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert sentinel not in captured
    assert "junit is not xml" in captured


def _valid_stats(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "exit": 0,
        "runtime": 0.1,
    }
    payload.update(overrides)
    return payload


def test_invalid_stats_roots_and_field_types_fail_closed_without_the_body(
    tmp_path: Path, capsys: object
) -> None:
    junit = tmp_path / "pytest-junit.xml"
    junit.write_text(_clean_junit(tests=1), encoding="utf-8")
    summary = tmp_path / "summary.md"
    sentinel = "password=SENTINEL_VALUE"
    long_secret = "token=" + ("Z" * 5000) + "SENTINEL_TAIL"
    cases = {
        "empty": ("", "stats is not json"),
        "syntax": ("{not json " + sentinel, "stats is not json"),
        "null": ("null", "stats root is not an object"),
        "array": ("[" + json.dumps(sentinel) + "]", "stats root is not an object"),
        "string": (json.dumps(sentinel), "stats root is not an object"),
        "number": ("1", "stats root is not an object"),
        "missing": (json.dumps({"collected": 1}), "stats missing passed"),
        "float-count": (json.dumps(_valid_stats(collected=1.5)), "not a non-negative integer"),
        "list-count": (json.dumps(_valid_stats(passed=[1])), "not a non-negative integer"),
        "bool-exit": (json.dumps(_valid_stats(exit=True)), "not a non-negative integer"),
        "negative-runtime": (json.dumps(_valid_stats(runtime=-1)), "not a finite non-negative"),
        "infinite-runtime": (
            json.dumps(_valid_stats(runtime=0.1)).replace("0.1", "1e309"),
            "not a finite non-negative",
        ),
        "string-exit": (json.dumps(_valid_stats(exit="0")), "not a non-negative integer"),
        "hostile": (
            json.dumps(_valid_stats(collected=long_secret)),
            "not a non-negative integer",
        ),
    }
    for name, (body, expected) in cases.items():
        stats = tmp_path / f"{name}.json"
        stats.write_text(body, encoding="utf-8")
        assert publish(stats, junit, None, summary) == 1
        captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        assert "record: unreadable" in captured
        assert expected in captured
        assert "Traceback" not in captured
        assert "SENTINEL" not in captured
        assert long_secret not in captured
    bad_junit = tmp_path / "bad-count.xml"
    bad_junit.write_text(
        '<?xml version="1.0"?><testsuites><testsuite tests="nope"></testsuite></testsuites>',
        encoding="utf-8",
    )
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(_valid_stats()), encoding="utf-8")
    assert publish(ok, bad_junit, None, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "junit tests is not a non-negative integer" in captured
    assert "Traceback" not in captured
    empty_suite = tmp_path / "empty.xml"
    empty_suite.write_text('<?xml version="1.0"?><testsuites></testsuites>', encoding="utf-8")
    assert publish(ok, empty_suite, None, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "junit has no testsuite" in captured
    exit_file = tmp_path / "exit.txt"
    exit_file.write_text("", encoding="utf-8")
    assert publish(ok, junit, exit_file, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "exit file empty" in captured
    exit_file.write_text("1\n2\n", encoding="utf-8")
    assert publish(ok, junit, exit_file, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "exit file is not one integer" in captured
    mismatched = tmp_path / "mismatch.json"
    mismatched.write_text(json.dumps(_valid_stats(collected=2, passed=1)), encoding="utf-8")
    exit_file.write_text("0\n", encoding="utf-8")
    assert publish(mismatched, junit, exit_file, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "accounted" in captured
    secret_skip = tmp_path / "skip.xml"
    secret_skip.write_text(
        '<?xml version="1.0"?>'
        '<testsuite tests="1" failures="0" errors="0" skipped="1" time="0.1">'
        '<testcase classname="tests.unit.demo" name="hidden">'
        '<skipped type="pytest.skip" message="password=SENTINEL_VALUE later"/>'
        "</testcase></testsuite>",
        encoding="utf-8",
    )
    assert publish(ok, secret_skip, exit_file, summary) == 1
    captured = capsys.readouterr().out + summary.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "unexpected skip" in captured
    assert "SENTINEL_VALUE" not in captured
    assert "password=[redacted]" in captured


def test_a_disagreement_or_a_failed_suite_fails_the_record(tmp_path: Path) -> None:
    junit = tmp_path / "pytest-junit.xml"
    junit.write_text(_JUNIT, encoding="utf-8")
    stats = tmp_path / "pytest-totals.json"
    _stats_file(stats, failed=0)
    assert publish(stats, junit, None, None) == 1
    _stats_file(stats)
    exit_file = tmp_path / "pytest-exit.txt"
    exit_file.write_text("1\n", encoding="utf-8")
    assert publish(stats, junit, exit_file, None) == 1
