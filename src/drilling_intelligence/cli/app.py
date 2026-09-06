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
            raise DrillingIntelligenceError(
                f"workspace path does not exist: {root}",
                hint="create it with `drillintel workspace create <path>`",
            )
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
        hint=("known wells: " + ", ".join(known))
        if known
        else "no wells registered yet; `drillintel ingest` associates documents with a well via --well",
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
        row = repository.get_or_create_workspace(
            known or str(workspace.root),
            name=workspace.config.name,
            data_dir=str(workspace.data_dir),
        )
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
    workspace = Workspace.create(
        args.path,
        _settings_for(args),
        name=args.name or "",
        corpus_dirs=list(args.corpus_dir or []),
    )
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
            raise DrillingIntelligenceError(
                f"nothing to ingest: {root} does not exist",
                hint="pass the folder that holds the documents",
            )
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
        result = pipeline.run(
            root=root,
            workspace_id=_workspace_row_id(workspace),
            well_id=well_id,
            force=args.force,
            limit=args.limit,
        )
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
            lines.append(
                f"  no longer on disk: {names}{' …' if len(result.removed) > 5 else ''} (still in the registry)"
            )
        for warning in result.warnings[:20]:
            lines.append(f"  warning: {warning}")
        for problem in result.invariant_problems[:10]:
            lines.append(
                f"  invariant: {problem['problem']} on {problem['table']}({problem['row_id']})"
            )
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
            lines.append(
                f"{number:>3}. {hit.metadata['filename']}  ({hit.kind}, score {hit.score:.3f}, revision {hit.version_number})"
            )
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
                state = (
                    "citation verified"
                    if hit.verification.get("ok")
                    else f"citation {hit.verification['status'].lower()}"
                )
                detail = (
                    hit.verification.get("detail") or Path(hit.verification.get("source", "")).name
                )
                lines.append(f"     {state}: {detail}")
        if not response.results:
            lines.append(
                "nothing found; `drillintel index status` shows whether the index is current"
            )
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
            lines = [
                f"rebuilt from the registry: {stats['documents']} document(s), {stats['versions']} version(s), {stats['chunks']} chunk(s)"
            ]
            lines.append(f"  written to {workspace.index_database_path}")
            return_code = 0
        elif args.action == "prune":
            removed = service.prune()
            stats = service.stats()
            payload = {"action": "prune", "versions_removed": removed, "stats": stats}
            lines = [
                f"removed {removed} obsolete version(s) from the searchable index",
                f"  now: {stats['documents']} document(s), {stats['chunks']} chunk(s)",
            ]
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
            from ..knowledge.repository import KnowledgeRepository

            knowledge = KnowledgeRepository(session).counts()
            open_conflicts = len(KnowledgeRepository(session).conflicts())
        service = SearchService.for_workspace(workspace)
        stats = service.stats()
        findings: list[str] = [
            f"{item['problem']} on {item['table']}({item['row_id']})" for item in problems
        ]
        if open_conflicts:
            # A workspace where two sources disagree and nobody has decided is not corrupt, but it is
            # not sound either, and `doctor` is what a person runs before trusting an answer.  A
            # disputed engineering value belongs in its findings even though every row is valid.
            findings.append(
                f"{open_conflicts} unresolved knowledge conflict(s): `drillintel knowledge conflicts`"
            )
        if stats.get("stale_versions") or stats.get("orphaned") or stats.get("missing_versions"):
            findings.append(
                "the search index disagrees with the registry: `drillintel index rebuild`"
            )
        if not stats.get("fts_available"):
            findings.append(
                "this SQLite build has no FTS5: search uses the scan path (same answers, more CPU)"
            )
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
            "knowledge": {
                "facts": knowledge.get("facts", 0),
                "open_conflicts": open_conflicts,
                "by_status": knowledge.get("by_status", {}),
            },
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
            f"knowledge {knowledge.get('facts', 0)} fact(s), {open_conflicts} unresolved conflict(s)",
            "findings " + ("none" if not findings else ""),
        ]
        lines.extend(f"  - {item}" for item in findings)
        _emit(payload, as_json=args.json, lines=lines)
        return 1 if findings else 0
    finally:
        workspace.close()


def command_knowledge(args: argparse.Namespace) -> int:
    """Ask what the platform *knows*, rebuild that knowledge, or settle an argument about it.

    ``status``, ``rebuild``, ``conflicts``, ``facts`` and ``resolve`` map one-to-one onto
    :class:`~drilling_intelligence.knowledge.service.KnowledgeExtractionService`; there is no
    command-line-only behaviour, and every number printed here is the number the database holds.
    """
    workspace = _open_workspace(args)
    try:
        from ..knowledge.service import KnowledgeExtractionService

        service = KnowledgeExtractionService.for_workspace(workspace)
        if args.action == "rebuild":
            payload = service.rebuild(
                workspace_id=_workspace_row_id(workspace),
                well_id=_resolve_well_id(workspace, args.well),
            )
            facts = payload["facts"]
            lines = [
                f"re-derived knowledge for {payload['versions']} version(s) from the stored artefacts",
                f"  facts created {facts['created']}, updated {facts['updated']}, unchanged {facts['unchanged']}",
                f"  relations written {payload['relations']}, fields skipped {payload['skipped_fields']}",
                f"  derived rows removed first: {payload['removed']} (manual notes are kept)",
                f"  conflicts open afterwards: {payload['conflicts']['conflicts']}",
            ]
            for warning in payload["warnings"][:10]:
                lines.append(f"  warning: {warning}")
            return_code = 0
        elif args.action == "conflicts":
            well_id = _resolve_well_id(workspace, args.well) or ""
            payload = service.conflicts(well_id=well_id, status=args.status, limit=args.limit)
            lines = [f"{payload['count']} conflict(s) with status {payload['status_filter']}"]
            for entry in payload["conflicts"]:
                lines.append(
                    f"  {entry['id']}  {entry['property']} [{entry['record_state']}] in {entry['compare_unit'] or 'no unit'}"
                )
                if entry.get("note"):
                    lines.append(f"    {entry['note']}")
                for candidate in entry["candidates"]:
                    stated = (
                        candidate.get("text")
                        or f"{candidate.get('value') if candidate.get('value') is not None else '?'} {candidate.get('unit') or ''}".strip()
                    )
                    preferred = (
                        " <- ranked first by authority, then date"
                        if candidate.get("authority_rank") == 0
                        else ""
                    )
                    lines.append(
                        f"      {stated}  ({candidate['item_id']})"
                        f" [{candidate.get('source') or 'unknown source'}, rev {candidate.get('revision') or '?'}, {candidate.get('document_date') or 'undated'}, {candidate.get('authority_tier') or 'no tier'}]"
                    )
                    lines.append(
                        f"        at {candidate.get('locator_ref') or 'no location recorded'}{preferred}"
                    )
                lines.append(
                    '    settle it: drillintel knowledge resolve {} --choose <item id above> --note "why"'.format(
                        entry["id"]
                    )
                )
            if not payload["conflicts"]:
                lines.append("  nothing is currently disputed")
            return_code = 0
        elif args.action == "resolve":
            payload = service.resolve(
                args.conflict_id,
                chosen_item_id=args.choose,
                note=args.note or "",
                by=args.by or "operator",
            )
            recheck = payload["recheck"]
            lines = [
                f"conflict {payload['conflict_id']} is {payload['status']}: kept {payload['chosen_item_id']}",
                f"  re-checked the key: {recheck['conflicts']} conflict(s), {recheck['cleared']} cleared, {recheck['items_marked']} still marked",
            ]
            if payload["resolution"].get("note"):
                lines.append(f"  note: {payload['resolution']['note']}")
            return_code = 0
        elif args.action == "facts":
            payload = service.facts(
                well=args.well or "",
                entity=args.entity or "",
                document_id=args.document_id or "",
                predicate=args.predicate or "",
                limit=args.limit,
                include_superseded=args.include_superseded,
            )
            lines = [f"{payload['count']} fact(s) for {payload['scope']}"]
            for entry in payload["facts"]:
                rendered = f"{entry['original_value']} {entry.get('original_unit') or ''}".strip()
                normalised = entry.get("normalized_value")
                # The normalised value is worth printing when it *adds* something - a unit
                # conversion, or a number parsed out of wording the source wrote by hand - and is
                # noise when it merely repeats what was already on the page.
                detail = ""
                if normalised not in (None, ""):
                    converted = f"{normalised} {entry.get('normalized_unit') or ''}".strip()
                    if converted != rendered.replace(",", ""):
                        detail = f"  (as measured: {converted})"
                flag = "" if entry.get("status") == "ACTIVE" else f" [{entry.get('status')}]"
                lines.append(f"  {entry['predicate']} = {rendered}{detail}{flag}")
                lines.append(f"    source: {entry['citation']}")
            if not payload["facts"]:
                lines.append(
                    "  nothing derived yet; run `drillintel knowledge rebuild` after ingesting"
                )
            return_code = 0
        else:
            payload = service.status(workspace_id=_workspace_row_id(workspace))

            def tally(key: str) -> str:
                entries = payload.get(key) or {}
                return (
                    ", ".join(f"{name} {count}" for name, count in sorted(entries.items()))
                    or "none"
                )

            lines = [
                f"knowledge items: {payload.get('facts', 0)} by origin: {tally('by_origin')} (by value type: {tally('by_value_type')})",
                f"by status: {', '.join(f'{key} {value}' for key, value in sorted((payload.get('by_status') or {}).items())) or 'none'}",
                f"by subject: {', '.join(f'{key} {value}' for key, value in sorted((payload.get('by_entity_type') or {}).items())) or 'none'}",
                f"relations {payload.get('relations', 0)}, open conflicts {payload.get('open_conflicts', 0)}",
                f"registry vs knowledge: {payload.get('versions_without_knowledge', 0)} of {payload.get('versions_with_artefacts', 0)} current version(s) have no facts, {payload.get('detached_facts', 0)} fact(s) citing a superseded version",
            ]
            index = payload.get("index") or {}
            if index:
                lines.append(
                    "search index: "
                    + (
                        f"{index.get('knowledge_chunks', 0)} fact chunk(s) of {index.get('chunks', 0)}"
                        if "error" not in index
                        else f"unavailable ({index['error']})"
                    )
                )
            stale = bool(payload.get("needs_rebuild"))
            lines.append(
                f"rebuild recommended: {'yes - drillintel knowledge rebuild' if stale else 'no'}"
            )
            return_code = 1 if stale else 0
        _emit(payload, as_json=args.json, lines=lines)
        return return_code
    finally:
        workspace.close()


# --------------------------------------------------------------------------- parser
def _common() -> argparse.ArgumentParser:
    """Options every subcommand accepts, so ``drillintel search x --json`` works as written."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        help="TOML config file (default: $DRILLINTEL_CONFIG, then ./configs/development.toml)",
    )
    common.add_argument("--workspace", help="workspace folder (default: the current folder)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument(
        "--debug", action="store_true", help="raise instead of printing an error message"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="drillintel",
        description="Drilling Intelligence - document registry, search and diagnostics (local-first).",
        parents=[common],
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command")

    version = sub.add_parser(
        "version", help="print the platform and Python versions", parents=[common]
    )
    version.set_defaults(handler=command_version)

    workspace_parser = sub.add_parser(
        "workspace", help="create or inspect a workspace folder", parents=[common]
    )
    workspace_sub = workspace_parser.add_subparsers(dest="action", required=True)
    create = workspace_sub.add_parser(
        "create", help="initialise a workspace (folders, marker, databases)", parents=[common]
    )
    create.add_argument("path", help="folder to initialise")
    create.add_argument("--name", default="", help="display name (default: the folder name)")
    create.add_argument(
        "--corpus-dir", action="append", help="relative folder to scan by default (repeatable)"
    )
    create.set_defaults(handler=command_workspace_create)

    ingest = sub.add_parser(
        "ingest",
        help="scan a folder: register what changed, extract, classify, index",
        parents=[common],
    )
    ingest.add_argument("root", help="folder of documents to ingest")
    ingest.add_argument("--well", help="associate the documents with this well (id or name)")
    ingest.add_argument(
        "--limit", type=int, default=0, help="process at most N files (0 = no limit)"
    )
    ingest.add_argument(
        "--force", action="store_true", help="reprocess files that the cache says are unchanged"
    )
    ingest.add_argument(
        "--full", action="store_true", help="include per-file results in --json output"
    )
    ingest.set_defaults(handler=command_ingest)

    search = sub.add_parser(
        "search", help="search indexed text; every result cites its location", parents=[common]
    )
    search.add_argument(
        "query", help='words or "quoted phrases"; measurements keep their units (10.2 ppg)'
    )
    search.add_argument("--well", help="restrict to one well (id or name)")
    search.add_argument("--type", help="document type, e.g. MUD_REPORT, DDR, DRILLING_PROGRAM")
    search.add_argument("--revision", help="restrict to a revision label")
    search.add_argument("--since", help="documents dated on or after this ISO date")
    search.add_argument("--until", help="documents dated on or before this ISO date")
    search.add_argument("--limit", type=int, default=20, help="maximum results (default 20)")
    search.add_argument(
        "--include-superseded", action="store_true", help="search history, not only what is current"
    )
    search.add_argument(
        "--verify",
        action="store_true",
        help="re-read each hit's source file and report whether the citation still holds",
    )
    search.add_argument(
        "--rebuild", action="store_true", help="rebuild the index from the registry first"
    )
    search.set_defaults(handler=command_search)

    index = sub.add_parser(
        "index", help="inspect, rebuild or prune the derived search index", parents=[common]
    )
    index_sub = index.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("status", "report what the index holds and whether it agrees with the registry"),
        ("rebuild", "recompute the whole index from the registry"),
        ("prune", "drop searchable state for superseded versions"),
    ):
        action = index_sub.add_parser(name, help=help_text, parents=[common])
        action.set_defaults(handler=command_index)

    knowledge = sub.add_parser(
        "knowledge",
        help="the facts the corpus asserts: status, rebuild, conflicts, listing, resolution",
        parents=[common],
    )
    knowledge_sub = knowledge.add_subparsers(dest="action", required=True)
    status = knowledge_sub.add_parser(
        "status", help="what is known, and whether it is behind the registry", parents=[common]
    )
    status.set_defaults(handler=command_knowledge)
    rebuild = knowledge_sub.add_parser(
        "rebuild",
        help="re-derive every fact from the stored artefacts (manual notes survive)",
        parents=[common],
    )
    rebuild.add_argument("--well", help="limit the rebuild to documents of this well (id or name)")
    rebuild.set_defaults(handler=command_knowledge)
    conflicts = knowledge_sub.add_parser(
        "conflicts",
        help="values that disagree, with both sides and their sources",
        parents=[common],
    )
    conflicts.add_argument("--well", help="restrict to one well (id or name)")
    conflicts.add_argument("--status", help="conflict status to list (default: open)", default=None)
    conflicts.add_argument(
        "--limit", type=int, default=50, help="maximum conflicts to list (default 50)"
    )
    conflicts.set_defaults(handler=command_knowledge)
    facts = knowledge_sub.add_parser(
        "facts", help="list derived facts for a well, an entity or a document", parents=[common]
    )
    facts.add_argument("--well", help="well this fact belongs to (id or name)")
    facts.add_argument("--entity", help="subject as type:id, e.g. bit:ki-1f4a or formation:frm-2")
    facts.add_argument("--document", dest="document_id", help="facts read from this document id")
    facts.add_argument("--predicate", help="only this property, e.g. mud_weight")
    facts.add_argument("--limit", type=int, default=50, help="maximum facts to list (default 50)")
    facts.add_argument(
        "--include-superseded", action="store_true", help="show what older revisions said too"
    )
    facts.set_defaults(handler=command_knowledge)
    resolve = knowledge_sub.add_parser(
        "resolve",
        help="settle a conflict: keep one value, retire the other (both stay stored)",
        parents=[common],
    )
    resolve.add_argument(
        "conflict_id", help="conflict id, as printed by `drillintel knowledge conflicts`"
    )
    resolve.add_argument("--choose", required=True, help="the knowledge_item id that wins")
    resolve.add_argument(
        "--note", help="why this side was chosen (recorded in the conflict and the audit trail)"
    )
    resolve.add_argument("--by", help="who decided (default: the current user, else 'operator')")
    resolve.set_defaults(handler=command_knowledge)

    doctor = sub.add_parser(
        "doctor",
        help="check the workspace, schema, registry invariants and index together",
        parents=[common],
    )
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
        except DrillingIntelligenceError as exc:
            # A caller that asked for a document gets a document even when the answer is "no": the
            # message and the hint the text path prints, on stdout, with a non-zero exit code.  A
            # traceback on the stream a script is parsing is the one thing --json promises to avoid.
            _emit(exc.to_dict() | {"ok": False}, as_json=True, lines=[f"error: {exc}"])
            return 1
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            print("interrupted", file=sys.stderr)
            return 130
        except Exception as exc:
            if getattr(args, "debug", False):
                raise
            # The same rule as the text path, including the same concession: an unexpected failure
            # is named in one line and ``--debug`` is how a developer gets the stack.
            _emit(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                as_json=True,
                lines=[],
            )
            return 1
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
