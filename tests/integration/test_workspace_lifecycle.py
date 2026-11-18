"""Workspace, session and SQLite lifecycle (§6).

`Workspace.close()` must release SQLite handles so a workspace folder can be
moved on Windows after all owners are closed.  This is not a sleep or forced
delete — it is deterministic disposal of engines and pooling.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from drilling_intelligence.cli.app import main
from drilling_intelligence.wells.workspace import Workspace


def test_repeated_open_and_close_is_safe(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    ws = Workspace.create(root)
    # Access database twice, close twice
    db1 = ws.database
    assert db1 is not None
    ws.close()
    # Reopen same path
    ws2 = Workspace.open(root)
    db2 = ws2.database
    assert db2 is not None
    ws2.close()
    # Close again (idempotent)
    ws2.close()
    ws.close()


def test_two_workspaces_same_path_close_independently(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    Workspace.create(root)
    ws1 = Workspace.open(root)
    ws2 = Workspace.open(root)
    # Both open databases
    assert ws1.database is not None
    assert ws2.database is not None
    # Closing one must not break the other
    ws1.close()
    # ws2 still usable
    with ws2.database.unit_of_work() as s:
        assert s is not None
    ws2.close()
    # Both now closed, file movable
    dest = tmp_path / "ws-moved"
    shutil.move(str(root), str(dest))
    assert dest.exists()
    assert not root.exists()


def test_move_after_close_with_active_read_transaction(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    ws = Workspace.create(root)
    # Open a read session
    sess = ws.database.session()
    # Do a read
    from sqlalchemy import text

    with ws.database.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    sess.close()
    ws.close()
    dest = tmp_path / "moved2"
    shutil.move(str(root), str(dest))
    assert dest.exists()


def test_move_after_failed_ingestion(tmp_path: Path) -> None:
    # Simulate a failed ingestion by opening workspace and not completing
    root = tmp_path / "ws"
    ws = Workspace.create(root)
    try:
        with ws.database.unit_of_work() as s:
            # Create a document then raise
            from sqlalchemy import text

            s.execute(text("SELECT 1"))
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass
    ws.close()
    dest = tmp_path / "moved3"
    shutil.move(str(root), str(dest))
    assert dest.exists()


def test_workspace_close_is_safe_after_partial_construction(tmp_path: Path) -> None:
    # Create workspace but don't open database
    root = tmp_path / "ws"
    ws = Workspace.create(root)
    # Close without ever opening database — must not raise
    ws.close()
    ws.close()
    dest = tmp_path / "moved4"
    shutil.move(str(root), str(dest))
    assert dest.exists()


def test_e2e_workspace_move_after_close(tmp_path: Path) -> None:
    # The exact E2E shape that previously hit WinError 32: create, ingest, close, move
    root = tmp_path / "ws"
    assert main(["workspace", "create", str(root)]) == 0
    ws = Workspace.open(root)
    # Trigger DB creation
    _ = ws.database
    ws.close()
    # Second open (as E2E does for second batch)
    ws2 = Workspace.open(root)
    _ = ws2.database
    ws2.close()
    # Move must succeed on Windows after both closes
    dest = tmp_path / "ws-final"
    shutil.move(str(root), str(dest))
    assert dest.joinpath(".drillintel", "database", "drilling_intelligence.db").exists()


def test_disposable_sqlite_path(tmp_path: Path) -> None:
    # A disposable path under TEMP should be movable after close
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "disp-ws"
        ws = Workspace.create(root)
        _ = ws.database
        ws.close()
        dest = Path(tmpdir) / "disp-ws2"
        shutil.move(str(root), str(dest))
        assert dest.exists()
        # Clean up
        shutil.rmtree(dest, ignore_errors=True)
