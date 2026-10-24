"""Tests for the constraint regenerator.

The regenerator is the tool that renews a pin, so the parts that decide what
lands in a constraint file are tested directly: what is filtered out, how the
file is rendered, and that the file on disk still says what the metadata
resolves to.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.constraints import (
    BUILD_PACKAGES,
    CONSTRAINTS_DIR,
    SETS,
    package_lines,
    render,
    requirement_for,
)


def test_package_lines_keeps_only_pinned_requirements() -> None:
    frozen = "\n".join(
        [
            "SQLAlchemy==2.1.5",
            "alembic==1.20.0",
            "pymupdf==1.28.2",
        ]
    )
    # Sorted case-insensitively, so the file reads alphabetically.
    assert package_lines(frozen) == ["alembic==1.20.0", "pymupdf==1.28.2", "SQLAlchemy==2.1.5"]


def test_package_lines_drops_the_installer_and_the_project() -> None:
    frozen = "\n".join(
        [
            "pip==26.2.1",
            "setuptools==84.2.0",
            "wheel==0.48.0",
            "-e git+https://example.invalid/pkg@abc#egg=drilling_intelligence",
            "drilling-intelligence==0.0.1a0",
            "ruff==0.17.1",
        ]
    )
    assert package_lines(frozen) == ["ruff==0.17.1"]


def test_package_lines_drops_anything_that_is_not_a_pin() -> None:
    frozen = "\n".join(
        [
            "SQLAlchemy>=2.0.30,<3.0",
            "ruff==0.17.1 ; python_version >= '3.11'",
            "certifi==2026.10.19",
        ]
    )
    assert package_lines(frozen) == ["certifi==2026.10.19"]


def test_package_lines_is_sorted_and_deduplicated() -> None:
    frozen = "b==2.0\na==1.0\nb==2.0\n"
    assert package_lines(frozen) == ["a==1.0", "b==2.0"]


def test_render_names_the_set_and_stamps_the_date() -> None:
    text = render("runtime", ["SQLAlchemy==2.1.5"], generated="2026-10-20")
    assert "Runtime dependency set" in text
    assert "Generated on 2026-10-20." in text
    assert "--set runtime" in text
    assert "SQLAlchemy==2.1.5" in text


def test_render_of_the_build_set_explains_the_isolated_environment() -> None:
    text = render("build", ["setuptools==84.2.0", "wheel==0.48.0"], generated="2026-10-20")
    assert "isolated environment" in text


def test_every_set_has_a_file_and_a_title() -> None:
    for name in SETS:
        path = CONSTRAINTS_DIR / f"{name}.txt"
        assert path.exists(), path
        text = path.read_text(encoding="utf-8")
        assert "Generated on" in text
        assert f"--set {name}" in text


def test_the_build_set_pins_the_build_backend() -> None:
    text = (CONSTRAINTS_DIR / "build.txt").read_text(encoding="utf-8")
    for package in BUILD_PACKAGES:
        assert f"{package}==" in text


def test_the_build_set_keeps_the_backend_and_its_dependency() -> None:
    from tools.constraints import package_lines

    frozen = "\n".join(
        [
            "pip==26.2.1",
            "setuptools==84.2.0",
            "wheel==0.48.0",
            "packaging==26.3",
        ]
    )
    assert package_lines(frozen, exclude=("pip", "drilling-intelligence")) == [
        "packaging==26.3",
        "setuptools==84.2.0",
        "wheel==0.48.0",
    ]


def test_the_runtime_set_does_not_pin_a_dev_tool() -> None:
    text = (CONSTRAINTS_DIR / "runtime.txt").read_text(encoding="utf-8")
    for tool in ("pytest", "ruff", "build", "mypy", "pytest-cov"):
        assert f"\n{tool}==" not in text


def test_the_ui_set_pins_the_optional_qt_runtime() -> None:
    text = (CONSTRAINTS_DIR / "ui.txt").read_text(encoding="utf-8")
    assert "PySide6_Essentials==" in text


def test_requirement_for_returns_the_name() -> None:
    assert requirement_for("SQLAlchemy==2.1.5") == "SQLAlchemy"


def test_requirements_are_resolved_from_the_metadata() -> None:
    from tools.constraints import _direct_requirements

    runtime = _direct_requirements(())
    development = _direct_requirements(("dev",))
    ui = _direct_requirements(("dev", "ui"))
    assert any(item.startswith("SQLAlchemy") for item in runtime)
    assert not any(item.startswith("pytest") for item in runtime)
    assert any(item.startswith("pytest") for item in development)
    assert not any(item.startswith("PySide6") for item in development)
    assert any(item.startswith("PySide6") for item in ui)


# --------------------------------------------------------------------------- the shipped files


def _pins(name: str) -> list[str]:
    """The pinned lines of one constraint file, without the comments."""
    text = (CONSTRAINTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _date_in(name: str) -> str:
    text = (CONSTRAINTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("# Generated on "):
            return line.removeprefix("# Generated on ").removesuffix(".")
    raise AssertionError(f"constraints/{name}.txt has no generation stamp")


def test_every_pin_is_an_exact_version() -> None:
    """A range is a floating pin, and a floating pin is not a pin."""
    for name in SETS:
        for line in _pins(name):
            assert "==" in line, f"{name}: {line}"
            version = line.split("==", 1)[1]
            assert version and not any(mark in version for mark in "<>!~*, "), f"{name}: {line}"


def test_no_pin_carries_an_environment_marker() -> None:
    """A marker makes a pin mean different things on two platforms.

    The files are read on Linux and on Windows, so a pin that only applies to
    one interpreter or one platform would be read as a pin on the other.
    """
    for name in SETS:
        for line in _pins(name):
            assert ";" not in line, f"{name}: {line}"
            assert "--hash" not in line, f"{name}: {line}"
            assert not line.startswith("-"), f"{name}: {line}"


def test_no_set_pins_the_same_package_twice() -> None:
    for name in SETS:
        names = [line.split("==", 1)[0].lower().replace("_", "-") for line in _pins(name)]
        assert len(names) == len(set(names)), name


def test_the_file_on_disk_is_what_the_tool_renders() -> None:
    """Drift check: the file is the tool's output, not a hand-edited list."""
    from tools.constraints import render

    for name in SETS:
        path = CONSTRAINTS_DIR / f"{name}.txt"
        expected = render(name, _pins(name), generated=_date_in(name))
        assert path.read_text(encoding="utf-8") == expected, name


def test_the_runtime_set_pins_the_transitive_dependencies_too() -> None:
    """A constraint file that names only the direct requirements floats the rest."""
    from importlib.metadata import PackageNotFoundError, requires

    from tools.constraints import _direct_requirements

    pinned = {line.split("==", 1)[0].lower().replace("_", "-") for line in _pins("runtime")}
    direct = _direct_requirements(())
    for requirement in direct:
        name = requirement.split(";")[0].strip()
        for specifier in name.split(","):
            package = specifier.strip().split(">")[0].split("<")[0].split("=")[0].strip()
            if not package:
                continue
            assert package.lower().replace("_", "-") in pinned, package
            try:
                declared = requires(package) or []
            except PackageNotFoundError:  # pragma: no cover - the extra is not installed
                continue
            for entry in declared:
                if ";" in entry:  # a marker means it does not apply everywhere
                    continue
                transitive = entry.split(";")[0].strip().split(">")[0].split("<")[0]
                transitive = transitive.split("=")[0].strip()
                if not transitive:
                    continue
                assert transitive.lower().replace("_", "-") in pinned, transitive


def test_the_ui_set_contains_every_runtime_pin() -> None:
    """The UI set is the runtime set plus the optional desktop runtime."""
    runtime = {line.split("==", 1)[0].lower().replace("_", "-") for line in _pins("runtime")}
    ui = {line.split("==", 1)[0].lower().replace("_", "-") for line in _pins("ui")}
    assert runtime <= ui, sorted(runtime - ui)


def test_the_product_sets_do_not_pin_the_build_backend_or_the_auditor() -> None:
    """The backend and the auditor are tools, not dependencies of the product."""
    for name in ("runtime", "development", "ui"):
        text = (CONSTRAINTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
        for tool in ("setuptools", "wheel", "pip_audit", "pip-audit"):
            assert f"\n{tool}==" not in text, f"{name}: {tool}"


def test_the_audit_set_pins_the_auditor_to_one_exact_version() -> None:
    pins = _pins("audit")
    auditor = [line for line in pins if line.lower().startswith("pip_audit==")]
    assert len(auditor) == 1, pins
    assert auditor[0].split("==", 1)[1]


def test_the_auditor_is_installed_from_its_pins_and_not_as_a_range() -> None:
    """A floating auditor would make two audits of one commit incomparable."""
    for name in ("ci.yml", "ci-windows.yml"):
        text = (Path(".github/workflows") / name).read_text(encoding="utf-8")
        assert "pip-audit>=2.9,<3" not in text, name
        assert "-c constraints/audit.txt pip-audit" in text, name
        assert ".audit-venv" in text, name
