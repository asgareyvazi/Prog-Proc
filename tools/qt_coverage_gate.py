#!/usr/bin/env python3
"""Strict Qt/UI coverage gate for V8.25.

Validates that the Qt/UI coverage measurement is genuine, fail-closed,
and matches the exact commit that produced it. All invariants are exercised
and covered by unit tests in test_qt_coverage_gate.py.

Usage:
  python tools/qt_coverage_gate.py --xml coverage-ui.xml --view coverage-ui-view.json \
    --junit pytest-ui-junit.xml --expected-sha <sha> --scope ui-test-selection

Exit 0 only when all checks pass; any missing, mismatched, or below-threshold
value exits 1 with ::error:: annotations and never silently warns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap for direct script execution (``python tools/qt_coverage_gate.py``)
# When invoked as a file, ``__package__`` is empty and ``tools`` is not on
# sys.path as a package. The gate needs ``tools.pytest_totals`` (which itself
# imports ``drilling_intelligence`` from ``src/``) and ``tools.coverage_paths``.
# Insert both the repository root and ``src`` so both the in-process
# (``pytest`` imports ``tools.qt_coverage_gate``) and the direct CLI
# (``python tools/qt_coverage_gate.py``) paths resolve identically.
# This is the documented, minimally invasive bootstrap for V8.27.
# ---------------------------------------------------------------------------
if __package__ in (None, ""):  # direct execution
    _REPO_ROOT_GATE = Path(__file__).resolve().parents[1]
    _SRC_ROOT_GATE = _REPO_ROOT_GATE / "src"
    for _P in (str(_REPO_ROOT_GATE), str(_SRC_ROOT_GATE)):
        if _P not in sys.path:
            sys.path.insert(0, _P)

# Shared path normalization — must match coverage_report's contract.
try:
    from tools.coverage_paths import (  # type: ignore[import-not-found]
        PACKAGE_MARKER,  # type: ignore[import-not-found]
        group_of,
        relative_to_package,
        xml_filename_to_relative,
    )
except ImportError:
    # Fallback when bootstrap hasn't yet run (e.g. import order)
    _FALLBACK_ROOT = Path(__file__).resolve().parents[1]
    _FALLBACK_SRC = _FALLBACK_ROOT / "src"
    for _P in (str(_FALLBACK_ROOT), str(_FALLBACK_SRC)):
        if _P not in sys.path:
            sys.path.insert(0, _P)
    from tools.coverage_paths import (  # type: ignore[no-redef, import-not-found]
        PACKAGE_MARKER,  # type: ignore[no-redef]
        group_of,
        relative_to_package,
        xml_filename_to_relative,
    )

EXPECTED_SELECTION = (
    "tests/ui + tests/unit/test_qt_termination.py + tests/unit/test_qt_termination_edge.py "
    "+ tests/unit/test_qt_lifetime_contract.py + tests/unit/test_qt_wait_matrix.py "
    "+ tests/unit/test_qt_workbench_policy.py + tests/unit/test_ui_coverage_boost.py "
    "+ tests/unit/test_ui_coverage_boost_extra.py + tests/unit/test_ui_coverage_boost_v21.py "
    "+ tests/unit/test_main_window_mocked_coverage.py + tests/unit/test_ui_coverage_boost_v22.py "
    "+ tests/unit/test_ui_coverage_boost_v23.py (Qt offscreen)"
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _fingerprint(test_selection: str) -> str:
    return hashlib.sha256(test_selection.encode("utf-8")).hexdigest()[:12]


def _is_full_sha(value: str) -> bool:
    return bool(_HEX40.fullmatch(value))


def _read_xml(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()  # noqa: S314
    except ET.ParseError as exc:
        raise ValueError(f"coverage XML is not valid XML: {exc}") from exc


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"view JSON is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("view JSON root is not an object")
    return data


def _check_pyside_present(view: dict) -> bool:
    deps = view.get("optional_dependencies")
    if not isinstance(deps, list):
        return False
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        if dep.get("name") == "ui":
            # Require explicit boolean True, not truthy
            return dep.get("present") is True
    return False


def _strict_int(value: object, label: str, errors: list[str], gname: str) -> int | None:
    if isinstance(value, bool):
        errors.append(f"11: {gname} {label} is bool, expected int")
        return None
    if not isinstance(value, int):
        errors.append(f"11: {gname} {label} is not int: {value!r}")
        return None
    if value < 0:
        errors.append(f"11: {gname} {label} is negative: {value!r}")
        return None
    return value


def _strict_percent(value: object, label: str, errors: list[str], gname: str) -> float | None:
    if isinstance(value, bool):
        errors.append(f"11: {gname} {label} is bool, expected number")
        return None
    if not isinstance(value, (int, float)):
        errors.append(f"11: {gname} {label} not numeric: {value!r}")
        return None
    f = float(value)
    if not math.isfinite(f):
        errors.append(f"11: {gname} {label} is non-finite: {value!r}")
        return None
    if f < 0 or f > 100:
        errors.append(f"11: {gname} {label} out of range [0,100]: {value!r}")
        return None
    return f


def _parse_xml_totals(xml_root: ET.Element) -> dict[str, int | float | None]:
    """Extract product-filtered totals from coverage XML.

    Must match :func:`tools.coverage_report.read_cobertura` and
    :func:`tools.coverage_paths.relative_to_package` so that the JSON view's
    ``overall`` (product-only) is compared with the same population in XML.

    Handling:
    - Normalizes ``\\`` to ``/``.
    - Inspects ``<sources>`` to determine ``rooted_in_package`` (any source
      containing ``drilling_intelligence``). This determines whether a bare
      ``ui/foo.py`` is a plausible product path or an ambiguous non-product
      path that must be rejected.
    - Empty/missing ``filename`` is not a product file.
    - Branch counts are derived from ``condition-coverage="50% (1/2)"`` in the
      same deterministic way as the producer; malformed values are treated as
      0/0 but will be caught elsewhere if totals mismatch.
    - If no product classes are found, we return zero totals rather than
      silently falling back to "all classes" — the caller will then fail with
      an actionable error (covers the ``view 9 > xml 0`` case).
    """

    sources = [(el.text or "").strip() for el in xml_root.iter("source") if el.text]
    rooted_in_package = any("drilling_intelligence" in s for s in sources)

    # Single, correct product-class selection (mirrors coverage_report.read_cobertura):
    # - If filename contains drilling_intelligence/, it's product regardless of sources.
    # - Else if rooted_in_package is True, a bare "ui/foo.py" is product if relative_to_package maps it.
    # - Else (not rooted, no marker) it's ambiguous/non-product and we skip (fail closed).
    product_classes: list[ET.Element] = []
    for cls in xml_root.iter("class"):
        fname = cls.get("filename") or ""
        if not fname:
            continue
        if PACKAGE_MARKER in fname.replace("\\", "/"):
            product_classes.append(cls)
            continue
        if rooted_in_package:
            rel2 = relative_to_package(fname)
            if rel2 is not None:
                product_classes.append(cls)

    # Do not fall back to "all classes" — if no product files, totals are zero
    # and the caller will report mismatch (e.g. view 9 > xml 0).
    lines_valid = 0
    lines_covered = 0
    branches_valid = 0
    branches_covered = 0
    for cls in product_classes:
        for line in cls.iter("line"):
            lines_valid += 1
            hits = line.get("hits")
            try:
                h = int(hits) if hits is not None else 0
            except ValueError:
                h = 0
            if h > 0:
                lines_covered += 1
            if (line.get("branch") or "").lower() == "true":
                cc = line.get("condition-coverage") or ""
                inside = cc[cc.index("(") + 1 : cc.rindex(")")] if "(" in cc and ")" in cc else ""
                covered, _, total = inside.partition("/")
                try:
                    t = int(total)
                    cov = int(covered)
                except ValueError:
                    t = 0
                    cov = 0
                branches_valid += t
                branches_covered += cov

    line_rate = (lines_covered / lines_valid) if lines_valid else 0.0
    branch_rate = (branches_covered / branches_valid) if branches_valid else 0.0

    return {
        "lines_valid": lines_valid,
        "lines_covered": lines_covered,
        "branches_valid": branches_valid,
        "branches_covered": branches_covered,
        "line_rate": line_rate,
        "branch_rate": branch_rate,
        "_product_classes": product_classes,  # internal, for caller checks
        "_sources": sources,
        "_rooted": rooted_in_package,
    }


def validate(
    *,
    xml_path: Path,
    view_path: Path,
    junit_path: Path | None,
    expected_sha: str,
    scope: str = "ui-test-selection",
    test_selection: str = EXPECTED_SELECTION,
) -> list[str]:
    """Return list of error messages; empty means PASS."""

    errors: list[str] = []

    # 0. expected_sha must be full 40 hex
    if not isinstance(expected_sha, str) or not _is_full_sha(expected_sha):
        errors.append(f"0: expected_sha is not a full 40-char hex SHA: {expected_sha!r}")
        # Continue to collect other errors but SHA checks will also fail
        # Do not return early: still validate other artifacts for diagnostics

    # 1. coverage-ui.xml exists and parses
    if not xml_path.is_file():
        errors.append(f"1: coverage-ui.xml missing at {xml_path}")
        return errors
    try:
        xml_root = _read_xml(xml_path)
    except ValueError as exc:
        errors.append(f"1: {exc}")
        return errors
    if xml_root.tag != "coverage":
        errors.append(f"1: coverage XML root is {xml_root.tag!r}, expected 'coverage'")

    # 2. coverage-ui-view.json exists and parses
    if not view_path.is_file():
        errors.append(f"2: coverage view JSON missing at {view_path}")
        return errors
    try:
        view = _read_json(view_path)
    except (ValueError, TypeError) as exc:
        errors.append(f"2: {exc}")
        return errors

    # 5. Metadata SHA exactly matches expected_sha and git HEAD (strict)
    commit = view.get("commit")
    if not isinstance(commit, str) or not commit:
        errors.append("5: view commit is missing or not a string")
    elif not _is_full_sha(commit):
        errors.append(f"5: view commit is not a full 40-char hex SHA: {commit!r}")
    elif commit != expected_sha:
        errors.append(f"5: view commit {commit!r} != expected SHA {expected_sha!r}")

    # Also verify it matches actual HEAD when git is available — mandatory, fail if git unavailable
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        actual = result.stdout.strip()
        if result.returncode != 0 or not actual:
            errors.append(
                "5: git rev-parse HEAD failed or returned empty — HEAD verification mandatory"
            )
        elif not _is_full_sha(actual):
            errors.append(f"5: git HEAD is not a full 40-char hex SHA: {actual!r}")
        elif actual != expected_sha:
            errors.append(f"5: git HEAD {actual!r} != expected SHA {expected_sha!r}")
        elif actual != commit:
            errors.append(f"5: view commit {commit!r} != git HEAD {actual!r}")
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(
            f"5: git rev-parse HEAD failed with exception: {exc} — HEAD verification mandatory"
        )

    # 6. Scope equals ui-test-selection
    view_scope = view.get("measurement_scope")
    if not isinstance(view_scope, str) or view_scope != scope:
        errors.append(f"6: scope is {view_scope!r}, expected {scope!r}")

    # 7. Selected test population matches intended 12-file selector
    view_sel = view.get("test_selection")
    if not isinstance(view_sel, str) or view_sel != test_selection:
        errors.append(
            f"7: test_selection mismatch:\n expected: {test_selection!r}\n got: {view_sel!r}"
        )

    # 8. Fingerprint is non-empty and matches algorithm for recorded selection
    fp = view.get("test_selection_fingerprint")
    if not isinstance(fp, str) or not fp:
        errors.append("8: fingerprint is missing or not a non-empty string")
    else:
        if not isinstance(view_sel, str):
            errors.append("8: cannot verify fingerprint because test_selection is not a string")
        else:
            expected_fp = _fingerprint(view_sel)
            if fp != expected_fp:
                errors.append(
                    f"8: fingerprint {fp!r} != expected {expected_fp!r} for selection {view_sel!r}"
                )
            # Also verify fingerprint is hex and length 12
            if not re.fullmatch(r"[0-9a-f]{12}", fp):
                errors.append(f"8: fingerprint {fp!r} is not 12-char hex")

    # 9. PySide6 is actually present in Qt-enabled measurement — strict schema check
    deps = view.get("optional_dependencies")
    if not isinstance(deps, list):
        errors.append("9: optional_dependencies is missing or not a list")
    elif not _check_pyside_present(view):
        errors.append(
            "9: optional dependency ui (PySide6) not present=True in view — Qt not installed in measurement"
        )
    else:
        # Validate that each dep has required fields
        for idx, dep in enumerate(deps):
            if not isinstance(dep, dict):
                errors.append(f"9: optional_dependencies[{idx}] is not an object")
                continue
            for field in ("name", "present", "groups"):
                if field not in dep:
                    errors.append(f"9: optional_dependencies[{idx}] missing field {field!r}")

    # 10. groups["qt/ui"] exists, measured, non-zero stmts and branches, strict types
    groups = view.get("groups")
    if not isinstance(groups, dict):
        errors.append("10: view groups is missing or not a dict")
        return errors
    # Check for unexpected group names (allow only known groups)
    expected_groups = {
        "qt/ui",
        "core",
        "cli",
        "database",
        "document/knowledge/promotion",
        "extraction",
        "other packages",
        "review",
    }
    for gname in groups:
        if gname not in expected_groups and gname not in {"overall"}:
            # Not an error, but warn if unexpected; for strictness, allow any but check later
            pass
    qt = groups.get("qt/ui")
    if not isinstance(qt, dict):
        errors.append('10: groups["qt/ui"] missing or not an object')
    else:
        # Strict int checks, bool rejected
        for key in ("statements", "covered_statements", "branches", "covered_branches"):
            val = qt.get(key)
            if isinstance(val, bool):
                errors.append(f"10: qt/ui {key} is bool, expected int: {val!r}")
            elif not isinstance(val, int):
                errors.append(f"10: qt/ui {key} is {type(val).__name__}, expected int: {val!r}")
            elif val < 0:
                errors.append(f"10: qt/ui {key} is negative: {val!r}")
        stmts = qt.get("statements")
        branches = qt.get("branches")
        if isinstance(stmts, int) and not isinstance(stmts, bool) and stmts <= 0:
            errors.append(f"10: qt/ui statements is {stmts!r}, expected >0")
        if isinstance(branches, int) and not isinstance(branches, bool) and branches <= 0:
            errors.append(f"10: qt/ui branches is {branches!r}, expected >0")
        # line_percent and branch_percent must be valid
        lp = qt.get("line_percent")
        bp = qt.get("branch_percent")
        for label, val in (("line_percent", lp), ("branch_percent", bp)):
            if val is None:
                errors.append(f"10: qt/ui {label} is missing")
            elif isinstance(val, bool):
                errors.append(f"10: qt/ui {label} is bool, expected number: {val!r}")
            elif not isinstance(val, (int, float)):
                errors.append(f"10: qt/ui {label} missing or not numeric: {val!r}")
            elif not math.isfinite(float(val)):
                errors.append(f"10: qt/ui {label} is non-finite: {val!r}")
            elif float(val) < 0 or float(val) > 100:
                errors.append(f"10: qt/ui {label} out of range [0,100]: {val!r}")

    # 11. Percentages and counts are valid numeric, finite, in range, mutually consistent
    for gname, gdata in groups.items():
        if not isinstance(gdata, dict):
            errors.append(f"11: group {gname!r} not a dict")
            continue
        stmts = gdata.get("statements")
        covered = gdata.get("covered_statements")
        branches = gdata.get("branches")
        cbranches = gdata.get("covered_branches")
        lp = gdata.get("line_percent")
        bp = gdata.get("branch_percent")
        # strict int checks
        for key, val in [
            ("statements", stmts),
            ("covered_statements", covered),
            ("branches", branches),
            ("covered_branches", cbranches),
        ]:
            if isinstance(val, bool):
                errors.append(f"11: {gname} {key} is bool, expected int: {val!r}")
            elif not isinstance(val, int):
                errors.append(f"11: {gname} {key} is not int: {val!r}")
            elif val < 0:
                errors.append(f"11: {gname} {key} is negative: {val!r}")
        # covered must not exceed total
        if (
            isinstance(stmts, int)
            and isinstance(covered, int)
            and not isinstance(stmts, bool)
            and not isinstance(covered, bool)
        ) and covered > stmts:
            errors.append(f"11: {gname} covered_statements {covered} > statements {stmts}")
        if (
            isinstance(branches, int)
            and isinstance(cbranches, int)
            and not isinstance(branches, bool)
            and not isinstance(cbranches, bool)
        ) and cbranches > branches:
            errors.append(f"11: {gname} covered_branches {cbranches} > branches {branches}")
        # percent checks
        for label, val in [("line_percent", lp), ("branch_percent", bp)]:
            if val is None:
                errors.append(f"11: {gname} {label} is missing")
                continue
            if isinstance(val, bool):
                errors.append(f"11: {gname} {label} is bool, expected number: {val!r}")
                continue
            if not isinstance(val, (int, float)):
                errors.append(f"11: {gname} {label} not numeric: {val!r}")
                continue
            f = float(val)
            if not math.isfinite(f):
                errors.append(f"11: {gname} {label} is non-finite: {val!r}")
                continue
            if f < 0 or f > 100:
                errors.append(f"11: {gname} {label} out of range [0,100]: {val!r}")
        # mutual consistency
        if (
            isinstance(stmts, int)
            and isinstance(covered, int)
            and not isinstance(stmts, bool)
            and not isinstance(covered, bool)
            and stmts > 0
        ):
            expected_lp = round(100.0 * covered / stmts, 2)
            if (
                isinstance(lp, (int, float))
                and not isinstance(lp, bool)
                and abs(float(lp) - expected_lp) > 0.01
            ):
                errors.append(f"11: {gname} line_percent {lp} != {covered}/{stmts}={expected_lp}")
        if (
            isinstance(branches, int)
            and isinstance(cbranches, int)
            and not isinstance(branches, bool)
            and not isinstance(cbranches, bool)
            and branches > 0
        ):
            expected_bp = round(100.0 * cbranches / branches, 2)
            if (
                isinstance(bp, (int, float))
                and not isinstance(bp, bool)
                and abs(float(bp) - expected_bp) > 0.01
            ):
                errors.append(
                    f"11: {gname} branch_percent {bp} != {cbranches}/{branches}={expected_bp}"
                )

    # 12. XML-derived counts and serialized JSON view correspond — strict aggregate check
    try:
        xml_totals = _parse_xml_totals(xml_root)
        # Compare overall totals — strict: view overall must exactly match XML root totals
        overall = view.get("overall")
        if not isinstance(overall, dict):
            errors.append("12: view overall is missing or not an object")
        else:
            for json_key, xml_key in [
                ("statements", "lines_valid"),
                ("covered_statements", "lines_covered"),
                ("branches", "branches_valid"),
                ("covered_branches", "branches_covered"),
            ]:
                json_val = overall.get(json_key)
                xml_val = xml_totals.get(xml_key)
                if isinstance(json_val, bool) or not isinstance(json_val, int):
                    errors.append(f"12: view overall {json_key} is not int: {json_val!r}")
                elif isinstance(xml_val, int) and json_val != xml_val:
                    errors.append(f"12: overall {json_key}: view {json_val} != xml {xml_val}")
            # Also check overall percentages — normalize line-rate (0-1) to percent (0-100)
            for json_key, xml_key in [
                ("line_percent", "line_rate"),
                ("branch_percent", "branch_rate"),
            ]:
                json_val = overall.get(json_key)
                xml_val = xml_totals.get(xml_key)
                if json_val is not None and xml_val is not None:
                    xml_percent = (
                        round(float(xml_val) * 100, 2) if xml_val <= 1 else round(float(xml_val), 2)
                    )
                    if (
                        isinstance(json_val, (int, float))
                        and abs(float(json_val) - xml_percent) > 0.5
                    ):
                        errors.append(
                            f"12: overall {json_key}: view {json_val} != xml {xml_percent} (from {xml_key} {xml_val})"
                        )
        # Also ensure at least one class present and overall statements >0
        classes = list(xml_root.iter("class"))
        if not classes:
            errors.append("12: coverage XML has no <class> elements")
        if (
            isinstance(overall, dict)
            and isinstance(overall.get("statements"), int)
            and overall.get("statements", 0) <= 0
        ):
            errors.append("12: view overall statements is 0")
        # Check group totals vs XML file selection: ensure qt/ui files are represented in XML
        # Use shared normalization (sources + Package marker) to count UI classes correctly
        # for both full paths (src/drilling_intelligence/ui/...) and package-rooted (ui/...)
        sources_for_ui = xml_totals.get("_sources", []) if isinstance(xml_totals, dict) else []
        rooted_for_ui = xml_totals.get("_rooted", False) if isinstance(xml_totals, dict) else False
        # Re-derive UI classes via the same logic as _parse_xml_totals
        ui_classes = []
        for c in classes:
            fname = c.get("filename") or ""
            if not fname:
                continue
            # Use shared helper: if it maps to product and its group is qt/ui
            # We need to check both marker and rooted cases
            if PACKAGE_MARKER in fname.replace("\\", "/"):
                rel = relative_to_package(fname)
            else:
                rel = xml_filename_to_relative(fname, sources_for_ui, rooted_for_ui)
                if rel is None:
                    # Try fallback: if relative_to_package would succeed but xml helper rejected
                    # due to not-rooted, we already handled marker case; so skip
                    continue
            if rel is not None and group_of(rel) == "qt/ui":
                ui_classes.append(c)
        if isinstance(qt, dict) and isinstance(qt.get("files"), int):
            json_files = qt.get("files")
            if isinstance(json_files, int) and len(ui_classes) < json_files:
                errors.append(
                    f"12: qt/ui files: view {json_files} > xml ui classes {len(ui_classes)}"
                )
            if isinstance(json_files, int) and len(ui_classes) == 0 and json_files > 0:
                # Provide more context when zero UI classes found
                # Check if XML has any product classes at all
                product_classes = xml_totals.get("_product_classes", [])
                if isinstance(product_classes, list) and len(product_classes) == 0:
                    errors.append(
                        f"12: no product classes found in XML (sources={sources_for_ui!r}, rooted={rooted_for_ui}) — cannot verify qt/ui"
                    )
        # Check for duplicate normalized class filenames in XML
        # Duplicate raw filenames are already caught, but normalized duplicates (e.g.
        # src/drilling_intelligence/ui/foo.py vs ui/foo.py with package-root source)
        # should also be flagged as they represent the same product file.
        seen = set()
        dups = set()
        seen_norm = set()
        dups_norm = set()
        for c in classes:
            fname = c.get("filename") or ""
            if fname in seen:
                dups.add(fname)
            seen.add(fname)
            # Normalized duplicate check
            if fname:
                rel_norm = relative_to_package(fname)
                # For bare ui/ paths, relative_to_package returns "ui/..." even without
                # rooted check — which is correct for duplicate detection.
                # If rel_norm is None, we still consider raw fname for duplicate
                norm = rel_norm if rel_norm is not None else fname.replace("\\", "/")
                if norm in seen_norm:
                    dups_norm.add(norm)
                seen_norm.add(norm)
        if dups:
            errors.append(f"12: duplicate class filenames in XML: {sorted(dups)!r}")
        if dups_norm and not dups:
            # Only report normalized dups if not already reported as raw dups
            errors.append(f"12: duplicate normalized product paths in XML: {sorted(dups_norm)!r}")
        # Validate that every class filename can be mapped (empty/missing)
        missing_filenames = [c for c in classes if not (c.get("filename") or "").strip()]
        if missing_filenames:
            errors.append(
                f"12: XML has {len(missing_filenames)} <class> with missing/empty filename"
            )
        # If the XML claims to be product-rooted but has ambiguous bare paths without root, fail closed
        # (already handled by product_classes==0 case above)
    except Exception as exc:  # noqa: BLE001 - any parsing error is a validation error
        errors.append(f"12: failed to parse XML totals: {exc}")

    # 13. UI test-selection JUnit/skip audit has no unapproved skipped test
    if junit_path is not None:
        if not junit_path.is_file():
            errors.append(f"13: JUnit path missing at {junit_path}")
        else:
            try:
                root_junit = ET.parse(junit_path).getroot()  # noqa: S314
                if root_junit.tag not in {"testsuite", "testsuites"}:
                    errors.append(
                        f"13: JUnit root is {root_junit.tag!r}, expected testsuite/testsuites"
                    )
                else:
                    from tools.pytest_totals import (
                        unexpected_skips_qt_enabled,  # local import to avoid cycle
                    )

                    skips = unexpected_skips_qt_enabled(junit_path)
                    if skips:
                        errors.append(
                            "13: unapproved skips in Qt-enabled JUnit:\n" + "\n".join(skips)
                        )
                    # Also validate that JUnit has no failures/errors
                    for suite in (
                        root_junit.findall("testsuite")
                        if root_junit.tag == "testsuites"
                        else [root_junit]
                    ):
                        failures = suite.get("failures")
                        errors_attr = suite.get("errors")
                        try:
                            if failures is not None and int(failures) != 0:
                                errors.append(
                                    f"13: JUnit suite {suite.get('name')!r} has failures={failures}"
                                )
                            if errors_attr is not None and int(errors_attr) != 0:
                                errors.append(
                                    f"13: JUnit suite {suite.get('name')!r} has errors={errors_attr}"
                                )
                        except ValueError:
                            errors.append(
                                f"13: JUnit suite failures/errors is not int: {failures}/{errors_attr}"
                            )
                    # Check for testcase with <failure> or <error>
                    for case in root_junit.iter("testcase"):
                        if case.find("failure") is not None:
                            errors.append(
                                f"13: JUnit testcase {case.get('classname')}::{case.get('name')} has <failure>"
                            )
                        if case.find("error") is not None:
                            errors.append(
                                f"13: JUnit testcase {case.get('classname')}::{case.get('name')} has <error>"
                            )
            except ET.ParseError as exc:
                errors.append(f"13: JUnit is not valid XML: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"13: failed to check JUnit: {exc}")

    # 14. Report's commit, selector, and scope agree with gate's inputs — already covered in 5,6,7
    # 15. Both thresholds fail closed — validated here, not just printed
    # 15a is the hard gate (70/40); 15b is the resilience headroom (71/42) — advisory.
    if isinstance(qt, dict):
        lp = qt.get("line_percent")
        bp = qt.get("branch_percent")
        if isinstance(lp, (int, float)) and not isinstance(lp, bool):
            if float(lp) < 70.0:
                errors.append(f"15a: qt/ui line {lp}% below 70% gate")
            # 71% resilience is advisory: do not add to errors
        else:
            errors.append("15: qt/ui line_percent missing or not numeric, cannot check threshold")
        if isinstance(bp, (int, float)) and not isinstance(bp, bool):
            if float(bp) < 40.0:
                errors.append(f"15a: qt/ui branch {bp}% below 40% gate")
            # 42% resilience is advisory
        else:
            errors.append("15: qt/ui branch_percent missing or not numeric, cannot check threshold")

    # 16. Missing or failed measurement cannot reuse old artifact — already covered in 5 via SHA equality
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", type=Path, required=True, help="coverage-ui.xml")
    parser.add_argument("--view", type=Path, required=True, help="coverage-ui-view.json")
    parser.add_argument(
        "--junit", type=Path, default=None, help="Qt-enabled JUnit XML for skip audit"
    )
    parser.add_argument(
        "--expected-sha", type=str, default=None, help="expected commit SHA (default: git HEAD)"
    )
    parser.add_argument("--scope", type=str, default="ui-test-selection")
    parser.add_argument("--test-selection", type=str, default=EXPECTED_SELECTION)
    args = parser.parse_args(argv)

    if args.expected_sha is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                print("::error::could not determine HEAD: git rev-parse failed", file=sys.stderr)
                return 1
            args.expected_sha = result.stdout.strip()
            if not _is_full_sha(args.expected_sha):
                print(
                    f"::error::git HEAD is not a full 40-char hex SHA: {args.expected_sha!r}",
                    file=sys.stderr,
                )
                return 1
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"::error::could not determine HEAD: {exc}", file=sys.stderr)
            return 1
    else:
        if not _is_full_sha(args.expected_sha):
            print(
                f"::error::expected_sha is not a full 40-char hex SHA: {args.expected_sha!r}",
                file=sys.stderr,
            )
            return 1

    errs = validate(
        xml_path=args.xml,
        view_path=args.view,
        junit_path=args.junit,
        expected_sha=args.expected_sha,
        scope=args.scope,
        test_selection=args.test_selection,
    )
    if errs:
        for e in errs:
            print(f"::error::{e}", file=sys.stderr)
        print("qt_coverage_gate: FAIL", file=sys.stderr)
        for e in errs:
            print(e, file=sys.stderr)
        return 1
    # Emit notices for pass, mirroring previous gate — only on success
    # Advisory resilience check: 71/42 is not a hard gate, but emit warning if below
    try:
        view = json.loads(args.view.read_text(encoding="utf-8"))
        qt = view.get("groups", {}).get("qt/ui", {})
        lp = qt.get("line_percent")
        bp = qt.get("branch_percent")
        # Advisory: warn if below 71/42 but do not fail
        if isinstance(lp, (int, float)) and not isinstance(lp, bool) and float(lp) < 71.0:
            print(
                f"::warning::coverage-resilience: qt/ui line {lp}% below 71% resilience target",
                file=sys.stderr,
            )
        if isinstance(bp, (int, float)) and not isinstance(bp, bool) and float(bp) < 42.0:
            print(
                f"::warning::coverage-resilience: qt/ui branch {bp}% below 42% resilience target",
                file=sys.stderr,
            )
        print(f"::notice::coverage-gate: PASS qt/ui {lp}% lines, {bp}% branches meets 70/40")
        # Only emit resilience PASS if actually meets 71/42
        if (
            isinstance(lp, (int, float))
            and isinstance(bp, (int, float))
            and float(lp) >= 71.0
            and float(bp) >= 42.0
        ):
            print(
                f"::notice::coverage-resilience: PASS qt/ui {lp}% lines, {bp}% branches meets 71/42 headroom (V8.23)"
            )
        else:
            print(
                f"::notice::coverage-resilience: ADVISORY qt/ui {lp}% lines, {bp}% branches below 71/42 headroom",
                file=sys.stderr,
            )
        print(f"::notice::coverage-ui-package: {lp}% lines, {bp}% branches (qt/ui package, UI run)")
    except Exception:  # noqa: BLE001
        pass
    print("qt_coverage_gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
