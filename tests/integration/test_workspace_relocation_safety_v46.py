"""V4.6 — relocation safety, path canonicalization and atomicity.

The binding suite proves *which* workspace a pipeline resolves to. This one attacks the cases where
the folder on disk and the database disagree, because that is where a relocation rule can do damage:
it is the only place in the system allowed to rewrite ``workspace.root_path``, and a rewrite is
silent, permanent and invisible to every workspace-scoped query afterwards.

The evidence rule under test is deliberately narrow. A path mismatch is treated as a move only when
the new root **contains the database file this pipeline is connected to**, which works because
ADR-0003 makes that file the system of record and ``.drillintel`` lives inside the workspace folder,
so a genuine move carries it. Everything else is refused.

Each rejection test asserts the database state as well as the error, because "it raised" is not the
same as "it changed nothing" - a rule that mutates and *then* validates is worse than no rule, since
it leaves the registry describing a folder the workspace never lived in.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.generate import build_corpus

from drilling_intelligence.config.settings import Settings
from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.database.models import (
    Document,
    IngestionRun,
)
from drilling_intelligence.database.models import (
    Workspace as WorkspaceRow,
)
from drilling_intelligence.ingestion.pipeline import IngestionPipeline
from drilling_intelligence.wells.repository import WellRepository
from drilling_intelligence.wells.workspace import Workspace

CONFIG = (
    '[app]\ndata_dir = ".drillintel"\n\n[ai]\nenabled = false\nrequire_ai = false\n\n'
    '[mineru]\nmode = "disabled"\n'
)


def _workspace(base: Path, name: str = "Alpha") -> Workspace:
    base.mkdir(parents=True, exist_ok=True)
    config = base / "workspace.toml"
    config.write_text(CONFIG, encoding="utf-8")
    workspace = Workspace.create(base / "w", Settings.load(config), name=name)
    with workspace.database.session() as session:
        WellRepository(session).get_or_create_workspace(str(workspace.root), name=name)
        session.commit()
    corpus = workspace.root / "corpus"
    build_corpus(corpus)
    return workspace


def _pipeline(workspace: Workspace, root: Path | str | None = None) -> IngestionPipeline:
    return IngestionPipeline(
        settings=workspace.settings,
        workspace_root=root if root is not None else workspace.root,
        database=workspace.database,
    )


def _state(database) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """Everything a relocation could disturb: registry rows, documents, ingestion runs."""
    with database.read_only() as session:
        rows = [
            (str(r.id), str(r.root_path))
            for r in session.execute(select(WorkspaceRow).order_by(WorkspaceRow.id)).scalars()
        ]
        docs = [
            (str(d.id), str(d.workspace_id))
            for d in session.execute(select(Document).order_by(Document.id)).scalars()
        ]
        runs = [str(r.id) for r in session.execute(select(IngestionRun)).scalars()]
    return rows, docs, runs


# ---------------------------------------------------------------- Attack A: unrelated folder


def test_an_unrelated_folder_cannot_claim_the_workspace(tmp_path) -> None:
    """The attack this rule exists to stop, asserted on state rather than on the exception."""
    workspace = _workspace(tmp_path / "home")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    before = _state(workspace.database)

    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(workspace, root=unrelated).run(root=workspace.root / "corpus")

    assert _state(workspace.database) == before, "a rejected relocation still mutated state"
    assert before[0][0][1] == str(workspace.root), "root_path was rewritten"


def test_an_unrelated_folder_is_refused_even_with_the_correct_workspace_id(tmp_path) -> None:
    """A correct id does not launder a wrong folder: the id is checked, the root is not trusted."""
    workspace = _workspace(tmp_path / "home")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    workspace_id = _state(workspace.database)[0][0][0]
    before = _state(workspace.database)

    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(workspace, root=unrelated).run(
            root=workspace.root / "corpus", workspace_id=workspace_id
        )

    assert _state(workspace.database) == before


# ------------------------------------------------------- Attack E: renamed folder, database left


def test_a_renamed_folder_without_the_database_is_refused(tmp_path) -> None:
    """Renaming the folder but not carrying ``.drillintel`` is not a move the system can honour.

    The database is the system of record, so a folder that does not contain it is not this
    workspace's home - it is an empty directory with a familiar name.  Refusing is the deterministic
    answer; guessing would either relocate the registry to a folder with no data in it, or invent a
    second workspace inside a database that already has one.
    """
    workspace = _workspace(tmp_path / "home")
    renamed = tmp_path / "renamed"
    shutil.copytree(str(workspace.root), str(renamed), ignore=shutil.ignore_patterns(".drillintel"))
    assert not (renamed / ".drillintel").exists()
    before = _state(workspace.database)

    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(workspace, root=renamed).run(root=workspace.root / "corpus")

    assert _state(workspace.database) == before


# ------------------------------------------- Attack D: a move, and the stale connection beside it


def test_a_stale_connection_after_a_move_is_refused_and_recreates_nothing(tmp_path) -> None:
    """Moving the folder out from under a live connection must not silently make a new database.

    After the move the connection still names the old path.  SQLite would happily create a fresh
    empty file there on the next write, so the dangerous outcome is not a wrong row - it is a second
    database appearing where the first one used to be.  The refusal is what prevents it.
    """
    workspace = _workspace(tmp_path / "home")
    old_database = str(workspace.database_path)
    assert Path(old_database).exists()
    before = _state(workspace.database)

    moved = tmp_path / "moved"
    shutil.move(str(workspace.root), str(moved))
    assert not Path(old_database).exists(), "the database did not travel with the folder"

    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(workspace, root=moved).run(root=moved / "corpus")

    assert not Path(old_database).exists(), "a rejected relocation recreated the old database file"
    assert _state(workspace.database) == before


def test_moving_the_folder_and_reopening_preserves_identity(tmp_path) -> None:
    """The supported move: carry the folder, open it at the new path, keep everything else."""
    workspace = _workspace(tmp_path / "home")
    identity = _state(workspace.database)[0][0][0]
    corpus = workspace.root / "corpus"
    assert _pipeline(workspace).run(root=corpus).ok
    with workspace.database.read_only() as session:
        documents = {
            str(d.id): (str(d.workspace_id), str(d.identity_path))
            for d in session.execute(select(Document)).scalars()
        }
    settings = workspace.settings
    workspace.close()

    moved = tmp_path / "moved"
    shutil.move(str(tmp_path / "home" / "w"), str(moved))
    assert (moved / ".drillintel").exists(), "the move did not carry the database"

    reopened = Workspace.open(moved, settings)
    try:
        assert _pipeline(reopened).run(root=moved / "corpus").ok
        rows = _state(reopened.database)[0]
        assert len(rows) == 1, f"the move created a second workspace row: {rows}"
        assert rows[0][0] == identity, "the move changed the workspace identity"
        assert rows[0][1] == str(moved.resolve()), "root_path was not refreshed to the new home"
        with reopened.database.read_only() as session:
            after = {
                str(d.id): (str(d.workspace_id), str(d.identity_path))
                for d in session.execute(select(Document)).scalars()
            }
        assert after == documents, "the move disturbed document identity or ownership"
    finally:
        reopened.close()


# ------------------------------------------------- Attack F: filesystem aliases and spellings


def test_a_symlinked_root_resolves_to_the_same_workspace(tmp_path) -> None:
    """``resolve()`` is the canonicalization, so an alias is the same folder, not a second one."""
    workspace = _workspace(tmp_path / "home")
    identity = _state(workspace.database)[0][0][0]
    link = tmp_path / "link"
    try:
        link.symlink_to(workspace.root)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlink support
        pytest.skip("this platform does not support symlinks")

    assert _pipeline(workspace, root=link).run(root=workspace.root / "corpus").ok
    rows = _state(workspace.database)[0]
    assert [r[0] for r in rows] == [identity], "a symlink registered a second workspace row"
    assert rows[0][1] == str(workspace.root), "the stored path was replaced by the alias"


@pytest.mark.parametrize(
    "spelling",
    ["trailing-slash", "dot-segment", "unnormalised"],
)
def test_path_spelling_variants_do_not_duplicate_the_workspace(tmp_path, spelling) -> None:
    """The same folder spelled differently must resolve to the row that already owns it."""
    workspace = _workspace(tmp_path / "home")
    identity = _state(workspace.database)[0][0][0]
    root = str(workspace.root)
    spelled = {
        "trailing-slash": root + "/",
        "dot-segment": root + "/./",
        "unnormalised": root + "/corpus/..",
    }[spelling]

    assert _pipeline(workspace, root=spelled).run(root=workspace.root / "corpus").ok
    rows = _state(workspace.database)[0]
    assert [r[0] for r in rows] == [identity], f"{spelling} created a second workspace row"
    assert rows[0][1] == root, f"{spelling} rewrote the canonical stored path"


# ----------------------------------------------------------------------- Section 6: atomicity


def test_a_refused_relocation_leaves_no_trace_anywhere(tmp_path) -> None:
    """Inspect, prove, validate, mutate - never mutate and then discover the evidence was thin.

    Asserts every surface a partial relocation could touch: the registry row's path and identity,
    the documents and their ownership, and the ingestion-run audit trail.  A run row claiming
    success for an ingest that was refused would be its own kind of corruption.
    """
    workspace = _workspace(tmp_path / "home")
    assert _pipeline(workspace).run(root=workspace.root / "corpus").ok
    before = _state(workspace.database)
    assert before[1], "the setup ingested nothing, so there is nothing to protect"
    assert before[2], "the setup recorded no ingestion run"

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    with pytest.raises(ValidationError, match="does not contain the database"):
        _pipeline(workspace, root=unrelated).run(root=unrelated)

    rows, docs, runs = _state(workspace.database)
    assert rows == before[0], "registry rows changed"
    assert docs == before[1], "documents or their workspace ownership changed"
    assert runs == before[2], "a refused ingest still wrote an ingestion run"


# ------------------------------------------------------- scan root versus storage location


def test_the_scan_root_does_not_change_workspace_identity(tmp_path) -> None:
    """``run(root=...)`` says where to read, never where to file.

    A pipeline attached to workspace A may ingest a corpus staged anywhere - that is a normal thing
    to want - and those documents belong to A.  What the scan root may not do is influence
    identity resolution, so a corpus from elsewhere cannot drag the workspace row with it.
    """
    workspace = _workspace(tmp_path / "home")
    elsewhere = tmp_path / "staging"
    elsewhere.mkdir()
    build_corpus(elsewhere)
    identity = _state(workspace.database)[0][0][0]

    assert _pipeline(workspace).run(root=elsewhere).ok
    rows, docs, _runs = _state(workspace.database)
    assert [r[0] for r in rows] == [identity], "scanning elsewhere moved the workspace"
    assert rows[0][1] == str(workspace.root), "the stored root followed the scan root"
    assert docs and {w for _d, w in docs} == {identity}, "documents were filed elsewhere"
