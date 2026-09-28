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


@pytest.fixture(autouse=True)
def _isolate_cli_logging():
    """Keep a CLI invocation's log handler from outliving the test that made it.

    ``configure_logging`` attaches ``StreamHandler(sys.stderr)`` to the ``drilling_intelligence``
    logger (``core/logging.py:119``) - bound to whatever ``sys.stderr`` is *at that moment* - and
    then sets a module-global ``_CONFIGURED`` idempotence flag.  Under ``capsys`` that stream is
    pytest's capture stream, which pytest closes at teardown; because the flag stays set, the next
    ``main()`` returns early and keeps writing into the closed stream, so ``ValueError: I/O
    operation on closed file`` leaks into a later test's captured output.

    That is cross-test contamination, not a product defect, but it made two knowledge-CLI
    assertions about "no Traceback in the output" fail or pass depending purely on which modules
    happened to run first.  Restoring the handler list *and* the flag returns each test to the
    fresh-process state a real CLI run would have.
    """
    import logging

    from drilling_intelligence.core import logging as platform_logging

    logger = logging.getLogger("drilling_intelligence")
    saved_handlers = list(logger.handlers)
    saved_configured = platform_logging._CONFIGURED
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            if handler not in saved_handlers:
                logger.removeHandler(handler)
                handler.close()
        platform_logging._CONFIGURED = saved_configured


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
def well(session, tmp_path: Path):
    """A real well, created through the well registry.

    Knowledge tests need one because a ``well`` subject has to point at a row that exists: the edge
    validator rejects a relation to a well nobody registered, and that rejection is part of what the
    knowledge layer promises.  Created through ``WellRepository`` rather than by hand, so the
    workspace/project links are as real as the well's.
    """
    from drilling_intelligence.wells.repository import WellRepository

    repository = WellRepository(session)
    repository.get_or_create_workspace(str(tmp_path), name="Knowledge Test")
    project = repository.get_or_create_project("Knowledge Test")
    return repository.create_well("A-3", project_id=project.id)


@pytest.fixture
def corpus_in_workspace(corpus_dir: Path, workspace) -> Path:
    """Corpus copied under the workspace so identity paths are workspace-relative."""
    import shutil

    target = workspace.root / "documents"
    target.mkdir(parents=True, exist_ok=True)
    for path in corpus_dir.iterdir():
        shutil.copy2(path, target / path.name)
    return target
