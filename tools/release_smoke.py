#!/usr/bin/env python3
"""Installation-level end-to-end smoke test: build, install clean, then really use it.

Importing a package and printing its version proves almost nothing.  This script builds the wheel
and the sdist, installs each into a **fresh virtualenv outside the source checkout**, and then drives
the installed CLI from a working directory where the repository cannot be imported by accident.  It
asserts the things that actually break on a release and never break in a source checkout:

* the package imports from ``site-packages``, with no repository path on ``sys.path``;
* the CLI entry point exists and answers ``--help`` / ``--version``;
* a new workspace bootstraps its schema with **no** ``migrations/`` directory present - the
  documented ``stamped-from-metadata`` path, which is the only one an installed wheel has;
* a well can be created and read back, so the schema is really usable and not merely present;
* ``doctor`` runs and its JSON is parseable;
* ``fields decision`` prints an empty-but-valid decision pack whose JSON parses;
* ``fields compare`` prints an empty-but-valid comparison pack for two fresh wells;
* ``analyze --list-questions`` prints the sixteen-question catalog and ``analyze --question``
  answers with parseable ``analyst-answer/1`` JSON - no golden corpus required;
* ``fields report`` prints an ``engineering-report/1`` document and writes standalone HTML
  with inline SVG - no golden corpus, no source-tree import;
* ``fields report --verify-citations`` prints ``engineering-report-audited/1`` for the same
  report identity and does not call a quiet audit fully verified;
* a search and a records read succeed against the empty-but-valid workspace.

Every check prints what it observed.  The exit code is non-zero on the first failure, so this is
usable both as a local gate and as a CI job.

Usage::

    python tools/release_smoke.py            # wheel and sdist
    python tools/release_smoke.py --wheel    # wheel only (faster)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: Run the installed CLI from here.  It must not be the repository, or a stray ``src/`` on
#: ``sys.path`` would let the checkout answer for the installed package.
#:
#: The name carries a space and a non-ASCII character on purpose.  A release is
#: Windows-first, and a path like this is where Windows breaks first: the quoting
#: of the console script, the code page of the console, the short-name form of
#: the directory.  The smoke test uses such a path on every platform, so that a
#: Linux run cannot pass while a Windows run fails.
NEUTRAL_CWD = Path(tempfile.gettempdir()) / "drill intel neutral"

#: The same, for the working tree of the smoke test itself.
HOSTILE_DIR_NAME = "drill intel smoke \u2713"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f'  [{detail}]' if detail else ''}")
    if not condition:
        _failures.append(label)
    return condition


def run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def build_dists(outdir: Path, sdist: bool) -> tuple[Path | None, Path | None]:
    """Build the wheel (and optionally the sdist) using the project's own build backend."""
    kinds = ["--wheel"] + (["--sdist"] if sdist else [])
    print(f"building {' + '.join(k.strip('-') for k in kinds)} into {outdir}")
    result = run(
        [sys.executable, "-m", "build", *kinds, "--outdir", str(outdir), str(REPO)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": ""},
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        raise SystemExit("the project's own build backend failed - nothing else is worth testing")
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    return (wheels[-1] if wheels else None), (sdists[-1] if sdists else None)


def _venv_module():
    import importlib.util

    path = Path(__file__).resolve().with_name("venv_executables.py")
    spec = importlib.util.spec_from_file_location("venv_executables", path)
    if spec is None or spec.loader is None:
        raise SystemExit("venv_executables.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _venv_program(root: Path, name: str) -> Path:
    """The real installed program, including the Windows ``.exe`` suffix."""
    module = _venv_module()
    found = module.venv_executable(root, name)
    if not found.is_file():
        raise SystemExit(f"virtualenv is missing {found}")
    return found


def inspect_artifact(kind: str, artefact: Path) -> None:
    """Read the artefact before installing it.

    What the wheel *carries* is part of the release contract, and it is easier
    to see here than after an install has unpacked it.  Three things are
    checked: both console scripts are declared, the type marker is shipped,
    and no migration script is shipped - the package bootstraps its schema from
    metadata, and shipping half a migration history would be worse than
    shipping none.
    """
    print(f"\n=== {kind} contents: {artefact.name} ===")
    if artefact.suffix == ".whl":
        import zipfile

        with zipfile.ZipFile(artefact) as archive:
            names = archive.namelist()
            entry_points = [name for name in names if name.endswith("entry_points.txt")]
            declared = ""
            if entry_points:
                declared = archive.read(entry_points[0]).decode("utf-8", "replace")
            shipped = [name for name in names if name.startswith("drilling_intelligence/")]
        check(f"{kind}: wheel declares drillintel", "drillintel = " in declared, declared[:60])
        check(f"{kind}: wheel declares drillintel-ui", "drillintel-ui = " in declared)
        check(
            f"{kind}: wheel ships the type marker",
            "drilling_intelligence/py.typed" in names,
        )
        check(
            f"{kind}: wheel ships no migration script",
            not any("/migrations/" in name or name.startswith("migrations/") for name in names),
        )
        check(
            f"{kind}: wheel ships the UI package lazily (no Qt import at install)",
            "drilling_intelligence/ui/app.py" in shipped,
        )
        check(
            f"{kind}: wheel ships no database or workspace",
            not any(name.endswith((".db", ".db-wal", ".db-shm")) for name in names),
        )
    else:
        import tarfile

        with tarfile.open(artefact) as archive:
            names = archive.getnames()
        check(
            f"{kind}: sdist ships no database",
            not any(name.endswith(".db") for name in names),
        )
        check(
            f"{kind}: sdist ships the source of the CLI and the UI launcher",
            any(name.endswith("src/drilling_intelligence/cli/app.py") for name in names)
            or any(name.endswith("drilling_intelligence/cli/app.py") for name in names),
        )


def fresh_venv(root: Path) -> Path:
    venv.create(root, with_pip=True, clear=True)
    return root


def install(venv_root: Path, target: Path) -> None:
    pip = _venv_program(venv_root, "pip")
    result = run(
        [str(pip), "install", "--quiet", "--disable-pip-version-check", str(target)],
        cwd=NEUTRAL_CWD,
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        raise SystemExit(f"installing {target.name} failed")


def smoke(kind: str, artefact: Path, workdir: Path) -> None:
    """Install one artefact into a clean venv and exercise the installed product."""
    print(f"\n=== {kind}: {artefact.name} ===")
    inspect_artifact(kind, artefact)
    NEUTRAL_CWD.mkdir(parents=True, exist_ok=True)
    venv_root = fresh_venv(workdir / "venv")
    install(venv_root, artefact)
    python = _venv_program(venv_root, "python")
    cli = _venv_program(venv_root, "drillintel")
    ui_cli = _venv_program(venv_root, "drillintel-ui")

    # 0. the virtualenv's own layout, both ways round.
    #    ``Scripts`` is where Windows puts the console scripts and ``bin`` is where
    #    POSIX puts them; the smoke test asks the helper module rather than
    #    repeating the rule, and checks that the *other* platform's answer is
    #    different, so a wrong branch cannot pass on the platform that has it.
    executables = _venv_module()
    here = executables.venv_bindir(venv_root)
    other = executables.venv_bindir(venv_root, platform_name="nt" if os.name != "nt" else "posix")
    check(f"{kind}: the virtualenv layout is discovered, not assumed", here.is_dir(), str(here))
    check(f"{kind}: the other platform's layout is different", other != here, str(other))
    check(
        f"{kind}: the console scripts carry the platform's suffix",
        cli.name == ("drillintel.exe" if os.name == "nt" else "drillintel"),
        cli.name,
    )

    # 1. import from site-packages, with the repository provably absent from sys.path
    probe = run(
        [
            str(python),
            "-c",
            "import sys, drilling_intelligence as d;"
            "print(d.__file__);"
            "print('LEAK' if any('Prog-Proc' in p for p in sys.path) else 'CLEAN')",
        ],
        cwd=NEUTRAL_CWD,
    )
    lines = probe.stdout.strip().splitlines()
    check(
        f"{kind}: package imports from site-packages",
        probe.returncode == 0 and len(lines) == 2,
        lines[0] if lines else probe.stderr.strip()[:120],
    )
    if len(lines) == 2:
        check(f"{kind}: no source-checkout leakage onto sys.path", lines[1] == "CLEAN", lines[1])
        check(f"{kind}: imported from an installed location", "site-packages" in lines[0], lines[0])

    # 2. the CLI entry point exists and answers
    for flag in ("--help", "--version"):
        result = run([str(cli), flag], cwd=NEUTRAL_CWD)
        check(
            f"{kind}: drillintel {flag}",
            result.returncode == 0,
            result.stdout.strip().splitlines()[0][:80] if result.stdout else result.stderr[:80],
        )

    # 2b. the optional desktop entry point exists and fails closed without Qt.
    #     The smoke test installs the wheel's runtime dependencies only, so Qt is
    #     absent here - which is exactly the state that has to be handled: a
    #     sentence a person can act on, exit code 2, and no traceback from the
    #     dynamic linker.  When Qt *is* present the launcher would open a window
    #     and block, so this check only runs in the Qt-absent state and says so.
    ui_help = run([str(ui_cli), "--help"], cwd=NEUTRAL_CWD)
    check(
        f"{kind}: drillintel-ui --help",
        ui_help.returncode == 0 and "drillintel-ui" in (ui_help.stdout + ui_help.stderr),
        (ui_help.stdout + ui_help.stderr).strip().splitlines()[0][:80] if ui_help.stdout else "",
    )
    qt_probe = run(
        [str(python), "-c", "import PySide6; print(PySide6.__version__)"], cwd=NEUTRAL_CWD
    )
    qt_present = qt_probe.returncode == 0
    if qt_present:
        print(f"  SKIP  {kind}: optional Qt is installed here; the absent-Qt path is not run")
    else:
        ui_absent = run([str(ui_cli)], cwd=NEUTRAL_CWD, timeout=60)
        message = ui_absent.stdout + ui_absent.stderr
        check(
            f"{kind}: drillintel-ui without Qt exits 2",
            ui_absent.returncode == 2,
            f"exit={ui_absent.returncode}",
        )
        check(
            f"{kind}: drillintel-ui without Qt explains what to install",
            "drillintel-ui could not load Qt" in message and "drilling-intelligence[ui]" in message,
            message.strip().splitlines()[0][:80] if message.strip() else "",
        )
        check(
            f"{kind}: drillintel-ui without Qt prints no traceback",
            "Traceback (most recent call last)" not in message,
        )

    # 3. a real workspace, bootstrapped with no migrations/ directory anywhere near it
    ws = workdir / "workspace"
    created = run([str(cli), "workspace", "create", str(ws), "--name", "Smoke"], cwd=NEUTRAL_CWD)
    check(
        f"{kind}: workspace create",
        created.returncode == 0,
        created.stdout.strip().splitlines()[0][:80] if created.stdout else created.stderr[:120],
    )
    check(f"{kind}: workspace.toml written", (ws / "workspace.toml").exists())

    # 4. opening it must build the schema from metadata - the only path a wheel has
    listed = run([str(cli), "wells", "list", "--workspace", str(ws)], cwd=NEUTRAL_CWD)
    combined = listed.stdout + listed.stderr
    check(
        f"{kind}: schema bootstraps from an installed package",
        listed.returncode == 0,
        "stamped-from-metadata" if "stamped-from-metadata" in combined else combined[:120],
    )
    check(
        f"{kind}: the metadata bootstrap path is the one taken",
        "no migration scripts available" in combined or "stamped-from-metadata" in combined,
    )

    dbs = sorted(ws.rglob("*.db"))
    check(f"{kind}: a registry database now exists", bool(dbs), str(dbs[0]) if dbs else "none")
    if dbs:
        import sqlite3

        with sqlite3.connect(dbs[0]) as con:
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            stamped = con.execute("SELECT version_num FROM alembic_version").fetchall()
        check(
            f"{kind}: core tables created",
            {"well", "document", "document_version"} <= tables,
            f"{len(tables)} tables",
        )
        check(f"{kind}: alembic_version stamped", bool(stamped), str(stamped))

    # 4b. the package does not carry a migration history.
    #     The installed product bootstraps from metadata; asking the installed
    #     package where its migrations are must answer "nowhere", and that answer
    #     is what makes the bootstrap path the one that runs.  If a wheel ever
    #     starts shipping migrations, this check notices before the schema path
    #     silently changes.
    migrations = run(
        [
            str(python),
            "-c",
            "from drilling_intelligence.database.migrations import find_migrations_dir;"
            "print(find_migrations_dir())",
        ],
        cwd=NEUTRAL_CWD,
    )
    check(
        f"{kind}: the installed package ships no migration history",
        migrations.returncode == 0 and migrations.stdout.strip() == "None",
        migrations.stdout.strip()[:80] or migrations.stderr.strip()[:80],
    )

    # 4c. a workspace under a path with a space and a non-ASCII character.
    #     Windows is where this breaks: the console script's quoting, the code
    #     page, the short-name form of the directory.  The path is used on every
    #     platform so that a Linux run cannot pass while a Windows run fails.
    hostile = workdir / "well site \u2713 with space"
    created_hostile = run(
        [str(cli), "workspace", "create", str(hostile), "--name", "Hostile"], cwd=NEUTRAL_CWD
    )
    check(
        f"{kind}: workspace create under a path with a space and a non-ASCII character",
        created_hostile.returncode == 0,
        created_hostile.stderr.strip()[:120] or str(hostile)[-40:],
    )
    listed_hostile = run([str(cli), "wells", "list", "--workspace", str(hostile)], cwd=NEUTRAL_CWD)
    check(
        f"{kind}: that workspace opens and bootstraps",
        listed_hostile.returncode == 0,
        listed_hostile.stderr.strip()[:120] or "ok",
    )
    non_ascii = run(
        [str(cli), "wells", "create", "--name", "SMOKE-\u00d8-1", "--workspace", str(hostile)],
        cwd=NEUTRAL_CWD,
    )
    read_back = run([str(cli), "wells", "list", "--workspace", str(hostile)], cwd=NEUTRAL_CWD)
    check(
        f"{kind}: a non-ASCII well name round-trips through the console",
        non_ascii.returncode == 0 and "SMOKE-\u00d8-1" in read_back.stdout,
        (read_back.stdout or read_back.stderr).strip()[:120],
    )

    # 5. write and read a well, so the schema is usable and not merely present
    made = run(
        [str(cli), "wells", "create", "--name", "SMOKE-1", "--workspace", str(ws)], cwd=NEUTRAL_CWD
    )
    check(
        f"{kind}: wells create",
        made.returncode == 0,
        made.stdout.strip().splitlines()[0][:80] if made.stdout else made.stderr[:120],
    )
    again = run([str(cli), "wells", "list", "--workspace", str(ws)], cwd=NEUTRAL_CWD)
    check(f"{kind}: the well reads back", "SMOKE-1" in again.stdout, again.stdout.strip()[:80])

    # 6. the read surfaces run against a valid but empty workspace
    # ``records`` needs a scope and says so with a usable hint when it does not get one, so the
    # smoke test supplies the well it just created rather than asserting on the refusal.
    scope = ["--workspace", str(ws), "--well", "SMOKE-1"]
    for argv in (
        ["records", "list", *scope],
        ["records", "summary", *scope],
        ["records", "review", *scope],
        ["search", "sand", *scope],
        ["timeline", *scope],
        ["knowledge", "status", "--workspace", str(ws)],
        ["index", "status", "--workspace", str(ws)],
        ["fields", "decision", *scope],
    ):
        result = run([str(cli), *argv], cwd=NEUTRAL_CWD)
        check(
            f"{kind}: {' '.join(argv[:2])} runs",
            result.returncode == 0,
            result.stderr.strip().splitlines()[-1][:90] if result.returncode else "ok",
        )

    # 7. doctor runs, and --json really is JSON
    doctor = run([str(cli), "doctor", "--workspace", str(ws)], cwd=NEUTRAL_CWD)
    check(f"{kind}: doctor runs", doctor.returncode in (0, 1), f"exit={doctor.returncode}")
    doctor_json = run([str(cli), "--json", "doctor", "--workspace", str(ws)], cwd=NEUTRAL_CWD)
    payload = None
    try:
        payload = json.loads(doctor_json.stdout)
    except (json.JSONDecodeError, ValueError):
        pass
    # The decision pack: an empty workspace must still yield a valid pack - schema, sections,
    # scope and identity - because "nothing recorded yet" is an answer, not an error.
    decision_json = run(
        [
            str(cli),
            "--json",
            "fields",
            "decision",
            "--workspace",
            str(ws),
            "--well",
            "SMOKE-1",
        ],
        cwd=NEUTRAL_CWD,
    )
    decision_ok = False
    decision_keys: list[str] = []
    try:
        decision_payload = json.loads(decision_json.stdout)
        decision_keys = sorted(decision_payload)
        decision_ok = (
            decision_json.returncode == 0
            and str(decision_payload.get("schema", "")).startswith("decision-pack/")
            and decision_payload.get("subject", {}).get("kind") == "well"
            and decision_payload.get("execution", {}).get("sections") == 0
            and bool(decision_payload.get("identity"))
            and "limitations" in decision_payload
        )
    except (ValueError, AttributeError, TypeError):
        decision_payload = None
    check(
        f"{kind}: fields decision --json is an empty-but-valid pack",
        decision_ok,
        str(decision_keys)[:90]
        if decision_keys
        else decision_json.stdout[:90] or decision_json.stderr[:90],
    )

    # The comparison pack: two fresh wells on an empty workspace must still yield a valid
    # pack - schema, basis, sections, identity - with zero rows everywhere, because "neither
    # well has anything recorded yet" is an answer, not an error.  No golden corpus is used.
    made_two = run(
        [str(cli), "wells", "create", "--name", "SMOKE-2", "--workspace", str(ws)],
        cwd=NEUTRAL_CWD,
    )
    check(f"{kind}: second well create for comparison", made_two.returncode == 0)
    compare_json = run(
        [
            str(cli),
            "--json",
            "fields",
            "compare",
            "--workspace",
            str(ws),
            "--well",
            "SMOKE-1",
            "--well",
            "SMOKE-2",
        ],
        cwd=NEUTRAL_CWD,
    )
    compare_ok = False
    compare_keys: list[str] = []
    try:
        compare_payload = json.loads(compare_json.stdout)
        compare_keys = sorted(compare_payload)
        compare_ok = (
            compare_json.returncode == 0
            and str(compare_payload.get("schema", "")) == "well-comparison/1"
            and compare_payload.get("basis", {}).get("kind") == "explicit_wells"
            and len(compare_payload.get("basis", {}).get("subjects") or []) == 2
            and bool(compare_payload.get("identity"))
            and compare_payload.get("summary", {}).get("metrics", 0) > 0
            and all(section.get("detail") for section in compare_payload.get("sections") or [])
        )
    except (ValueError, AttributeError, TypeError):
        compare_payload = None
    check(
        f"{kind}: fields compare --json is an empty-but-valid pack",
        compare_ok,
        str(compare_keys)[:90]
        if compare_keys
        else compare_json.stdout[:90] or compare_json.stderr[:90],
    )

    # The analyst catalog: list it (parseable, sixteen ids), then answer one question with
    # parseable analyst-answer/1 JSON on the empty workspace.
    catalog_json = run(
        [str(cli), "--json", "analyze", "--list-questions", "--workspace", str(ws)],
        cwd=NEUTRAL_CWD,
    )
    catalog_ok = False
    try:
        catalog_payload = json.loads(catalog_json.stdout)
        ids = [entry.get("question_id") for entry in catalog_payload.get("questions") or []]
        catalog_ok = (
            catalog_json.returncode == 0
            and catalog_payload.get("count") == 16
            and ids[0] == "well_profile"
            and ids[-1] == "offset_comparison"
        )
    except (ValueError, AttributeError, TypeError, IndexError):
        catalog_payload = None
    check(
        f"{kind}: analyze --list-questions prints the sixteen-question catalog",
        catalog_ok,
        str(catalog_json.stdout[:90]) if catalog_payload is None else "16 ids",
    )
    analyze_json = run(
        [
            str(cli),
            "--json",
            "analyze",
            "--question",
            "npt_summary",
            "--well",
            "SMOKE-1",
            "--workspace",
            str(ws),
        ],
        cwd=NEUTRAL_CWD,
    )
    analyze_ok = False
    try:
        analyze_payload = json.loads(analyze_json.stdout)
        analyze_ok = (
            analyze_json.returncode == 0
            and str(analyze_payload.get("schema", "")) == "analyst-answer/1"
            and analyze_payload.get("question_id") == "npt_summary"
            and bool(analyze_payload.get("identity"))
            and "answer" in analyze_payload
        )
    except (ValueError, AttributeError, TypeError):
        analyze_payload = None
    check(
        f"{kind}: analyze --question --json is an empty-but-valid answer",
        analyze_ok,
        str(analyze_json.stdout[:90]) if analyze_payload is None else "analyst-answer/1",
    )

    report_path = workdir / "smoke-report.html"
    report_json = run(
        [
            str(cli),
            "--json",
            "fields",
            "report",
            "--workspace",
            str(ws),
            "--well",
            "SMOKE-1",
            "--output",
            str(report_path),
        ],
        cwd=NEUTRAL_CWD,
    )
    report_ok = False
    report_identity = ""
    try:
        report_payload = json.loads(report_json.stdout)
        report_identity = str(report_payload.get("identity") or "")
        html = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        report_ok = (
            report_json.returncode == 0
            and report_payload.get("schema") == "engineering-report/1"
            and report_payload.get("mode") == "single_well"
            and bool(report_identity)
            and html.startswith("<!DOCTYPE html>")
            and report_identity in html
            and "<svg" in html
            and "<script" not in html.lower()
            and "http://www.w3.org/2000/svg" in html
            and "https://" not in html
        )
    except (ValueError, AttributeError, TypeError, OSError):
        report_payload = None
    check(
        f"{kind}: fields report --json --output is a standalone engineering report",
        report_ok,
        report_identity[:16] if report_ok else (report_json.stderr or report_json.stdout)[:90],
    )
    audit_path = workdir / "smoke-report-audit.html"
    audit_json = run(
        [
            str(cli),
            "--json",
            "fields",
            "report",
            "--workspace",
            str(ws),
            "--well",
            "SMOKE-1",
            "--verify-citations",
            "--output",
            str(audit_path),
        ],
        cwd=NEUTRAL_CWD,
    )
    audit_ok = False
    audit_overall = ""
    try:
        audit_payload = json.loads(audit_json.stdout)
        audit_html = audit_path.read_text(encoding="utf-8") if audit_path.exists() else ""
        audit_overall = str((audit_payload.get("audit") or {}).get("overall") or "")
        audit_ok = (
            audit_payload.get("schema") == "engineering-report-audited/1"
            and (audit_payload.get("report") or {}).get("identity") == report_identity
            and audit_overall in {"FULLY_VERIFIED", "PARTIALLY_VERIFIED", "FAILED", "INCOMPLETE"}
            and audit_overall == "PARTIALLY_VERIFIED"
            and int((audit_payload.get("audit") or {}).get("eligible") or 0) == 0
            and "Citation verification was requested" in audit_html
            and "FULLY_VERIFIED" not in audit_html
            and audit_overall in audit_html
            and "<script" not in audit_html.lower()
            and "Citation verification was not requested" in html
        )
    except (ValueError, AttributeError, TypeError, OSError, KeyError):
        audit_payload = None
    check(
        f"{kind}: fields report --verify-citations is a separate audit of the same report",
        audit_ok,
        audit_overall or (audit_json.stderr or audit_json.stdout)[:90],
    )
    render_probe = run(
        [
            str(python),
            "-c",
            "from drilling_intelligence.reporting import render_svg; "
            "svg = render_svg({'exhibit_id':'smoke','title':'Smoke','state':'NO_DATA',"
            "'exhibit_type':'state','subjects':[],'series':[],'unit':None,'reason':'empty'}); "
            "print('SVG' if svg.startswith('<svg') and '<script' not in svg else 'BAD')",
        ],
        cwd=NEUTRAL_CWD,
    )
    check(
        f"{kind}: installed package renders SVG without the source tree",
        render_probe.returncode == 0 and render_probe.stdout.strip() == "SVG",
        render_probe.stdout.strip() or render_probe.stderr[:90],
    )

    check(
        f"{kind}: doctor --json is parseable JSON",
        isinstance(payload, dict),
        str(sorted(payload))[:90] if isinstance(payload, dict) else doctor_json.stdout[:90],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", action="store_true", help="test the wheel only")
    parser.add_argument("--sdist", action="store_true", help="test the sdist only")
    parser.add_argument("--keep", action="store_true", help="do not delete the temporary tree")
    args = parser.parse_args()
    want_sdist = args.sdist or not args.wheel
    want_wheel = args.wheel or not args.sdist

    # The whole smoke test runs under a directory whose name carries a space and
    # a non-ASCII character.  Everything below - the virtualenv, the wheel, the
    # workspace - therefore lives at such a path, which is the release question
    # on Windows and is not exercised by a plain /tmp path on Linux.
    workdir = Path(tempfile.mkdtemp(prefix="drillintel-smoke-")) / HOSTILE_DIR_NAME
    workdir.mkdir(parents=True)
    NEUTRAL_CWD.mkdir(parents=True, exist_ok=True)
    dist = workdir / "dist"
    dist.mkdir()
    try:
        wheel, sdist = build_dists(dist, sdist=want_sdist)
        if want_wheel:
            if not check("a wheel was built", wheel is not None):
                return 1
            smoke("wheel", wheel, workdir / "wheel")  # type: ignore[arg-type]
        if want_sdist:
            if not check("an sdist was built", sdist is not None):
                return 1
            smoke("sdist", sdist, workdir / "sdist")  # type: ignore[arg-type]
    finally:
        if args.keep:
            print(f"\nkept {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    if _failures:
        print(f"RELEASE SMOKE FAILED - {len(_failures)} check(s):")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("RELEASE SMOKE PASSED - clean install, schema bootstrap and CLI all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
