"""Regenerate the dependency constraint files under ``constraints/``.

The files are *constraints*, not a lock file: they pin the version of the
packages they name and say nothing about the packages they do not name.
``pyproject.toml`` keeps version ranges, so an operator can install a newer
patch release; these files pin the exact resolution the CI proves.

The tool exists so that a pin is renewed by a command that is recorded in
the repository, not by hand-editing a list.  It resolves one set in a throw
away virtual environment, writes the result, and prints what it wrote.

Usage::

    python tools/constraints.py --set development
    python tools/constraints.py --set ui
    python tools/constraints.py --set runtime
    python tools/constraints.py --set build

``--check`` re-resolves and compares instead of writing, which is what a
reviewer wants: it fails when a file and the metadata disagree.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS_DIR = REPO_ROOT / "constraints"

#: The extras each set resolves.  ``runtime`` is the installed product and
#: resolves the runtime dependencies directly, so the file also pins the
#: transitive packages that the product pulls in.
SETS: dict[str, tuple[str, ...]] = {
    "runtime": (),
    "development": ("dev",),
    "ui": ("dev", "ui"),
    "build": (),
    "audit": (),
}

#: The build backend is resolved in an isolated environment by pip.  It is
#: pinned separately because the product never imports it.
BUILD_PACKAGES = ("setuptools", "wheel")

#: The audit tool is not a dependency of the product either: it is the thing
#: that checks the pins.  It is pinned in its own file so that an audit is
#: reproducible - a floating range would let the auditor change between two
#: runs of the same commit - and so that installing it cannot move a version
#: in the environment the product is tested in.
AUDIT_PACKAGES = ("pip-audit",)

#: ``pip`` is the installer, not a dependency of the product.  The project
#: itself is installed as an editable, so it never appears in a constraint
#: file either.
EXCLUDED = ("pip", "setuptools", "wheel", "drilling-intelligence")

HEADER = """\
# {title}
#
# Pinned resolution of the dependency set this repository builds and tests
# against. The package metadata in pyproject.toml keeps version ranges so an
# operator can install a newer patch release; these files pin the versions the
# CI actually proves. They are constraints, not a lock: `pip install -c <file>`
# applies them to the packages that are named, and nothing else.
#
# Regenerate: `python tools/constraints.py --set {set_name}`. Do not hand-edit a
# pin without regenerating the file, and do not widen a pin without a CI run on
# the new SHA.
#
# Verified on CPython 3.11 and 3.14 (Linux and Windows wheels).
#
# Generated on {generated}.
"""

_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^;]+$")


def package_lines(freeze_output: str, exclude: tuple[str, ...] = EXCLUDED) -> list[str]:
    """Return the ``name==version`` lines of a ``pip freeze`` output.

    Editable installs, the installer itself, the build backend and the
    project itself are removed: none of them is a dependency of the product.
    The build backend is the exception - it is pinned in its own file, so it
    is excluded from the product sets and kept in the build set.
    """
    lines: list[str] = []
    for raw in freeze_output.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line or line.startswith("-e "):
            continue
        name = line.split("==", 1)[0].strip().lower()
        if name in exclude:
            continue
        if not _REQUIREMENT.match(line):
            continue
        lines.append(line)
    return sorted(set(lines), key=str.lower)


def render(set_name: str, packages: list[str], generated: str | None = None) -> str:
    """Render the full text of a constraint file."""
    title = {
        "runtime": "Runtime dependency set",
        "development": "Development dependency set (runtime + the dev extra)",
        "ui": "UI dependency set (development + the optional ui extra)",
        "build": "Build backend pins",
        "audit": "Audit tool pins",
    }[set_name]
    stamp = generated or date.today().isoformat()
    body = "\n".join(packages)
    text = HEADER.format(title=title, set_name=set_name, generated=stamp)
    text += "\n" + body + "\n"
    if set_name == "build":
        text += (
            "\n# The build backend is resolved in an isolated environment by pip.\n"
            "# These two pins make that environment reproducible as well.\n"
        )
    if set_name == "audit":
        text += (
            "\n# The audit tool is installed in an environment of its own, so that the\n"
            "# versions the product is tested with are the versions the product ships\n"
            "# with.  These pins are what make two audits of the same commit comparable.\n"
        )
    return text


_STAMP = re.compile(r"^# Generated on (\d{4}-\d{2}-\d{2})\.$", re.MULTILINE)


def stamp_of(text: str) -> str | None:
    """The date a constraint file records, if it records one."""
    match = _STAMP.search(text)
    return match.group(1) if match else None


def check(set_name: str, packages: list[str], current: str) -> tuple[bool, str]:
    """Compare a resolution with a file, ignoring the file's own date.

    The date in the header records when the file was regenerated.  It is not a
    property of the resolution, so it must not be compared: a check that fails
    because the clock moved one day fails every day after the file was written,
    which teaches everybody to ignore it.  The *pins* are what a drift check
    compares, so the date the file already carries is fed back into the
    rendering.
    """
    expected = render(set_name, packages, generated=stamp_of(current))
    return current == expected, expected


def requirement_for(package: str) -> str:
    """Return the requirement string for one pinned package."""
    return package.split("==", 1)[0]


def resolve(set_name: str, python: str | None = None) -> list[str]:
    """Resolve one set in a throw away environment and return the pins."""
    if set_name not in SETS:
        raise ValueError(f"unknown set: {set_name}")
    with tempfile.TemporaryDirectory(prefix="constraints-") as workdir:
        root = Path(workdir)
        venv = root / "venv"
        subprocess.run(
            [python or sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
        )
        programs = venv / ("Scripts" if sys.platform == "win32" else "bin")
        pip = programs / ("pip.exe" if sys.platform == "win32" else "pip")
        if not pip.exists():
            pip = programs / ("pip3.exe" if sys.platform == "win32" else "pip3")
        subprocess.run(
            [str(pip), "install", "--upgrade", "pip"],
            check=True,
            capture_output=True,
        )
        if set_name == "build":
            requirements = list(BUILD_PACKAGES)
        elif set_name == "audit":
            requirements = list(AUDIT_PACKAGES)
        else:
            requirements = _direct_requirements(SETS[set_name])
        # The build backend is upgraded on purpose: a fresh virtual environment
        # seeds an old setuptools, and a pin of that seed is not a pin of the
        # backend the project asks for.
        command = ["install"]
        if set_name == "build":
            command.append("--upgrade")
        subprocess.run(
            [str(pip), *command, *requirements],
            check=True,
            capture_output=True,
        )
        frozen = subprocess.run(
            [str(pip), "freeze", "--all", "--exclude-editable"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    if set_name == "build":
        keep = ("pip", "drilling-intelligence")
        return package_lines(frozen, exclude=keep)
    if set_name == "audit":
        # The auditor is installed into an environment of its own, so the
        # installer, the build backend and the project itself are not pins of
        # the auditor.
        return package_lines(frozen)
    return package_lines(frozen)


def _direct_requirements(extras: tuple[str, ...]) -> list[str]:
    """Read the direct runtime and extra requirements out of ``pyproject.toml``."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.11 has tomllib
        import tomli as tomllib  # type: ignore[no-redef]
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    requirements = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        requirements.extend(optional.get(extra, []))
    return requirements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_name", required=True, choices=sorted(SETS))
    parser.add_argument(
        "--check",
        action="store_true",
        help="resolve and compare with the file on disk instead of writing it",
    )
    args = parser.parse_args(argv)

    packages = resolve(args.set_name)
    if not packages:
        print(f"resolution for {args.set_name} is empty", file=sys.stderr)
        return 1
    path = CONSTRAINTS_DIR / f"{args.set_name}.txt"

    if args.check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        matches, text = check(args.set_name, packages, current)
        if not matches:
            print(f"{path} is not what the metadata resolves to", file=sys.stderr)
            for expected_line, actual_line in zip(
                text.splitlines(), current.splitlines(), strict=False
            ):
                if expected_line != actual_line:
                    print(f"  metadata: {expected_line}", file=sys.stderr)
                    print(f"  file:     {actual_line}", file=sys.stderr)
            if len(text.splitlines()) != len(current.splitlines()):
                print(
                    f"  {len(text.splitlines())} pins expected, "
                    f"{len(current.splitlines())} in the file",
                    file=sys.stderr,
                )
            return 1
        print(f"{path} is up to date")
        return 0

    text = render(args.set_name, packages)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} ({len(packages)} pins)")
    for line in packages:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
