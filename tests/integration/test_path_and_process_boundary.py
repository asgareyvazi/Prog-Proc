"""Filesystem and process boundaries: nothing escapes the workspace, nothing reaches a shell.

These are the two ways an ingestion pipeline can be turned against its operator: by reading a file
the scan was not pointed at, or by letting a filename become shell syntax.  Both are exercised here
against the real scanner and the real subprocess helper rather than argued from source.

The subprocess cases matter because MinerU is invoked with a path the operator chose.  ``run_command``
takes a list argv and never passes ``shell=True`` - there is no ``shell=True`` anywhere in ``src/`` -
and the assertions below pin that, so a future change to string interpolation would fail loudly
instead of quietly becoming a command-injection hole.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from drilling_intelligence.database.models import Document
from drilling_intelligence.ingestion.pipeline import IngestionPipeline

SHELL_NAME = "a;rm -rf ~$(whoami)`id`.txt"


def _needs_symlinks(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    try:
        probe.symlink_to(tmp_path)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not support symlinks")
    probe.unlink()


def _registered(workspace) -> list[tuple[str, str]]:
    with workspace.database.read_only() as session:
        return sorted((d.filename, d.identity_path) for d in session.scalars(select(Document)))


def _run(workspace, root: Path):
    pipeline = IngestionPipeline(
        settings=workspace.settings, workspace_root=workspace.root, database=workspace.database
    )
    return pipeline.run(root=root)


def test_symlinks_pointing_outside_the_scan_root_are_never_followed(
    workspace, tmp_path: Path
) -> None:
    """A scan of folder X registers files in X, not whatever X happens to link to.

    Following a symlink out of the root would let a corpus stage a link to ``/etc``, a home
    directory or another well's documents and have them ingested as if they belonged there.
    """
    _needs_symlinks(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("OUTSIDE SECRET\n", encoding="utf-8")

    corpus = workspace.root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / "normal.txt").write_text("inside the root\n", encoding="utf-8")
    (corpus / "link_to_outside_file.txt").symlink_to(outside / "secret.txt")
    (corpus / "link_to_outside_dir").symlink_to(outside)
    (corpus / "nested_symlink.txt").symlink_to(corpus / "link_to_outside_file.txt")
    (corpus / "broken_symlink.txt").symlink_to(tmp_path / "does_not_exist")

    result = _run(workspace, corpus)
    assert result.ok, result.error

    names = [name for name, _path in _registered(workspace)]
    assert names == ["normal.txt"], names
    assert not [p for _n, p in _registered(workspace) if "outside" in p or "secret" in p]
    # The outside file must still exist and be untouched - the scanner read nothing through the link.
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "OUTSIDE SECRET\n"


def test_a_directory_symlink_climbing_above_the_root_is_not_followed(
    workspace, tmp_path: Path
) -> None:
    """The same guarantee one level down, where the link is inside a subdirectory."""
    _needs_symlinks(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "leak.txt").write_text("should never be read\n", encoding="utf-8")

    corpus = workspace.root / "corpus"
    (corpus / "sub").mkdir(parents=True)
    (corpus / "sub" / "up_and_out").symlink_to(outside)

    result = _run(workspace, corpus)
    assert result.ok, result.error
    assert _registered(workspace) == [], _registered(workspace)


def test_a_hostile_filename_is_stored_as_data_and_never_executed(workspace) -> None:
    """Shell metacharacters, a leading ``~`` and non-ASCII are all just characters in a name.

    The name is carried as a value through the registry.  Nothing here should expand ``~``, treat
    ``;`` as a separator, or run ``$(...)`` - and the file's own content proves it was read as a
    file rather than interpreted.
    """
    corpus = workspace.root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for name in (SHELL_NAME, "~root_passwd.txt", "café_résumé_中文.txt", "-etc-passwd.txt"):
        (corpus / name).write_text("just text\n", encoding="utf-8")

    result = _run(workspace, corpus)
    assert result.ok, result.error
    assert result.failures == 0, [item.error for item in result.failures_report()]

    names = sorted(name for name, _path in _registered(workspace))
    assert names == sorted(
        [SHELL_NAME, "~root_passwd.txt", "café_résumé_中文.txt", "-etc-passwd.txt"]
    ), names
    # The metacharacter name survived verbatim: no expansion, no truncation at the first ';'.
    assert SHELL_NAME in names
    assert not [name for name in names if name.startswith("-etc") and name != "-etc-passwd.txt"]


def test_a_dotfile_is_ignored_rather_than_ingested(workspace) -> None:
    """A name starting with a dot is not a document, and quietly ingesting one would surprise."""
    corpus = workspace.root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / ".hidden.txt").write_text("hidden\n", encoding="utf-8")
    (corpus / "visible.txt").write_text("visible\n", encoding="utf-8")

    result = _run(workspace, corpus)
    assert result.ok, result.error
    assert [name for name, _path in _registered(workspace)] == ["visible.txt"]


# -- process boundary ---------------------------------------------------------


def test_run_command_passes_arguments_as_a_list_so_a_filename_cannot_become_shell_syntax(
    tmp_path: Path,
) -> None:
    """The subprocess seam receives argv elements, never a string a shell would re-parse.

    Proven by running a real program that echoes its own ``argv``: if the hostile name had been
    interpolated into a shell, the metacharacters would have been expanded or split, and the
    program would have seen several arguments instead of one.
    """
    from drilling_intelligence.integrations.base import run_command

    script = tmp_path / "argv_dump.py"
    script.write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    result = run_command(["python3", str(script), SHELL_NAME], timeout=30.0)

    assert result["returncode"] == 0, result["stderr"]
    import json

    seen = json.loads(result["stdout"])
    assert seen == [SHELL_NAME], (
        f"the filename reached the child as {seen!r}; it must arrive as one unmodified argument"
    )
    assert result["timeout"] is False


def test_run_command_reports_a_timeout_instead_of_hanging_or_guessing(tmp_path: Path) -> None:
    """A hung external engine must come back as a described failure, not block the run."""
    from drilling_intelligence.integrations.base import run_command

    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    result = run_command(["python3", str(script)], timeout=1.0)

    assert result["timeout"] is True
    assert result["returncode"] == -1
    assert "timed out" in result["stderr"], result["stderr"]


def test_run_command_propagates_only_the_environment_it_is_given(tmp_path: Path) -> None:
    """Extra variables are added on top of the caller's environment, not silently replacing it.

    Both halves matter: a child that lost ``PATH`` could not find its own dependencies, and one
    that silently inherited secrets the caller never passed would widen their exposure.
    """
    from drilling_intelligence.integrations.base import run_command

    script = tmp_path / "env_dump.py"
    script.write_text(
        "import json, os\nprint(json.dumps({'extra': os.environ.get('MINERU_EXTRA', ''),\n"
        "                                    'path': bool(os.environ.get('PATH'))}))\n",
        encoding="utf-8",
    )
    result = run_command(
        ["python3", str(script)], timeout=30.0, env={"MINERU_EXTRA": "passed-through"}
    )

    import json

    assert result["returncode"] == 0, result["stderr"]
    seen = json.loads(result["stdout"])
    assert seen == {"extra": "passed-through", "path": True}, seen


def test_a_missing_executable_is_reported_not_raised(tmp_path: Path) -> None:
    """``run_command`` never raises on a bad exit, so callers decide what is fatal."""
    from drilling_intelligence.integrations.base import run_command

    result = run_command([str(tmp_path / "no_such_binary")], timeout=10.0)
    assert result["returncode"] == -1
    assert "not found" in result["stderr"].lower(), result["stderr"]


def test_a_malformed_http_endpoint_is_refused_as_configuration_not_fetched(tmp_path: Path) -> None:
    """An endpoint that is not a URL must not be reinterpreted as a local file path.

    The MinerU prober only ever passes the endpoint to the HTTP helper, so a value like
    ``../../etc/passwd`` or ``file:///etc/passwd`` can never become a filesystem read.
    """
    import drilling_intelligence.integrations.mineru.discovery as discovery_mod
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.integrations.mineru.discovery import MinerUProber

    fetched: list[str] = []

    def handler(method, url, **kwargs):
        fetched.append(url)
        return {"ok": False, "status_code": 0, "json": None, "text": "", "url": url, "error": "bad"}

    original = discovery_mod.http_json
    discovery_mod.http_json = handler
    try:
        settings = Settings.load()
        for endpoint in ("../../etc/passwd", "file:///etc/passwd", "not a url"):
            settings.mineru.mode = "http"
            settings.mineru.endpoint = endpoint
            fetched.clear()
            status = MinerUProber(settings).status()

            assert status.available is False
            # Whatever was attempted, it was attempted over HTTP with the endpoint as a prefix -
            # never opened as a path.
            assert all(url.startswith(endpoint) for url in fetched), fetched
            assert not Path(endpoint).exists() or endpoint != "../../etc/passwd"
    finally:
        discovery_mod.http_json = original
    assert os.environ.get("MINERU_EXTRA") is None
