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
NEUTRAL_CWD = Path(tempfile.gettempdir())

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f'  [{detail}]' if detail else ''}")
    if not condition:
        _failures.append(label)
    return condition


def run(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=600, check=False
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


def fresh_venv(root: Path) -> Path:
    venv.create(root, with_pip=True, clear=True)
    exe = "Scripts" if os.name == "nt" else "bin"
    return root / exe


def install(bin_dir: Path, target: Path) -> None:
    result = run(
        [str(bin_dir / "pip"), "install", "--quiet", "--disable-pip-version-check", str(target)],
        cwd=NEUTRAL_CWD,
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        raise SystemExit(f"installing {target.name} failed")


def smoke(kind: str, artefact: Path, workdir: Path) -> None:
    """Install one artefact into a clean venv and exercise the installed product."""
    print(f"\n=== {kind}: {artefact.name} ===")
    bin_dir = fresh_venv(workdir / "venv")
    install(bin_dir, artefact)
    python = bin_dir / "python"
    cli = bin_dir / "drillintel"

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

    workdir = Path(tempfile.mkdtemp(prefix="drillintel-smoke-"))
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
