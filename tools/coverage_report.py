#!/usr/bin/env python3
"""The package-level view of the coverage, and the comparison with the baseline.

A total percentage on its own answers the wrong question.  "74%" does not say
which part of the platform is unmeasured and which part is measured and thin,
and it cannot distinguish a package that is thin from a package that was never
exercised because the run did not have the optional dependency it needs.

So this tool reads the Cobertura XML that ``pytest --cov --cov-report=xml``
writes and answers three questions:

1. What is the coverage *per package group* - the seven groups the platform is
   built from (core, database, extraction, document/knowledge/promotion, CLI,
   review, Qt/UI), with the remaining packages itemised so that nothing is
   hidden inside an "other" bucket?
2. Was an optional dependency present in the environment that produced the
   measurement?  A group whose optional runtime was absent is reported as
   *measured without it*, not as uncovered: the difference matters, and the
   report says which one it is.
3. How does this measurement compare with the recorded baseline?

No threshold is enforced and no file is excluded from the measurement.  The
coverage number is a record, not a gate; a gate needs a baseline that was
agreed on, and this repository does not have one yet (see
docs/mission-reports/END_TO_END_FINAL_REPORT.md).  The tool therefore exits
zero whatever the comparison says, and says the difference out loud.

Usage:
    python tools/coverage_report.py --xml coverage.xml --output docs/coverage_report.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PREFIX = "src/drilling_intelligence/"

#: The package groups the platform is read by.  The first seven are the groups
#: the V8.17 mission asks for; everything else is listed under "other packages"
#: and itemised, so that a package cannot disappear into an average.
GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "database": ("database",),
    "extraction": ("extraction",),
    "document/knowledge/promotion": ("documents", "knowledge", "operations"),
    "cli": ("cli",),
    "review": ("review",),
    "qt/ui": ("ui",),
}

#: A package that is not in a named group is still reported, on its own line.
OTHER_GROUP = "other packages"

#: The optional dependencies, and the groups whose numbers depend on them.
#: ``extra`` is a pip extra of this project; ``command`` is an external
#: runtime; ``module`` is a module of another project.
OPTIONAL_DEPENDENCIES: tuple[dict[str, object], ...] = (
    {
        "name": "ui",
        "kind": "pip extra",
        "module": "PySide6",
        "groups": ("qt/ui",),
        "note": (
            "The desktop runtime is optional; the headless installation never "
            "imports Qt.  The Qt layer is measured by the workbench job."
        ),
    },
    {
        "name": "mineru",
        "kind": "external runtime",
        "command": "mineru",
        "groups": ("extraction", "other packages"),
        "note": (
            "MinerU is an optional external command.  Without it the router "
            "records that OCR is needed instead of extracting text."
        ),
    },
    {
        "name": "ollama",
        "kind": "local service",
        "command": "ollama",
        "groups": ("other packages",),
        "note": (
            "The network-marked tests skip unless a local Ollama instance "
            "answers; the adapters are still exercised through their fakes."
        ),
    },
)

BASELINE_PATH = REPO_ROOT / "docs" / "coverage_baseline.json"


@dataclass
class FileCoverage:
    """One measured file."""

    path: str
    statements: int = 0
    covered_statements: int = 0
    branches: int = 0
    covered_branches: int = 0

    @property
    def line_percent(self) -> float:
        if not self.statements:
            return 100.0
        return 100.0 * self.covered_statements / self.statements

    @property
    def branch_percent(self) -> float:
        if not self.branches:
            return 100.0
        return 100.0 * self.covered_branches / self.branches


@dataclass
class GroupCoverage:
    """The measured files of one group, added up."""

    name: str
    files: list[FileCoverage] = field(default_factory=list)

    @property
    def statements(self) -> int:
        return sum(item.statements for item in self.files)

    @property
    def covered_statements(self) -> int:
        return sum(item.covered_statements for item in self.files)

    @property
    def branches(self) -> int:
        return sum(item.branches for item in self.files)

    @property
    def covered_branches(self) -> int:
        return sum(item.covered_branches for item in self.files)

    @property
    def line_percent(self) -> float:
        if not self.statements:
            return 100.0
        return 100.0 * self.covered_statements / self.statements

    @property
    def branch_percent(self) -> float:
        if not self.branches:
            return 100.0
        return 100.0 * self.covered_branches / self.branches

    def as_dict(self) -> dict[str, object]:
        return {
            "statements": self.statements,
            "covered_statements": self.covered_statements,
            "branches": self.branches,
            "covered_branches": self.covered_branches,
            "line_percent": round(self.line_percent, 2),
            "branch_percent": round(self.branch_percent, 2),
            "files": len(self.files),
        }


@dataclass
class Coverage:
    """A whole measurement."""

    groups: dict[str, GroupCoverage]
    files: list[FileCoverage]
    source: str

    @property
    def overall(self) -> GroupCoverage:
        group = GroupCoverage(name="overall")
        group.files = list(self.files)
        return group


PACKAGE_MARKER = "drilling_intelligence/"


def relative_to_package(path: str) -> str | None:
    """The path of a measured file inside the product package.

    Cobertura is written in two shapes depending on how the report was asked
    for.  ``pytest --cov`` writes the full path it measured, so a file arrives
    as ``src/drilling_intelligence/core/units.py``.  A report rooted at the
    package writes ``core/units.py`` and names the root once, in
    ``<sources><source>``.  Both are the same measurement of the same file, and
    a reader of this report must not get 100% of nothing because the shape
    changed.
    """
    normalised = path.replace("\\", "/")
    index = normalised.rfind(PACKAGE_MARKER)
    if index >= 0:
        return normalised[index + len(PACKAGE_MARKER) :]
    if normalised.startswith(("src/", "tests/", "tools/", "/")) or ".." in normalised:
        return None
    if normalised.startswith(SOURCE_PREFIX):
        return normalised[len(SOURCE_PREFIX) :]
    return normalised


def package_of(path: str) -> str:
    """``src/drilling_intelligence/core/units.py`` -> ``core``."""
    relative = relative_to_package(path)
    if relative is None:
        return OTHER_GROUP
    parts = relative.split("/")
    if len(parts) < 2:
        # A module at the root of the package (``__init__.py``) belongs to the
        # package itself, and is reported with the packages that are not in a
        # named group rather than being dropped.
        return OTHER_GROUP
    return parts[0]


def group_of(path: str) -> str:
    """The group a measured file belongs to."""
    package = package_of(path)
    for group, packages in GROUPS.items():
        if package in packages:
            return group
    return OTHER_GROUP


def _condition_counts(text: str | None) -> tuple[int, int]:
    """``"50% (1/2)"`` -> ``(1, 2)``."""
    if not text:
        return 0, 0
    if "(" not in text:
        return 0, 0
    inside = text[text.index("(") + 1 : text.rindex(")")]
    covered, _, total = inside.partition("/")
    try:
        return int(covered), int(total)
    except ValueError:
        return 0, 0


def read_cobertura(path: Path) -> Coverage:
    """Read a Cobertura XML file into per-file and per-group numbers."""
    tree = ElementTree.parse(path)  # noqa: S314 - the file is written by our own coverage run
    root = tree.getroot()
    files: list[FileCoverage] = []
    sources = [(element.text or "").strip() for element in root.iter("source") if element.text]
    rooted_in_package = any("drilling_intelligence" in source for source in sources)
    for element in root.iter("class"):
        filename = element.get("filename") or ""
        if not filename:
            continue
        relative = relative_to_package(filename)
        if relative is None:
            continue
        if PACKAGE_MARKER not in filename.replace("\\", "/") and not rooted_in_package:
            # The report is not rooted at the product package and the file is
            # not inside it: a dependency that was measured by accident is not
            # part of the product's view.
            continue
        item = FileCoverage(path=relative)
        statements: set[int] = set()
        covered: set[int] = set()
        for line in element.iter("line"):
            try:
                number = int(line.get("number") or "0")
            except ValueError:
                continue
            statements.add(number)
            try:
                hits = int(line.get("hits") or "0")
            except ValueError:
                hits = 0
            if hits > 0:
                covered.add(number)
            if (line.get("branch") or "").lower() == "true":
                branch_covered, branch_total = _condition_counts(line.get("condition-coverage"))
                item.branches += branch_total
                item.covered_branches += branch_covered
        item.statements = len(statements)
        item.covered_statements = len(covered)
        files.append(item)
    groups: dict[str, GroupCoverage] = {}
    for item in files:
        group = groups.setdefault(group_of(item.path), GroupCoverage(name=group_of(item.path)))
        group.files.append(item)
    return Coverage(groups=groups, files=files, source=str(path))


def optional_dependencies() -> list[dict[str, object]]:
    """What is actually installed in the environment that produced the report."""
    facts: list[dict[str, object]] = []
    for spec in OPTIONAL_DEPENDENCIES:
        module = spec.get("module")
        command = spec.get("command")
        if module:
            present = _module_present(str(module))
            probe = f"import {module}"
        else:
            present = shutil.which(str(command)) is not None
            probe = f"{command} on PATH"
        facts.append(
            {
                "name": spec["name"],
                "kind": spec["kind"],
                "probe": probe,
                "present": present,
                "groups": list(spec["groups"]),
                "note": spec["note"],
            }
        )
    return facts


def _module_present(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - a broken install
        return False


def _runtime() -> str:
    return f"CPython {platform.python_version()} on {platform.system()} {platform.release()}"


def _commit() -> str:
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is not always present
        return "unknown"
    try:
        result = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git is not always present
        return "unknown"
    sha = result.stdout.strip()
    return sha or "unknown"


def _infer_scope(xml_path: str) -> str:
    name = pathlib.Path(xml_path).name
    if name == "coverage-ui.xml":
        return "ui-test-selection"
    if name == "coverage.xml":
        return "full-suite"
    return "unknown"

SCHEMA_VERSION = 2

def build_view(coverage: Coverage, *, scope: str | None = None, test_selection: str | None = None, xml_path: str | None = None) -> dict[str, object]:
    """The whole measurement as a plain dictionary."""
    groups = {name: group.as_dict() for name, group in sorted(coverage.groups.items())}
    overall = coverage.overall.as_dict()
    source = xml_path or coverage.source
    inferred = _infer_scope(source)
    measurement_scope = scope or inferred
    if test_selection is None:
        if measurement_scope == "full-suite":
            test_selection = "pytest (full suite)"
        elif measurement_scope == "ui-test-selection":
            test_selection = "tests/ui (Qt offscreen) + relevant Qt lifecycle tests"
        else:
            test_selection = "unknown"
    view: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "xml_path": source,
        "measurement_scope": measurement_scope,
        "test_selection": test_selection,
        "commit": _commit(),
        "runtime": _runtime(),
        "optional_dependencies": optional_dependencies(),
        "overall": overall,
        "groups": groups,
    }
    return view



def _baseline_comparable(view: dict[str, object], baseline: dict[str, object] | None) -> tuple[bool, str]:
    if not baseline:
        return False, "no baseline"
    view_scope = str(view.get("measurement_scope") or "")
    base_scope = str(baseline.get("measurement_scope") or "")
    # Old baselines have no scope; treat them as full-suite for backward compat
    if not base_scope:
        base_scope = "full-suite"
    if view_scope != base_scope:
        return False, f"baseline scope '{base_scope}' != measurement scope '{view_scope}'"
    view_deps = {str(d.get("name")): bool(d.get("present")) for d in view.get("optional_dependencies", [])}  # type: ignore[arg-type]
    base_deps = {str(d.get("name")): bool(d.get("present")) for d in baseline.get("optional_dependencies", [])}  # type: ignore[arg-type]
    # Only ui-test-selection cares about Qt presence difference
    if view_scope == "ui-test-selection" and view_deps.get("ui") != base_deps.get("ui"):
        # If baseline has no ui info (old), don't treat as mismatch
        if "ui" in base_deps or "ui" in view_deps:
            # If baseline explicitly headless (present=False) and view Qt-enabled (present=True), not comparable
            # But old baseline with no deps is considered comparable for full-suite, not for ui
            if base_deps.get("ui") is False and view_deps.get("ui") is True:
                return False, "Qt/UI optional dependency presence differs (headless vs Qt-enabled)"
    return True, ""

def compare(view: dict[str, object], baseline: dict[str, object] | None) -> list[str]:
    """The difference between this measurement and the recorded baseline."""
    if not baseline:
        return ["no baseline is recorded; this measurement is the first one"]
    comparable, reason = _baseline_comparable(view, baseline)
    if not comparable:
        lines = [f"baseline not comparable: {reason}", "no delta is reported; scopes or optional dependencies differ", "the comparison is a record; no threshold is enforced and no file is excluded"]
        base_scope = str(baseline.get("measurement_scope") or baseline.get("label") or "unknown")
        view_scope = str(view.get("measurement_scope") or "unknown")
        lines.insert(0, f"baseline scope: {base_scope}; measurement scope: {view_scope}")
        return lines
    lines: list[str] = []
    base_groups = baseline.get("groups") or {}
    now_groups = view.get("groups") or {}
    for name in sorted(set(base_groups) | set(now_groups)):
        base = base_groups.get(name) or {}
        now = now_groups.get(name) or {}
        base_line = float(base.get("line_percent", 0.0))
        now_line = float(now.get("line_percent", 0.0))
        delta = now_line - base_line
        lines.append(f"{name}: {base_line:.2f}% -> {now_line:.2f}% ({delta:+.2f} points)")
    base_overall = (baseline.get("overall") or {}).get("line_percent", 0.0)
    now_overall = (view.get("overall") or {}).get("line_percent", 0.0)
    lines.append(
        f"overall: {float(base_overall):.2f}% -> {float(now_overall):.2f}% "
        f"({float(now_overall) - float(base_overall):+.2f} points)"
    )
    lines.append("the comparison is a record; no threshold is enforced and no file is excluded")
    return lines



def read_baseline(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_percent(value: float, total: int) -> str:
    """Format a percentage with qualification when it is 100% of nothing."""
    if total == 0:
        # 100% of 0 statements is not coverage – it is “not measured”.
        return "— (not measured)"
    return f"{value}%"


def render_markdown(view: dict[str, object], baseline: dict[str, object] | None) -> str:
    """The report as a markdown document."""
    overall = view["overall"]
    scope = str(view.get("measurement_scope", "unknown"))
    test_sel = str(view.get("test_selection", "unknown"))
    xml_path = str(view.get("xml_path", view.get("source", "unknown")))
    schema = str(view.get("schema_version", ""))
    comparable, comp_reason = _baseline_comparable(view, baseline)
    baseline_label = "none"
    if baseline:
        baseline_label = str(baseline.get("measurement_scope") or baseline.get("label") or baseline.get("commit", "unknown"))
    lines: list[str] = [
        "# The coverage of the product, by package group",
        "",
        f"Generated by `python tools/coverage_report.py --xml {xml_path} --scope {scope}`.",
        f"Measurement scope: **{scope}** — test selection: `{test_sel}` — schema v{schema} — baseline comparable: {comparable} ({comp_reason or 'ok'}) — baseline: {baseline_label}.",
        "",
        "A single percentage cannot be read: it hides the difference between a",
        "package that is thin and a package that was not measured at all.  This",
        "document is therefore a per-group view, with the optional dependencies",
        "that decide what each group could have measured.",
        "",
        "> **Terminology (V8.19).** `coverage-full-suite` is the whole-source coverage of the full test suite (coverage.xml);",
        "> `coverage-ui-total` is the whole-source coverage *during* the UI-test selection (coverage-ui.xml);",
        "> `qt/ui` package coverage is the package-specific line/branch coverage of the ui package alone.",
        "> A group with zero measured statements is shown as \"\u2014 (not measured)\" not \"100%\".",
        "> Deltas are only shown when measurement scope, test selection, and optional-dependency presence match the baseline.",
        "",
        f"- measurement: `{view['source']}`",
        f"- xml_path: `{xml_path}`",
        f"- measurement_scope: `{scope}`",
        f"- test_selection: `{test_sel}`",
        f"- commit: `{view['commit']}`",
        f"- runtime: {view['runtime']}",
        "",
        "## The whole product",
        "",
        "| measure | covered | total | percent |",
        "| --- | --- | --- | --- |",
        f"| statements (lines) | {overall['covered_statements']} | {overall['statements']} "
        f"| {overall['line_percent']}% |",
        f"| branches | {overall['covered_branches']} | {overall['branches']} "
        f"| {overall['branch_percent']}% |",

        "",
        "## By package group",
        "",
        "| group | files | statements | covered | line % | branches | covered | branch % |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    groups = view["groups"]
    for name in sorted(groups):
        group = groups[name]
        line_cell = _format_percent(float(group["line_percent"]), int(group["statements"]))
        branch_cell = _format_percent(float(group["branch_percent"]), int(group["branches"]))
        lines.append(
            f"| {name} | {group['files']} | {group['statements']} "
            f"| {group['covered_statements']} | {line_cell} "
            f"| {group['branches']} | {group['covered_branches']} "
            f"| {branch_cell} |"
        )
    lines.extend(
        [
            "",
            "## The optional dependencies of this measurement",
            "",
            "A group that depends on an optional runtime is measured without it",
            "when the runtime is absent.  That is not the same as being uncovered,",
            "and the difference is stated here rather than left in the average.",
            "",
            "| optional dependency | kind | probe | present here | affects |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for fact in view["optional_dependencies"]:
        present = "yes" if fact["present"] else "no"
        affects = ", ".join(str(item) for item in fact["groups"])
        lines.append(
            f"| {fact['name']} | {fact['kind']} | `{fact['probe']}` | {present} | {affects} |"
        )
    lines.append("")
    for fact in view["optional_dependencies"]:
        present = "present" if fact["present"] else "absent"
        lines.append(f"- **{fact['name']}** is {present} here. {fact['note']}")
    lines.extend(
        [
            "",
            "## The comparison with the baseline",
            "",
        ]
    )
    for line in compare(view, baseline):
        lines.append(f"- {line}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", type=Path, default=REPO_ROOT / "coverage.xml")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--output", type=Path, default=None, help="write the markdown report here")
    parser.add_argument(
        "--json", type=Path, default=None, help="write the measurement as JSON here"
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record this measurement as the baseline (a deliberate act)",
    )
    parser.add_argument("--label", default="", help="what this measurement is")
    parser.add_argument("--scope", default=None, help="measurement scope: full-suite or ui-test-selection")
    parser.add_argument("--test-selection", default=None, help="pytest selector that produced the XML")
    args = parser.parse_args(argv)

    if not args.xml.exists():
        print(f"no coverage XML at {args.xml}; nothing was measured")
        return 0

    coverage = read_cobertura(args.xml)
    view = build_view(coverage, scope=args.scope, test_selection=args.test_selection, xml_path=str(args.xml))
    if args.label:
        view["label"] = args.label
    baseline = read_baseline(args.baseline)

    overall = view["overall"]
    print(
        f"coverage total: {overall['line_percent']}% of lines, {overall['branch_percent']}% of branches"
    )
    for name in sorted(view["groups"]):
        group = view["groups"][name]
        line_str = _format_percent(float(group["line_percent"]), int(group["statements"]))
        branch_str = _format_percent(float(group["branch_percent"]), int(group["branches"]))
        print(
            f"  {name}: {line_str} lines, {branch_str} branches "
            f"({group['files']} files, {group['statements']} statements)"
        )
    for line in compare(view, baseline):
        print(f"baseline: {line}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_markdown(view, baseline), encoding="utf-8")
        print(f"wrote {args.output}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(view, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    if args.write_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(view)
        payload["label"] = args.label or view.get("label", "")
        args.baseline.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote the baseline {args.baseline}")

    # The number is a record, not a gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
