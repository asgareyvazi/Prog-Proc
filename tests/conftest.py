"""Shared fixtures: a real document corpus, a temporary workspace, a migrated DB.

Everything here is built on real files and a real SQLite database.  There are no
mock parsers and no fake extraction results anywhere in this suite (master spec
section 92).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.fixtures.generate import GROUND_TRUTH, build_corpus  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    return GROUND_TRUTH


@pytest.fixture
def corpus_dir(tmp_path: Path) -> Path:
    """A generated corpus of genuine PDF/XLSX/DOCX/CSV/TXT files."""
    root = tmp_path / "corpus"
    build_corpus(root)
    return root


@pytest.fixture
def settings(tmp_path: Path):
    from drilling_intelligence.config.settings import Settings

    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[app]",
                'data_dir = ".drillintel"',
                "",
                "[database]",
                'sqlite_filename = "drilling_intelligence.db"',
                "",
                "[logging]",
                'level = "WARNING"',
                "",
                "[ai]",
                "enabled = false",
                "require_ai = false",
                "",
                "[mineru]",
                'mode = "disabled"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return Settings.load(config)


@pytest.fixture
def workspace(tmp_path: Path, settings):
    """An initialised workspace with migrated databases."""
    from drilling_intelligence.wells.workspace import Workspace

    root = tmp_path / "workspace"
    workspace = Workspace.create(root, settings, name="Test Workspace")
    return workspace


@pytest.fixture
def db(workspace):
    """A database built from the models (fast), in the workspace's own data directory.

    ``Workspace`` creates its SQLite file lazily, so the directory has to exist before a
    URL can be opened; the schema here is ``create_all`` rather than migrated, which is
    what unit tests want (the migration path is covered in ``tests/integration``).
    """
    from drilling_intelligence.database.session import Database

    Path(workspace.database_url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
    database = Database.from_url(workspace.database_url, workspace.settings)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def session(db):
    with db.unit_of_work() as session:
        yield session


@pytest.fixture
def corpus_in_workspace(corpus_dir: Path, workspace) -> Path:
    """Corpus copied under the workspace so identity paths are workspace-relative."""
    import shutil

    target = workspace.root / "documents"
    target.mkdir(parents=True, exist_ok=True)
    for path in corpus_dir.iterdir():
        shutil.copy2(path, target / path.name)
    return target
