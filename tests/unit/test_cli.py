"""The ``drillintel`` CLI, and the packaging metadata that has to stay honest.

Two things are proven here, and they belong together: the CLI is the smoke test for the whole
core (real workspace, real files, real database, real index), and
``pyproject.toml`` is the promise that makes that runnable from an installed package.  A test
that reads the metadata and checks it against the tree is what stops the two from drifting
apart again - which is exactly the bug this file exists to prevent.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from tests.fixtures.generate import build_corpus

from drilling_intelligence import __version__
from drilling_intelligence.cli import main
from drilling_intelligence.cli.app import build_parser

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

#: Dependency name in ``pyproject.toml`` -> module the installed package imports.
IMPORT_NAMES = {
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "pymupdf": "pymupdf",
    "openpyxl": "openpyxl",
    "python-docx": "docx",
    "httpx": "httpx",
    "pandas": "pandas",
    "pillow": "PIL",
    "pydantic": "pydantic",
    "numpy": "numpy",
    "pdfplumber": "pdfplumber",
    "docling": "docling",
    "wellpathpy": "wellpathpy",
}


def _pyproject() -> dict:
    tomllib = pytest.importorskip("tomllib")
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture
def live_workspace(tmp_path: Path):
    """A real workspace with a real corpus ingested and indexed - the whole point of the CLI."""
    from drilling_intelligence.config.settings import Settings
    from drilling_intelligence.wells.workspace import Workspace

    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[app]",
                'data_dir = ".drillintel"',
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
    settings = Settings.load(config)
    workspace_root = tmp_path / "project"
    assert (
        run(
            "workspace",
            "create",
            str(workspace_root),
            "--config",
            str(config),
            "--name",
            "North Cormorant",
            "--json",
        )[0]
        == 0
    )
    corpus = workspace_root / "corpus"
    build_corpus(corpus)
    workspace = Workspace.open(workspace_root, settings)
    try:
        with workspace.database.session() as session:
            from drilling_intelligence.wells.repository import WellRepository

            repo = WellRepository(session)
            repo.get_or_create_workspace(str(workspace_root), name="North Cormorant")
            project = repo.get_or_create_project("North Cormorant")
            repo.create_well("A-3", project_id=project.id)
            session.commit()
    finally:
        workspace.close()
    return workspace_root, config, settings


def add_conflicting_mud_report(corpus_dir: Path) -> Path:
    """A second mud report for A-3, reading 10.6 ppg where the first says 10.2.

    Built with the same builder the generated corpus uses, then edited in place: the disagreement has
    to be a pair of real files rather than a hand-made database row, or the test exercises storage and
    skips the detection that is the whole subject.
    """
    from openpyxl import load_workbook
    from tests.fixtures.generate import build_mud_report_xlsx

    second = corpus_dir / "mud_report_check_well-a3.xlsx"
    build_mud_report_xlsx(second)
    book = load_workbook(second)
    book["Summary"]["B9"] = 10.6
    book.save(second)
    return second


def run(*argv: str) -> tuple[int, str, str]:
    """Run one CLI command and capture what it printed, so assertions are about output."""
    from io import StringIO

    out, err = StringIO(), StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(list(argv))
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


def payload(stdout: str) -> dict:
    """The one JSON document ``--json`` promised.

    Parsing the whole capture is the check: a stray library print before or after the payload
    makes ``json.loads`` fail, which is exactly the bug ``--json`` must not have.
    """
    assert stdout.startswith("{"), stdout[:200]
    return json.loads(stdout)


class TestPackagingMetadataIsTrue:
    def test_every_declared_script_target_imports_and_exposes_its_attribute(self) -> None:
        scripts = _pyproject()["project"].get("scripts", {})
        assert scripts, "a package with no entry points should not declare any"
        for name, target in scripts.items():
            module_name, _, attribute = target.partition(":")
            module = __import__(module_name, fromlist=[attribute or "main"])
            assert hasattr(module, attribute), (
                f"{name} points at {target}, which has no {attribute!r}"
            )

    def test_no_entry_point_names_a_module_that_does_not_exist(self) -> None:
        """The bug this guards: a `drillintel-ui` script for a UI package that was never built."""
        declared = set()
        for table in ("scripts", "gui-scripts"):
            declared.update(_pyproject()["project"].get(table, {}).values())
        for target in declared:
            module_name = target.partition(":")[0]
            relative = module_name.replace(".", "/")
            exists = (SRC / f"{relative}.py").exists() or (SRC / relative / "__init__.py").exists()
            assert exists, f"{target!r} names {module_name}, which is not in the package"

    def test_an_empty_gui_scripts_table_is_not_shipped(self) -> None:
        table = _pyproject()["project"].get("gui-scripts")
        assert not table, "an empty [project.gui-scripts] is noise; a populated one must be real"

    def test_declared_package_data_files_exist(self) -> None:
        package_data = _pyproject()["tool"]["setuptools"].get("package-data", {})
        for package, patterns in package_data.items():
            directory = SRC / package.replace(".", "/")
            assert directory.is_dir(), f"package-data names {package}, which is not a directory"
            for pattern in patterns:
                matches = list(directory.glob(pattern))
                assert matches, f"{package} declares package data {pattern!r}, which does not exist"
        assert (SRC / "drilling_intelligence" / "py.typed").is_file()
        assert _pyproject()["tool"]["setuptools"].get("include-package-data") is True, (
            "package data is dropped without this"
        )

    def test_every_runtime_dependency_is_actually_imported_by_the_package(self) -> None:
        """A dependency nobody imports is an install cost and an attack surface, not a feature."""
        declared = []
        for item in _pyproject()["project"]["dependencies"]:
            name = re.split(r"[<>=!\[]", item, maxsplit=1)[0].strip().lower()
            declared.append(name)
        assert declared, "the package must state its dependencies"
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SRC / "drilling_intelligence").rglob("*.py")
        )
        for name in declared:
            module = IMPORT_NAMES.get(name, name.replace("-", "_"))
            assert re.search(rf"(^|\n)\s*(import|from)\s+{re.escape(module)}\b", source), (
                f"{name} is declared but never imported by src/"
            )

    def test_optional_extras_do_not_leak_into_the_install_requires(self) -> None:
        extras = _pyproject()["project"].get("optional-dependencies", {})
        assert set(extras) <= {"ui", "vec", "dev"}, (
            "an extra is a promise about an *optional* capability"
        )
        declared = {
            re.split(r"[<>=!\[]", item, maxsplit=1)[0].strip().lower()
            for item in _pyproject()["project"]["dependencies"]
        }
        for name, packages in extras.items():
            if name == "dev":
                continue
            for package in packages:
                key = re.split(r"[<>=!\[]", package, maxsplit=1)[0].strip().lower()
                assert key not in declared, f"{package!r} is in [{name}] *and* in dependencies"

    def test_declared_python_floor_matches_the_code_and_the_linter_target(self) -> None:
        data = _pyproject()
        floor = data["project"]["requires-python"]
        assert floor.startswith(">=3.11"), floor
        assert data["tool"]["ruff"]["target-version"] == "py311"

    def test_documented_markers_are_registered(self) -> None:
        markers = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
        names = {item.split(":", 1)[0].strip() for item in markers}
        assert {"ui", "network", "mineru", "engineering"} <= names
        assert "--strict-markers" in _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]


class TestCliParsers:
    def test_every_subcommand_parses(self) -> None:
        parser = build_parser()
        for argv in (
            ["version"],
            ["workspace", "create", "new-project"],
            ["ingest", "corpus", "--well", "A-3", "--force", "--limit", "5"],
            ["search", "mud weight 10.2 ppg", "--type", "MUD_REPORT", "--limit", "20"],
            ["search", "q", "--verify", "--include-superseded", "--rebuild"],
            ["index", "status"],
            ["index", "rebuild"],
            ["index", "prune"],
            ["doctor"],
        ):
            assert parser.parse_args(argv).command, argv

    def test_no_subcommand_prints_help_and_exits_two(self) -> None:
        code, _out, _err = run("--json")
        assert code == 2


class TestCliEndToEnd:
    def test_ingest_reports_the_run_and_a_second_pass_is_idempotent(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        code, out, err = run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--json",
        )
        assert code == 0, err
        first = payload(out)
        assert first["counts"]["NEW"] == 6
        assert first["failures"] == 0
        assert first["indexed"] == 6 and first["indexed_chunks"] > 100
        assert first["index_stats"]["documents"] == 6

        code, out, err = run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--json",
        )
        assert code == 0, err
        second = payload(out)
        assert second["counts"]["UNCHANGED"] == 6
        assert second["counts"]["TO_PROCESS"] == 0
        assert second["indexed"] == 0

    def test_search_prints_a_cited_answer_with_the_location_that_matters(
        self, live_workspace
    ) -> None:
        workspace_root, config, _settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        code, out, err = run(
            "search",
            "mud weight 10.2 ppg",
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--type",
            "MUD_REPORT",
            "--limit",
            "5",
            "--json",
        )
        assert code == 0, err
        result = payload(out)
        assert result["count"] >= 1
        hit = result["results"][0]
        assert hit["document_id"] and hit["version_id"]
        assert hit["score"] > 0
        assert "10.2" in hit["snippet"]
        assert hit["sheet"] == "Summary"
        assert hit["locator_ref"].startswith("Sheet: Summary > ")
        assert hit["provenance"]["locator"]["kind"] == "excel"
        assert hit["provenance"]["excerpt"]
        assert hit["metadata"]["document_type"] == "MUD_REPORT"
        assert hit["cited"] is True

    def test_search_text_output_includes_the_citation_line(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        code, out, err = run(
            "search",
            "casing shoe test",
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--type",
            "DRILLING_PROGRAM",
        )
        assert code == 0, err
        assert "result(s) for" in out
        assert "Page 1" in out
        assert "well_a3_program_rev12.pdf" in out

    def test_verify_reports_the_source_check_per_hit(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        code, out, err = run(
            "search",
            "mud weight 10.2 ppg",
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--verify",
            "--json",
        )
        assert code == 0, err
        results = payload(out)["results"]
        cited = [item for item in results if item["cited"]]
        assert cited
        for item in cited:
            assert item["verification"]["status"] == "MATCH", item["verification"]
            assert Path(item["verification"]["source"]).name == item["metadata"]["filename"]

    def test_an_unknown_well_is_an_error_that_names_the_alternatives(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        code, _out, err = run(
            "search",
            "mud",
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--well",
            "B-99",
        )
        assert code == 1
        assert "no well matches" in err
        assert "A-3" in err

    def test_a_missing_folder_is_reported_rather_than_traced_back(
        self, live_workspace, tmp_path: Path
    ) -> None:
        _workspace_root, config, _settings = live_workspace
        code, _out, err = run(
            "ingest",
            str(tmp_path / "nowhere"),
            "--workspace",
            str(tmp_path),
            "--config",
            str(config),
        )
        assert code == 1
        assert "does not exist" in err or "not a Drilling Intelligence workspace" in err
        assert "Traceback" not in err

    def test_debug_surfaces_an_unexpected_error_instead_of_a_summary(
        self, live_workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A domain error is printed; a bug is shown.  ``--debug`` is the difference.

        Silently turning an unexpected exception into "error: RuntimeError: boom" is fine for a
        user and useless for a developer, so the flag must reach the original traceback - and
        without it the CLI must still not print one.
        """
        from drilling_intelligence.cli import app as cli_app

        def boom(*_args, **_kwargs):
            raise RuntimeError("unexpected boom")

        monkeypatch.setattr(cli_app.SearchService, "for_workspace", staticmethod(boom))
        workspace_root, config, _settings = live_workspace
        code, _out, err = run(
            "search", "mud", "--workspace", str(workspace_root), "--config", str(config)
        )
        assert code == 1
        assert "unexpected boom" in err and "Traceback" not in err
        with pytest.raises(RuntimeError, match="unexpected boom"):
            main(
                [
                    "search",
                    "mud",
                    "--workspace",
                    str(workspace_root),
                    "--config",
                    str(config),
                    "--debug",
                ]
            )

    def test_no_message_prompts_for_a_command_the_cli_does_not_have(self) -> None:
        """Every ``drillintel <word>`` quoted in the package must be a command the parser accepts.

        The packaging metadata lied about an entry point for the same reason an error hint can
        lie about a subcommand: the text is written before the code exists, and nothing checks it
        afterwards.  The check here is behavioural - argparse exits 0 for a real command's
        ``--help`` and 2 for an unknown one - so it cannot drift from the parser.
        """
        import contextlib
        import io
        import re

        sources = list((ROOT / "src").rglob("*.py"))
        quoted: dict[str, list[str]] = {}
        for path in sources:
            for word in re.findall(
                r"drillintel ([a-z][a-z0-9-]*)", path.read_text(encoding="utf-8")
            ):
                quoted.setdefault(word, []).append(path.relative_to(ROOT).as_posix())
        assert quoted, (
            "the CLI is referenced from error messages and docstrings; a scan finding nothing means the scan is wrong"
        )

        def exists(command: str) -> bool:
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    main([command, "--help"])
            except SystemExit as exc:  # argparse's documented way of ending a --help
                return exc.code == 0
            except Exception:  # noqa: BLE001 - any other failure is not "this command exists"
                return False
            return True

        invented = {word: where for word, where in quoted.items() if not exists(word)}
        assert not invented, f"messages advertise commands the CLI does not implement: {invented}"

    def test_ingest_scopes_documents_to_one_workspace_row(self, live_workspace) -> None:
        """The registry ``workspace`` row is the identity scope, used from the very first run.

        Without it the first ingest registers everything under ``workspace_id IS NULL``, and the
        moment a well appears a second, disconnected set of rows covers the same files - so
        per-workspace queries and removal detection quietly see half the folder.
        """
        from sqlalchemy import select

        from drilling_intelligence.database.models import Document
        from drilling_intelligence.database.models import Workspace as WorkspaceRow
        from drilling_intelligence.wells.workspace import Workspace

        workspace_root, config, settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        opened = Workspace.open(workspace_root, settings)
        try:
            with opened.database.session() as session:
                documents = session.execute(
                    select(Document.workspace_id, Document.identity_path)
                ).all()
                rows = session.execute(
                    select(
                        WorkspaceRow.root_path,
                        WorkspaceRow.id,
                        WorkspaceRow.name,
                        WorkspaceRow.data_dir,
                    )
                ).all()
        finally:
            opened.close()

        assert len(rows) == 1, f"one ingest must not split the registry: {rows}"
        root, workspace_row_id, name, data_dir = rows[0]
        assert (
            Path(root) == workspace_root
            and name == "North Cormorant"
            and data_dir.endswith(".drillintel")
        )
        assert documents, "ingest must have registered documents"
        assert {document_workspace for document_workspace, _path in documents} == {workspace_row_id}

    def test_index_status_and_rebuild_agree_with_the_registry(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        code, out, err = run(
            "index", "status", "--workspace", str(workspace_root), "--config", str(config), "--json"
        )
        assert code == 0, err
        stats = payload(out)["stats"]
        assert stats["documents"] == 6 and stats["chunks"] > 100
        assert stats["missing_versions"] == 0

        code, out, err = run(
            "index",
            "rebuild",
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--json",
        )
        assert code == 0, err
        assert payload(out)["stats"]["chunks"] == stats["chunks"], (
            "a rebuild must reproduce the same size"
        )

        code, out, err = run(
            "index", "prune", "--workspace", str(workspace_root), "--config", str(config), "--json"
        )
        assert code == 0, err
        assert payload(out)["versions_removed"] == 0

    def test_doctor_passes_on_a_healthy_workspace(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        code, out, err = run(
            "doctor", "--workspace", str(workspace_root), "--config", str(config), "--json"
        )
        assert code == 0, (out, err)
        report = payload(out)
        assert report["registry"]["documents"] == 6
        assert report["integrity_problems"] == []
        assert report["findings"] == []
        assert report["version"] == __version__ == "0.0.1a0", (
            "the CLI must report the packaged version"
        )

    def test_doctor_reports_a_search_index_that_is_behind_the_registry(
        self, live_workspace
    ) -> None:
        from drilling_intelligence.wells.workspace import Workspace

        workspace_root, config, settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        # Empty the index out from under the service: what a crash, or a deleted sidecar file,
        # actually leaves behind.
        opened = Workspace.open(workspace_root, settings)
        try:
            opened.search_service().index.clear()
        finally:
            opened.close()
        code, out, _err = run(
            "doctor", "--workspace", str(workspace_root), "--config", str(config), "--json"
        )
        assert code == 1
        report = payload(out)
        assert report["index"]["chunks"] == 0
        assert report["index"]["missing_versions"] == 6
        assert any("index rebuild" in item for item in report["findings"])

    def test_a_superseded_version_stops_answering_through_the_cli(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
        )
        target = workspace_root / "corpus" / "lesson_learned_ll-2025-014.txt"
        target.write_text(
            target.read_text(encoding="utf-8") + "\nFollow-up: the crew re-primed the pump.\n",
            encoding="utf-8",
        )
        code, out, err = run(
            "ingest",
            str(workspace_root / "corpus"),
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--json",
        )
        assert code == 0, err
        assert payload(out)["counts"]["MODIFIED"] == 1
        code, out, _err = run(
            "search",
            "re-primed",
            "--workspace",
            str(workspace_root),
            "--config",
            str(config),
            "--json",
        )
        results = payload(out)["results"]
        assert [item["version_number"] for item in results] == [2]
        assert results[0]["locator_ref"].startswith("Lines ")


class TestKnowledgeCommands:
    """What the corpus asserts, and how a disagreement is settled - through the terminal.

    These are the only tests that exercise the knowledge layer the way a engineer would use it:
    one command, one printed answer.  The numbers asserted here are the numbers the generated
    corpus really produces, which is what makes a regression visible: if the counts move, either
    the extraction changed or the corpus did, and either is worth a failing test.
    """

    def _run(self, workspace_root: Path, config: Path, *argv: str) -> tuple[int, str, str]:
        return run(*argv, "--workspace", str(workspace_root), "--config", str(config))

    def _ingest(self, workspace_root: Path, config: Path) -> dict:
        code, out, err = self._run(
            workspace_root, config, "ingest", str(workspace_root / "corpus"), "--json"
        )
        assert code == 0, err
        return payload(out)

    def test_status_counts_the_facts_the_corpus_asserted(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        self._ingest(workspace_root, config)
        code, out, err = self._run(workspace_root, config, "knowledge", "status", "--json")
        assert code == 0, err
        report = payload(out)
        assert report["facts"] == 64
        assert report["by_origin"] == {"EXTRACTED": 64}, "nothing was typed by a hand here"
        assert report["by_status"] == {"ACTIVE": 55, "UNVERIFIED": 9}
        assert report["by_value_type"] == {"quantity": 45, "date": 9, "text": 6, "ratio": 4}
        assert report["by_entity_type"]["well"] == 18
        assert report["relations"] == 64
        assert report["open_conflicts"] == 0
        assert report["versions_with_artefacts"] == 6
        assert report["versions_without_knowledge"] == 0, (
            "ingest derived knowledge for every stored artefact"
        )
        assert report["detached_facts"] == 0
        assert report["index"]["knowledge_chunks"] == 64, "facts are searchable, not only listable"
        assert report["needs_rebuild"] is False
        _code, text, _err = self._run(workspace_root, config, "knowledge", "status")
        assert "knowledge items: 64" in text
        assert "registry vs knowledge: 0 of 6 current version(s) have no facts" in text
        assert "rebuild recommended: no" in text

    def test_facts_carry_their_provenance_and_narrow_on_demand(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        self._ingest(workspace_root, config)
        code, out, err = self._run(
            workspace_root, config, "knowledge", "facts", "--well", "A-3", "--json"
        )
        assert code == 0, err
        report = payload(out)
        assert report["scope"] == "well A-3"
        assert report["count"] == 18 == len(report["facts"])
        for entry in report["facts"]:
            assert entry["citation"], (
                f"a fact printed without a source is a fact nobody can check: {entry}"
            )
            assert entry["provenance"], (
                "the citation is rendered from provenance, which must be present"
            )
            assert entry["origin"] == "EXTRACTED"
            assert entry["item_id"].startswith("ki-")
        mud = [entry for entry in report["facts"] if entry["predicate"] == "mud_weight"]
        assert [entry["original_value"] for entry in mud] == ["10.2"]
        assert mud[0]["citation"].endswith("Cell: B9")
        code, out, err = self._run(
            workspace_root,
            config,
            "knowledge",
            "facts",
            "--well",
            "A-3",
            "--predicate",
            "mud_weight",
        )
        assert code == 0, err
        assert "1 fact(s) for well A-3" in out
        assert "mud_weight = 10.2 ppg" in out, out
        assert "source: mud_report_well-a3.xlsx > Sheet: Summary > Cell: B9" in out

    def test_a_well_that_does_not_exist_is_an_error_even_in_json(self, live_workspace) -> None:
        """``--json`` is a promise about the *stream*, not a promise that nothing went wrong.

        A script that asks for machine-readable output must be able to read the failure too; an
        uncaught traceback on stdout would break every pipeline that trusted the flag.
        """
        workspace_root, config, _settings = live_workspace
        self._ingest(workspace_root, config)
        code, out, err = self._run(
            workspace_root, config, "knowledge", "facts", "--well", "B-99", "--json"
        )
        assert code == 1
        report = payload(out)
        assert report["ok"] is False
        assert "no well matches 'B-99'" in report["message"]
        assert report["code"] == "KNOWLEDGE"
        assert "Traceback" not in out + err
        code, _out, err = self._run(workspace_root, config, "knowledge", "facts", "--well", "B-99")
        assert code == 1
        assert "no well matches 'B-99'" in err

    def test_a_stale_knowledge_layer_asks_for_a_rebuild_and_rebuild_delivers_it(
        self, live_workspace
    ) -> None:
        """The recovery path has to be as trustworthy as the happy path.

        The knowledge rows are derived, so losing them is not a data loss - but the workspace must
        say so instead of answering from an empty registry, and ``rebuild`` must reproduce exactly
        what ingestion had derived from the stored artefacts.
        """
        workspace_root, config, settings = live_workspace
        self._ingest(workspace_root, config)
        before = payload(self._run(workspace_root, config, "knowledge", "status", "--json")[1])
        from sqlalchemy import text as sql

        from drilling_intelligence.wells.workspace import Workspace

        workspace = Workspace.open(workspace_root, settings)
        try:
            with workspace.database.session() as session:
                session.execute(sql("DELETE FROM knowledge_relation"))
                session.execute(sql("DELETE FROM knowledge_item"))
                session.commit()
        finally:
            workspace.close()
        code, out, _err = self._run(workspace_root, config, "knowledge", "status", "--json")
        assert code == 1, "a workspace that knows nothing must not exit as if all were well"
        stale = payload(out)
        assert stale["facts"] == 0
        assert stale["versions_without_knowledge"] == stale["versions_with_artefacts"] == 6
        assert stale["needs_rebuild"] is True
        code, out, err = self._run(workspace_root, config, "knowledge", "rebuild", "--json")
        assert code == 0, err
        rebuilt = payload(out)
        assert rebuilt["removed"] == 0, "there was nothing derived left to drop"
        # The tally counts *writes*, and two documents state some facts identically, so the second
        # write of a key updates the row instead of duplicating it - which is the point of the lookup
        # key, and the reason a rebuild cannot double the corpus.
        assert rebuilt["facts"]["created"] == 64, rebuilt["facts"]
        assert rebuilt["facts"]["created"] + rebuilt["facts"]["updated"] == 66
        assert rebuilt["facts"]["unchanged"] == 0, (
            "nothing was skipped: the rows had all been removed"
        )
        assert rebuilt["conflicts"]["conflicts"] == 0
        after = payload(self._run(workspace_root, config, "knowledge", "status", "--json")[1])
        for key in (
            "facts",
            "relations",
            "by_status",
            "by_value_type",
            "by_entity_type",
            "open_conflicts",
            "detached_facts",
            "versions_without_knowledge",
        ):
            assert after[key] == before[key], (
                f"rebuild did not reproduce what ingestion derived ({key})"
            )

    def test_two_sources_disagreeing_are_printed_as_an_argument_with_a_way_to_settle_it(
        self, live_workspace
    ) -> None:
        """The conflict surface, end to end: found, shown with both sides, then decided by a person."""
        workspace_root, config, _settings = live_workspace
        self._ingest(workspace_root, config)
        add_conflicting_mud_report(workspace_root / "corpus")
        code, out, err = self._run(
            workspace_root, config, "ingest", str(workspace_root / "corpus"), "--json"
        )
        assert code == 0, err
        assert payload(out)["counts"]["NEW"] == 1
        code, out, err = self._run(workspace_root, config, "knowledge", "conflicts", "--json")
        assert code == 0, err
        report = payload(out)
        assert report["count"] == 1, report
        conflict = report["conflicts"][0]
        assert conflict["property"] == "mud_weight"
        assert conflict["record_state"] == "ACTUAL"
        assert conflict["status"] == "OPEN"
        assert conflict["note"] == "2 different values stated by 2 sources"
        assert {entry["value"] for entry in conflict["candidates"]} == {10.2, 10.6}
        assert {entry["source"] for entry in conflict["candidates"]} == {
            "mud_report_well-a3.xlsx",
            "mud_report_check_well-a3.xlsx",
        }
        for entry in conflict["candidates"]:
            assert entry["locator_ref"].endswith("Cell: B9"), entry
            assert entry["provenance"]["filename"] == entry["source"], (
                "the printed citation and the recorded provenance must agree about the file"
            )
        assert conflict["well_id"], "a conflict must say which well it is about, not only its key"
        _code, text, _err = self._run(
            workspace_root, config, "knowledge", "conflicts", "--well", "A-3"
        )
        assert "1 conflict(s) with status OPEN" in text
        assert "settle it: drillintel knowledge resolve " in text, text
        assert "Cell: B9" in text, "both sides are printed with the place they were read from"

        # The 10.2 reading is the one chosen, whoever printed it first: both sources are mud reports
        # of the same date, so the platform has no basis to rank one over the other - which is the
        # point.  A person decides, so the test has to name the value it decides on.
        chosen = next(
            entry["item_id"]
            for entry in conflict["candidates"]
            if entry["value"] == pytest.approx(10.2)
        )
        other = next(
            entry["item_id"]
            for entry in conflict["candidates"]
            if entry["value"] != pytest.approx(10.2)
        )
        code, out, err = self._run(
            workspace_root,
            config,
            "knowledge",
            "resolve",
            conflict["id"],
            "--choose",
            chosen,
            "--note",
            "the check weight was read before the trip was cleaned up",
            "--by",
            "A. Gharib",
            "--json",
        )
        assert code == 0, err
        result = payload(out)
        assert result["status"] == "RESOLVED_MANUALLY"
        assert result["chosen_item_id"] == chosen
        assert result["resolution"]["by"] == "A. Gharib"
        assert "before the trip was cleaned up" in result["resolution"]["note"]
        assert result["recheck"]["conflicts"] == 0
        assert (
            payload(self._run(workspace_root, config, "knowledge", "conflicts", "--json")[1])[
                "count"
            ]
            == 0
        )
        _code, text, _err = self._run(workspace_root, config, "knowledge", "conflicts")
        assert "nothing is currently disputed" in text
        # The answer is the chosen one; the retired side stays reachable as history.
        current = payload(
            self._run(
                workspace_root,
                config,
                "knowledge",
                "facts",
                "--well",
                "A-3",
                "--predicate",
                "mud_weight",
                "--json",
            )[1]
        )
        assert [entry["original_value"] for entry in current["facts"]] == ["10.2"]
        history = payload(
            self._run(
                workspace_root,
                config,
                "knowledge",
                "facts",
                "--well",
                "A-3",
                "--predicate",
                "mud_weight",
                "--include-superseded",
                "--json",
            )[1]
        )
        assert sorted(entry["original_value"] for entry in history["facts"]) == ["10.2", "10.6"]
        assert {entry["status"] for entry in history["facts"]} == {"ACTIVE", "RETIRED"}
        assert other in {entry["item_id"] for entry in history["facts"]}

    def test_doctor_reports_an_unresolved_conflict_as_a_finding(self, live_workspace) -> None:
        """Every row can be valid and the workspace still be unsound.

        ``doctor`` checks that the structures agree, and a conflict is not a structural fault - it is
        two sources that have not been reconciled.  It belongs in the same place a person looks for
        problems, because the alternative is an answer somebody trusted that had an argument behind it.
        """
        workspace_root, config, _settings = live_workspace
        self._ingest(workspace_root, config)
        code, out, err = self._run(workspace_root, config, "doctor", "--json")
        assert code == 0, err
        clean = payload(out)
        assert clean["knowledge"]["facts"] == 64
        assert clean["knowledge"]["open_conflicts"] == 0
        assert clean["integrity_problems"] == []

        add_conflicting_mud_report(workspace_root / "corpus")
        self._run(workspace_root, config, "ingest", str(workspace_root / "corpus"))
        code, out, err = self._run(workspace_root, config, "doctor", "--json")
        assert code == 1, "a dispute is a finding"
        report = payload(out)
        assert report["knowledge"]["open_conflicts"] == 1
        assert any("unresolved knowledge conflict" in item for item in report["findings"]), report[
            "findings"
        ]
        assert report["integrity_problems"] == [], "nothing is malformed - only undecided"
        _code, text, _err = self._run(workspace_root, config, "doctor")
        assert "1 unresolved conflict(s)" in text, text

    def test_resolve_refuses_a_winner_that_was_never_on_the_table(self, live_workspace) -> None:
        workspace_root, config, _settings = live_workspace
        self._ingest(workspace_root, config)
        add_conflicting_mud_report(workspace_root / "corpus")
        self._run(workspace_root, config, "ingest", str(workspace_root / "corpus"))
        conflict = payload(
            self._run(workspace_root, config, "knowledge", "conflicts", "--json")[1]
        )["conflicts"][0]
        code, out, err = self._run(
            workspace_root,
            config,
            "knowledge",
            "resolve",
            conflict["id"],
            "--choose",
            "ki-not-a-fact",
            "--json",
        )
        assert code == 1, "an id that is not a candidate cannot be a decision"
        refusal = payload(out)
        assert refusal["ok"] is False
        assert "is not one of this conflict" in refusal["message"]
        assert "Traceback" not in out + err, (
            "--json is a promise about the stream, not about success"
        )
        _code, text_out, text_err = self._run(
            workspace_root,
            config,
            "knowledge",
            "resolve",
            conflict["id"],
            "--choose",
            "ki-not-a-fact",
        )
        assert "is not one of this conflict" in text_err, text_err
        assert "Traceback" not in text_err
        assert text_out == "", "a rejected attempt prints no document"
        remaining = payload(
            self._run(workspace_root, config, "knowledge", "conflicts", "--json")[1]
        )
        assert remaining["count"] == 1, (
            "a rejected attempt leaves the argument open, it does not close it"
        )
