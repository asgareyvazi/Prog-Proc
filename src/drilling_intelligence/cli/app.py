"""``drillintel``: the command-line face of the same services the desktop UI will use.

The CLI exists for three reasons and does nothing outside them:

1.  **Run and verify the core without a GUI.**  Every subcommand calls straight into the same
    :class:`~drilling_intelligence.wells.workspace.Workspace`,
    :class:`~drilling_intelligence.ingestion.pipeline.IngestionPipeline` and
    :class:`~drilling_intelligence.search.service.SearchService` the UI will use.  There is no
    CLI-only logic here and no second implementation of anything: a rule that can only be
    reached from a terminal is a rule nobody tests.
2.  **Operations that must be possible headless.**  Rebuilding a disposable index, checking the
    registry invariants after an interrupted run, or asking "what does the platform hold about
    well A-3" must not require a desktop session.
3.  **Machine-readable output.**  ``--json`` on every command, so a script or a smoke test
    asserts on the same numbers a human reads.

``argparse`` (stdlib) with explicit subcommands and nothing else.  Exit codes: ``0`` success,
``1`` a domain error or ingestion failure, ``2`` a usage error (argparse's own).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from .. import __version__
from ..config.settings import Settings
from ..core.errors import DrillingIntelligenceError
from ..database.integrity import (
    check_current_version_invariants,
    check_extraction_cache,
    check_knowledge_relations,
)
from ..documents.repository import DocumentRepository
from ..ingestion.pipeline import IngestionPipeline
from ..search.service import SearchService, build_index
from ..wells.repository import WellRepository
from ..wells.workspace import Workspace

__all__ = ["build_parser", "main"]


# --------------------------------------------------------------------------- shared helpers
def _settings_for(args: argparse.Namespace) -> Settings:
    return Settings.load(args.config) if args.config else Settings.load()


def _open_workspace(args: argparse.Namespace, *, create: bool = False) -> Workspace:
    """Open (or initialise) the workspace a command operates on.

    The default is the current folder, which is what makes ``cd`` into a project and asking
    questions about it work without flags - and what makes a mistake loud rather than silent:
    :class:`~drilling_intelligence.wells.workspace.Workspace` refuses a folder without the
    workspace marker.
    """
    root = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    root = root.resolve()
    if not root.exists():
        if not create:
            raise DrillingIntelligenceError(f"workspace path does not exist: {root}", hint="create it with `drillintel workspace create <path>`")
        return Workspace.create(root, _settings_for(args))
    workspace = Workspace.open(root, _settings_for(args), create=create)
    workspace.configure_logging()
    return workspace


#: The stdout that was in force when a ``--json`` command started.  ``main`` sets it, ``_emit``
#: writes to it, and nothing else touches it: the CLI is single-threaded by construction, and a
#: test that swaps ``sys.stdout`` keeps receiving the payload because "the stream in force" is
#: exactly the one it swapped in.
_DATA_STREAM: Any = None


def _data_stream(as_json: bool):
    """Where the payload goes: the stdout of the *caller*, not of a chatty parser.

    ``--json`` makes stdout a data channel - one JSON document, nothing else - which means
    third-party noise (PyMuPDF prints a layout-engine suggestion while importing, and other
    parsers will find their own ways to talk) must not land in it.  ``main`` redirects
    ``sys.stdout`` to stderr for the duration of a ``--json`` command, and the payload is
    written to the stream that was there before the redirect.  When nobody redirected anything -
    a library calling ``_emit`` directly - that is simply stdout.
    """
    if as_json and _DATA_STREAM is not None:
        return _DATA_STREAM
    return sys.stdout


def _emit(payload: Any, *, as_json: bool, lines: list[str]) -> None:
    """One result: JSON for machines, aligned text for people.  Same data, two renderings."""
    stream = _data_stream(as_json)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), file=stream)
        stream.flush()
        return
    for line in lines:
        print(line)


def _resolve_well_id(workspace: Workspace, ref: str | None) -> str | None:
    """Accept a well id *or* a well name: nobody remembers UUIDs at a terminal.

    An unknown reference is an error that lists the alternatives rather than a filter that
    quietly matches nothing - a silently empty result set is the worst thing a search tool can
    do to a person looking for a document.
    """
    if not ref:
        return None
    with workspace.database.session() as session:
        repository = WellRepository(session)
        well = repository.get_well(ref) or repository.find_well(ref)
        if well is not None:
            return str(well.id)
        known = [item.name for item in repository.list_wells(limit=50)]
    raise DrillingIntelligenceError(
        f"no well matches {ref!r} in this workspace",
        hint=("known wells: " + ", ".join(known)) if known else "no wells registered yet; `drillintel ingest` associates documents with a well via --well",
    )


def _workspace_row_id(workspace: Workspace) -> str:
    """The registry ``workspace`` row for this folder, created on first use.

    The on-disk workspace - its marker file, folders and SQLite files - and the registry row
    named ``workspace`` are different things, and the row is the scope every document identity is
    keyed on.  Registration happens here rather than only when a well appears, because otherwise
    the first ingest files everything under ``workspace_id IS NULL`` and the moment a well is
    created a second, disconnected set of rows appears for the same files; per-workspace queries
    and "no longer on disk" detection both read through that id and would quietly miss half the
    folder.  An existing row is matched by resolved path first, so a workspace registered by the
    API with a differently spelled path is reused instead of duplicated.
    """
    with workspace.database.session() as session:
        repository = WellRepository(session)
        known = ""
        for row in repository.list_workspaces():
            try:
                if Path(row.root_path).expanduser().resolve() == workspace.root:
                    known = str(row.root_path)
                    break
            except OSError:  # pragma: no cover - a stored path that cannot be resolved
                continue
        # Going through ``get_or_create_workspace`` even for a row that exists is deliberate: it
        # is the one place that fills in a name or a data directory left empty by whoever created
        # the row, and it is keyed on the *stored* path so the tolerant match above can never end
        # up registering the same folder twice.
        row = repository.get_or_create_workspace(known or str(workspace.root), name=workspace.config.name, data_dir=str(workspace.data_dir))
        session.commit()
        return str(row.id)


# --------------------------------------------------------------------------- commands
def command_version(args: argparse.Namespace) -> int:
    _emit(
        {"version": __version__, "python": sys.version.split()[0], "platform": sys.platform},
        as_json=args.json,
        lines=[f"drillintel {__version__} (python {sys.version.split()[0]}, {sys.platform})"],
    )
    return 0


def command_workspace_create(args: argparse.Namespace) -> int:
    workspace = Workspace.create(args.path, _settings_for(args), name=args.name or "", corpus_dirs=list(args.corpus_dir or []))
    payload = {"created": str(workspace.root), **workspace.summary()}
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"workspace created at {workspace.root}",
            f"  registry: {workspace.database_path}",
            f"  index:    {workspace.index_database_path}",
            f"  corpora:  {', '.join(workspace.config.corpus_dirs) or '(none)'}",
        ],
    )
    workspace.close()
    return 0


def command_ingest(args: argparse.Namespace) -> int:
    workspace = _open_workspace(args)
    try:
        root = Path(args.root).expanduser().resolve()
        if not root.exists():
            raise DrillingIntelligenceError(f"nothing to ingest: {root} does not exist", hint="pass the folder that holds the documents")
        well_id = _resolve_well_id(workspace, args.well)
        # The same optional seam the UI uses: ingestion works without an index, and with one it
        # also makes what it extracted searchable in the same pass.  `build_index` chooses the
        # in-memory backend if the sidecar cannot be written, so a read-only folder ingests
        # rather than failing.
        pipeline = IngestionPipeline(
            settings=workspace.settings,
            workspace_root=workspace.root,
            database=workspace.database,
            index=build_index(workspace.index_database),
        )
        result = pipeline.run(root=root, workspace_id=_workspace_row_id(workspace), well_id=well_id, force=args.force, limit=args.limit)
        payload = result.to_dict()
        if not args.full:
            payload.pop("results", None)
        lines = [
            f"scanned {result.files_found} file(s) under {root}",
            f"  registered {result.files_registered}, extracted {result.files_extracted}, from cache {result.from_cache}, failures {result.failures}",
            f"  counts: {', '.join(f'{key}={value}' for key, value in sorted(result.counts.items()))}",
            f"  indexed {result.indexed} version(s) / {result.indexed_chunks} chunk(s); {result.index_removed} obsolete version(s) left the index",
        ]
        if result.removed:
            names = ", ".join(str(item.get("filename", "")) for item in result.removed[:5])
            lines.append(f"  no longer on disk: {names}{' …' if len(result.removed) > 5 else ''} (still in the registry)")
        for warning in result.warnings[:20]:
            lines.append(f"  warning: {warning}")
        for problem in result.invariant_problems[:10]:
            lines.append(f"  invariant: {problem['problem']} on {problem['table']}({problem['row_id']})")
        _emit(payload, as_json=args.json, lines=lines)
        return 1 if result.failures else 0
    finally:
        workspace.close()


def command_search(args: argparse.Namespace) -> int:
    workspace = _open_workspace(args)
    try:
        service = SearchService.for_workspace(workspace)
        if args.rebuild:
            service.rebuild()
        well_id = _resolve_well_id(workspace, args.well)
        response = service.search(
            args.query,
            well_id=well_id,
            document_type=args.type.upper() if args.type else None,
            revision=args.revision,
            date_from=args.since,
            date_to=args.until,
            include_superseded=args.include_superseded,
            limit=args.limit,
            verify=args.verify,
        )
        header = f"{len(response.results)} result(s) for {args.query!r}"
        if response.broadened:
            header += "  [no chunk matched every term; these match any of them]"
        if response.truncated:
            header += "  [candidate cap reached: more matched than were scored]"
        lines = [header]
        for number, hit in enumerate(response.results, start=1):
            lines.append(f"{number:>3}. {hit.metadata['filename']}  ({hit.kind}, score {hit.score:.3f}, revision {hit.version_number})")
            for row in _wrapped(hit.snippet, 96):
                lines.append(f"     {row}")
            where = hit.locator_ref or "document level (no location recorded)"
            label = f"{hit.metadata['document_type']}"
            if hit.metadata["well_name"]:
                label += f", well {hit.metadata['well_name']}"
            if hit.metadata["document_date"]:
                label += f", dated {hit.metadata['document_date'][:10]}"
            lines.append(f"     at {where}   [{label}]")
            if hit.verification:
                state = "citation verified" if hit.verification.get("ok") else f"citation {hit.verification['status'].lower()}"
                detail = hit.verification.get("detail") or Path(hit.verification.get("source", "")).name
                lines.append(f"     {state}: {detail}")
        if not response.results:
            lines.append("nothing found; `drillintel index status` shows whether the index is current")
        _emit(response.to_dict(), as_json=args.json, lines=lines)
        return 0
    finally:
        workspace.close()


def _wrapped(text: str, width: int) -> list[str]:
    """Snippets are one paragraph of a document: keep them readable, never lose the numbers."""
    out: list[str] = []
    for block in str(text).splitlines() or [""]:
        words, current = block.split(), ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) > width and current:
                out.append(current)
                current = word
            else:
                current = candidate
        out.append(current)
    return out[:6] or [""]


def command_index(args: argparse.Namespace) -> int:
    workspace = _open_workspace(args)
    try:
        service = SearchService.for_workspace(workspace)
        if args.action == "rebuild":
            stats = service.rebuild()
            payload: dict[str, Any] = {"action": "rebuild", "stats": stats}
            lines = [f"rebuilt from the registry: {stats['documents']} document(s), {stats['versions']} version(s), {stats['chunks']} chunk(s)"]
            lines.append(f"  written to {workspace.index_database_path}")
            return_code = 0
        elif args.action == "prune":
            removed = service.prune()
            stats = service.stats()
            payload = {"action": "prune", "versions_removed": removed, "stats": stats}
            lines = [f"removed {removed} obsolete version(s) from the searchable index", f"  now: {stats['documents']} document(s), {stats['chunks']} chunk(s)"]
            return_code = 0
        else:
            stats = service.stats()
            payload = {"action": "status", "stats": stats, "needs_rebuild": service.needs_rebuild()}
            lines = [
                f"index file: {workspace.index_database_path}",
                f"  documents {stats['documents']}, versions {stats['versions']}, chunks {stats['chunks']}",
                f"  schema {stats['schema_version']}, built {stats['built_at'] or '(never rebuilt; maintained incrementally)'}",
                f"  registry revision at build: {stats['registry_revision'] or 'unknown'}",
                f"  fts5: {'available (candidate acceleration only)' if stats['fts_available'] else 'unavailable (scan path; same results)'}",
                f"  drift: {stats['stale_versions']} stale, {stats['orphaned']} orphaned, {stats['missing_versions']} not yet indexed",
                f"  rebuild recommended: {'yes' if service.needs_rebuild() else 'no'}",
            ]
            return_code = 1 if service.needs_rebuild() else 0
        _emit(payload, as_json=args.json, lines=lines)
        return return_code
    finally:
        workspace.close()


def command_doctor(args: argparse.Namespace) -> int:
    """One command that reads every structure and says whether they agree.

    It is the recovery path after a crash, a hand-edited database or a move between machines:
    the registry's invariants, the cache's uniqueness promise, the knowledge edges and the
    search index are all checked, and each finding names the command that fixes it.
    """
    workspace = _open_workspace(args)
    try:
        with workspace.database.session() as session:
            repository = DocumentRepository(session)
            counts = repository.counts()
            problems = [problem.to_dict() for problem in check_current_version_invariants(session)]
            problems += [problem.to_dict() for problem in check_extraction_cache(session)]
            problems += [problem.to_dict() for problem in check_knowledge_relations(session)]
        service = SearchService.for_workspace(workspace)
        stats = service.stats()
        findings: list[str] = [f"{item['problem']} on {item['table']}({item['row_id']})" for item in problems]
        if stats.get("stale_versions") or stats.get("orphaned") or stats.get("missing_versions"):
            findings.append("the search index disagrees with the registry: `drillintel index rebuild`")
        if not stats.get("fts_available"):
            findings.append("this SQLite build has no FTS5: search uses the scan path (same answers, more CPU)")
        migration = workspace.migration
        if migration is not None and not migration.up_to_date:
            findings.append(
                f"schema is at {migration.current!r} while head is {migration.head!r} (mode {migration.mode!r}): run `alembic upgrade head`"
            )
        summary = workspace.settings.summary()
        payload = {
            "version": __version__,
            "python": sys.version.split()[0],
            "workspace": workspace.summary(),
            "settings": summary,
            "registry": counts,
            "schema": migration.to_dict() if migration is not None else None,
            "index": stats,
            "integrity_problems": problems,
            "findings": findings,
        }
        lines = [
            f"drillintel {__version__} (python {sys.version.split()[0]})",
            f"workspace {workspace.root}",
            f"  registry {workspace.database_path}",
            f"  index    {workspace.index_database_path}",
            f"schema {migration.current or '(none)'} at head {migration.head or '(unknown)'} - {migration.mode if migration else 'not opened'}",
            f"config  {workspace.settings.summary()['source_path']}",
            f"ai      {summary['ai']['provider']} / model {summary['ai']['model'] or '(none)'}, required={summary['ai']['require_ai']}",
            f"registry {counts['documents']} document(s), {counts['versions']} version(s), {counts['extractions']} artefact(s)",
            f"search   {stats['documents']} document(s), {stats['chunks']} chunk(s), fts5={'yes' if stats['fts_available'] else 'no'}",
            f"types    {', '.join(f'{key}={value}' for key, value in sorted(counts['by_classification'].items())) or 'nothing registered yet'}",
            "findings " + ("none" if not findings else ""),
        ]
        lines.extend(f"  - {item}" for item in findings)
        _emit(payload, as_json=args.json, lines=lines)
        return 1 if findings else 0
    finally:
        workspace.close()


# --------------------------------------------------------------------------- parser
def _common() -> argparse.ArgumentParser:
    """Options every subcommand accepts, so ``drillintel search x --json`` works as written."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="TOML config file (default: $DRILLINTEL_CONFIG, then ./configs/development.toml)")
    common.add_argument("--workspace", help="workspace folder (default: the current folder)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--debug", action="store_true", help="raise instead of printing an error message")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(prog="drillintel", description="Drilling Intelligence - document registry, search and diagnostics (local-first).", parents=[common])
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command")

    version = sub.add_parser("version", help="print the platform and Python versions", parents=[common])
    version.set_defaults(handler=command_version)

    workspace_parser = sub.add_parser("workspace", help="create or inspect a workspace folder", parents=[common])
    workspace_sub = workspace_parser.add_subparsers(dest="action", required=True)
    create = workspace_sub.add_parser("create", help="initialise a workspace (folders, marker, databases)", parents=[common])
    create.add_argument("path", help="folder to initialise")
    create.add_argument("--name", default="", help="display name (default: the folder name)")
    create.add_argument("--corpus-dir", action="append", help="relative folder to scan by default (repeatable)")
    create.set_defaults(handler=command_workspace_create)

    ingest = sub.add_parser("ingest", help="scan a folder: register what changed, extract, classify, index", parents=[common])
    ingest.add_argument("root", help="folder of documents to ingest")
    ingest.add_argument("--well", help="associate the documents with this well (id or name)")
    ingest.add_argument("--limit", type=int, default=0, help="process at most N files (0 = no limit)")
    ingest.add_argument("--force", action="store_true", help="reprocess files that the cache says are unchanged")
    ingest.add_argument("--full", action="store_true", help="include per-file results in --json output")
    ingest.set_defaults(handler=command_ingest)

    search = sub.add_parser("search", help="search indexed text; every result cites its location", parents=[common])
    search.add_argument("query", help='words or "quoted phrases"; measurements keep their units (10.2 ppg)')
    search.add_argument("--well", help="restrict to one well (id or name)")
    search.add_argument("--type", help="document type, e.g. MUD_REPORT, DDR, DRILLING_PROGRAM")
    search.add_argument("--revision", help="restrict to a revision label")
    search.add_argument("--since", help="documents dated on or after this ISO date")
    search.add_argument("--until", help="documents dated on or before this ISO date")
    search.add_argument("--limit", type=int, default=20, help="maximum results (default 20)")
    search.add_argument("--include-superseded", action="store_true", help="search history, not only what is current")
    search.add_argument("--verify", action="store_true", help="re-read each hit's source file and report whether the citation still holds")
    search.add_argument("--rebuild", action="store_true", help="rebuild the index from the registry first")
    search.set_defaults(handler=command_search)

    index = sub.add_parser("index", help="inspect, rebuild or prune the derived search index", parents=[common])
    index_sub = index.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("status", "report what the index holds and whether it agrees with the registry"),
        ("rebuild", "recompute the whole index from the registry"),
        ("prune", "drop searchable state for superseded versions"),
    ):
        action = index_sub.add_parser(name, help=help_text, parents=[common])
        action.set_defaults(handler=command_index)

    doctor = sub.add_parser("doctor", help="check the workspace, schema, registry invariants and index together", parents=[common])
    doctor.set_defaults(handler=command_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, run one command, turn domain errors into a message and an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False) and not getattr(args, "command", None):
        return command_version(args)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    global _DATA_STREAM  # noqa: PLW0603 - see _data_stream(): one command, one stream
    if getattr(args, "json", False):
        # stdout carries the document; everything else a command or a library prints goes to
        # stderr, where it belongs and where a pipeline will not try to parse it.
        _DATA_STREAM = sys.stdout
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return int(handler(args))
        finally:
            _DATA_STREAM = None
    try:
        return int(handler(args))
    except DrillingIntelligenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - the module is run through the entry point
    raise SystemExit(main())
