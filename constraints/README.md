# Dependency constraints

**Status:** repository-owned pins for the versions the CI builds and tests.

`pyproject.toml` keeps version ranges so an operator can install a newer
patch release. These files pin the exact resolution the repository proves.
They are *constraints*, not a lock file: `pip install -c <file>` applies them
to the packages that are named and to nothing else, and a package that is not
listed is not affected.

| File | Set | Used by |
| --- | --- | --- |
| `runtime.txt` | the installed product: persistence, extraction, HTTP transport | `pip install -c constraints/runtime.txt .` |
| `development.txt` | runtime plus the `dev` extra (pytest, ruff, build, pytest-cov, mypy) | the Linux and Windows test jobs |
| `ui.txt` | development plus the optional `ui` extra (PySide6-Essentials) | the offscreen Qt jobs |
| `build.txt` | the build backend (setuptools, wheel) | the isolated build environment |

## Why constraints and not a lock file

`uv.lock`, `poetry.lock` and `requirements.txt` all pin one resolution. This
repository installs one product on two operating systems and two Python
runtimes (3.11 and 3.14), with an optional Qt extra. A single universal lock
would either carry a resolution this platform never installs or pin the
platform-specific Qt wheel for every platform. Constraints keep the
resolution per set and let pip choose the correct wheel for the machine,
while the *version* stays fixed.

The cost is that a pin has to be renewed by hand. That is the point: the
renewal is a reviewed change with a CI run behind it, not a silent resolution
at install time.

## Using them

```bash
python -m pip install -c constraints/development.txt -c constraints/build.txt -e ".[dev]"
python -m pip install -c constraints/ui.txt -c constraints/build.txt -e ".[dev,ui]"
```

The build constraints are passed separately because pip resolves the build
backend in its own isolated environment.

`pip` itself is not pinned. It is the installer, not a dependency of the
product, and pinning it would make the CI depend on a pip release that the
product never asked for.

## Updating a pin

1. Change the version in `pyproject.toml` if the range moves.
2. Regenerate the affected file with the tool that owns the format:

   ```bash
   python tools/constraints.py --set runtime
   python tools/constraints.py --set development
   python tools/constraints.py --set ui
   python tools/constraints.py --set build
   ```

   The tool resolves the set in a throw away virtual environment, writes the
   file and prints every pin. It reads the requirements out of
   `pyproject.toml`, so there is no second list to keep in step. A file that
   was edited by hand fails the check:

   ```bash
   python tools/constraints.py --set runtime --check
   ```

3. Run `python tools/dependency_audit.py` against the new set.
4. Push. The Linux and Windows workflows install with the new pins, so a pin
   that has no wheel for a supported runtime fails the run instead of failing
   on a user's machine.

## Vulnerability audit

`tools/dependency_audit.py` audits the installed environment with
`pip-audit`. It distinguishes three outcomes and only one of them is a
failure:

| Outcome | Meaning | Build |
| --- | --- | --- |
| `clean` | the advisory service answered and reported no known vulnerability | pass |
| `vulnerable` | the advisory service answered and reported a known vulnerability | **fail** |
| `unavailable` | the advisory service could not be reached | warn, pass, recorded |

An advisory-service outage is not a defect in this repository, and the CI job
must not report it as one. It is still recorded, as an explicit
`dependency-audit.json` entry and a workflow warning, so a silent outage
cannot be mistaken for a clean audit.
