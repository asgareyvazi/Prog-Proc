"""Shared Cobertura path normalization for coverage_report and qt_coverage_gate.

The producer (coverage.py via pytest-cov) writes Cobertura XML in two shapes:

- Full repo-relative paths: ``src/drilling_intelligence/ui/foo.py``
- Package-root-relative paths: ``ui/foo.py`` with ``<sources><source>.../src/drilling_intelligence</source>``
- Windows backslashes must be normalized.
- Sources may be absolute, repo-relative, or package-relative.
- Unrelated files (tests/tools/deps) must be excluded, not counted as product.

This module is the single source of truth for mapping an XML ``<class filename>``
to a product-relative path (``core/units.py``) or ``None`` if not a product file.

Both :mod:`tools.coverage_report` and :mod:`tools.qt_coverage_gate` must use it so that
view overall and XML totals are compared on the same population.
"""

from __future__ import annotations

PACKAGE_MARKER = "drilling_intelligence/"
SOURCE_PREFIX = "src/drilling_intelligence/"

# Groups as defined in coverage_report
GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "database": ("database",),
    "extraction": ("extraction",),
    "document/knowledge/promotion": ("documents", "knowledge", "operations"),
    "cli": ("cli",),
    "review": ("review",),
    "qt/ui": ("ui",),
}
OTHER_GROUP = "other packages"


def relative_to_package(path: str) -> str | None:
    """Map an XML filename to a product-relative path or ``None`` if not product.

    Mirrors the contract in :func:`tools.coverage_report.relative_to_package`.

    - Normalizes backslashes.
    - If ``drilling_intelligence/`` occurs, returns the suffix after it.
    - Otherwise, rejects ``src/``, ``tests/``, ``tools/``, absolute and ``..`` paths
      (those are non-product when no package marker is present).
    - If path is ``src/drilling_intelligence/...``, strips that prefix.
    - Otherwise returns the normalized path as-is (package-root-relative case).

    Examples:
        ``src/drilling_intelligence/ui/foo.py`` -> ``ui/foo.py``
        ``ui/foo.py`` (with package-root source) -> ``ui/foo.py``
        ``/abs/src/drilling_intelligence/ui/foo.py`` -> ``ui/foo.py`` (via marker)
        ``tests/ui/test_workbench.py`` -> ``None``
        ``C:\\src\\drilling_intelligence\\ui\\foo.py`` -> ``ui/foo.py``
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
    """``ui/foo.py`` -> ``ui``, ``core/units.py`` -> ``core``."""
    relative = relative_to_package(path)
    if relative is None:
        return OTHER_GROUP
    parts = relative.split("/")
    if len(parts) < 2:
        return OTHER_GROUP
    return parts[0]


def group_of(path: str) -> str:
    """Map a product-relative path to its coverage group."""
    package = package_of(path)
    for group, packages in GROUPS.items():
        if package in packages:
            return group
    return OTHER_GROUP


def xml_filename_to_relative(
    filename: str | None, sources: list[str], rooted_in_package: bool
) -> str | None:
    """Resolve an XML ``<class filename>`` to a product-relative path.

    This is the gate's entry point for XML filenames. It applies the same
    ``relative_to_package`` rules, but also respects the ``rooted_in_package``
    flag derived from ``<sources>``:

    - If ``rooted_in_package`` is False and the filename lacks ``drilling_intelligence/``,
      then a bare ``ui/foo.py`` is ambiguous (could be product or unrelated) and we
      return ``None`` to avoid inflating counts. The original ``coverage_report`` logic
      is ``if PACKAGE_MARKER not in filename and not rooted_in_package: skip``.
    - Empty/missing filenames return ``None``.

    The caller should treat ``None`` as "not a product file, do not count".
    """
    if not filename:
        return None
    normalised = filename.replace("\\", "/")
    # If the XML is not rooted in the package, a bare path without the package marker
    # cannot be reliably mapped to product — treat as non-product.
    if PACKAGE_MARKER not in normalised and not rooted_in_package:
        # But still allow the marker case via relative_to_package
        # Check via relative_to_package which will return None for src/tests etc, but for
        # package-root-relative without marker and without rooted flag, we must return None.
        # The check above already ensures we return None for non-rooted bare ui/ paths.
        # To be safe, consult relative_to_package — if it would return a package path but
        # we are not rooted, we still return None for safety.
        # Exception: if the normalised path itself is already product-relative (e.g. "ui/foo.py")
        # but not rooted, we cannot know — fail closed.
        return None
    return relative_to_package(filename)


def is_ui_file(relative: str | None) -> bool:
    """True if a product-relative path belongs to the ``qt/ui`` group."""
    if relative is None:
        return False
    return group_of(relative) == "qt/ui"
