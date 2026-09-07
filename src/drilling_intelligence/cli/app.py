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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..config.settings import Settings
from ..core.errors import DrillingIntelligenceError
from ..database.integrity import (
    check_current_version_invariants,
    check_extraction_cache,
    check_knowledge_relations,
    check_operational_integrity,
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


def _record_counts(session: Session) -> dict[str, int]:
    """How many operational and engineering rows exist, which is what ``doctor`` prints.

    Counted with five queries rather than by loading rows, because a workspace with forty thousand NPT
    records must still answer this in a second - and because the only honest definition of "empty field" is
    the one that does not depend on how much data the check is willing to read.
    """
    from sqlalchemy import func

    from ..database.models import (
        DdrReport,
        LessonLearned,
        NptRecord,
        ProblemOccurrence,
        WellEvent,
        WellOperation,
    )

    def count(model: Any, *conditions: Any) -> int:
        statement = select(func.count()).select_from(model)
        for condition in conditions:
            statement = statement.where(condition)
        return int(session.scalar(statement) or 0)

    return {
        "reports": count(DdrReport),
        "operations": count(WellOperation),
        "events": count(WellEvent),
        "npt": count(NptRecord),
        "problems": count(ProblemOccurrence),
        "lessons": count(LessonLearned),
        "lessons_approved": count(LessonLearned, LessonLearned.status == "APPROVED"),
    }


def _table(rows: Sequence[Mapping[str, Any]], columns: Sequence[tuple[str, int]]) -> list[str]:
    """A fixed-width rendering of the same dictionaries ``--json`` prints.

    One helper for every list command, so the way an absent value is written ("-") does not differ between
    them: a person comparing two tables should not have to learn two vocabularies for "nobody recorded
    this".
    """
    if not rows:
        return []
    lines = ["  ".join(key.ljust(width) for key, width in columns)]
    for row in rows:
        cells: list[str] = []
        for key, width in columns:
            value = row.get(key)
            if isinstance(value, float):
                rendered = f"{value:g}"
            elif value is None or value == "":
                rendered = "-"
            else:
                rendered = str(value)
            cells.append(rendered[: width - 1].ljust(width))
        lines.append("  ".join(cells).rstrip())
    return lines


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
        failures = result.failures
        if getattr(args, "promote", False):
            # Promotion on request, never by default (ADR-0010).  A file has to be *read* before its
            # tables can become records, which is what ingestion just did - but a version that changed
            # mid-project should not silently rewrite what a person has been confirming.  The scope is
            # the one the user asked to ingest, so `--well A-3 --promote` cannot touch B-11's rows.
            from ..operations.service import OperationalService

            with workspace.database.session() as session:
                summary = OperationalService.for_workspace(workspace).promote_workspace(
                    session=session, well_id=well_id or ""
                )
                session.commit()
            totals = summary.get("totals") or {}
            payload["promotion"] = summary
            lines.append(
                f"  promoted {summary.get('versions', 0)} version(s): "
                f"{totals.get('created', 0)} row(s) new, {totals.get('unchanged', 0)} already there"
                + (f", {totals['conflict']} conflicting" if totals.get("conflict") else "")
            )
            skipped = summary.get("skipped") or {}
            if skipped:
                lines.append(
                    "  promotion skipped: "
                    + ", ".join(f"{key} {value}" for key, value in sorted(skipped.items()))
                )
            failures += int(totals.get("conflict", 0))
        _emit(payload, as_json=args.json, lines=lines)
        return 1 if failures else 0
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
            # The operational tables are read too.  A promoted row that links a problem on one well to an
            # event on another, or a revision chain with a loop in it, satisfies every constraint in the
            # schema - which is precisely why it needs a checker, and why `doctor` is where a person finds
            # it before quoting the number it produced.
            problems += [problem.to_dict() for problem in check_operational_integrity(session)]
            records = _record_counts(session)
            from ..knowledge.repository import KnowledgeRepository

            knowledge = KnowledgeRepository(session).counts()
            open_conflicts = len(KnowledgeRepository(session).conflicts())
        service = SearchService.for_workspace(workspace)
        stats = service.stats()
        findings: list[str] = [
            f"{item['problem']} on {item['table']}({item['row_id']})" for item in problems
        ]
        # What the operational tables hold, as its own line.  Deliberately not a *finding*: findings
        # change the exit code, which is how a script decides whether to trust this workspace, and "the
        # field has 5 NPT records" is context rather than something to fix.  "5,000 documents and no
        # operational records" is a workspace nobody has promoted into yet, and telling that apart from an
        # empty field is worth a line of output - but not worth a false alarm.
        notes = [
            "records    "
            + ", ".join(f"{key}={value}" for key, value in sorted(records.items()) if value)
            or "records    nothing promoted yet"
        ]
        if records["lessons"] and not records["lessons_approved"]:
            notes.append(
                f"lessons    {records['lessons']} recorded, none approved: "
                "`drillintel lessons list --unapproved`"
            )
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
            "operational": records,
            "notes": notes,
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
            *notes,
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


# --------------------------------------------------------------------------- scope
def _scope(
    args: argparse.Namespace,
    workspace: Workspace,
    *,
    allow_well: bool = True,
    required: bool = True,
    session: Any = None,
) -> dict[str, str]:
    """Resolve ``--well``/``--field``/``--project`` into ids, or explain why none of them matched.

    A name is accepted as well as an id, because a person at a terminal has the name in their head and a
    script has the id.  An unknown reference is an error that lists the alternatives rather than a filter
    that quietly matches nothing - the same rule :func:`_resolve_well_id` applies to documents.
    """
    from ..database.models import Project

    scope: dict[str, str] = {}
    if allow_well and getattr(args, "well", None):
        scope["well_id"] = _resolve_well_id(workspace, args.well) or ""
    if getattr(args, "field", None):
        from ..intelligence.service import IntelligenceService

        if session is not None:
            scope["field_id"] = IntelligenceService.for_workspace(workspace).resolve_field(
                args.field, session=session
            )
        else:
            with workspace.database.session() as own:
                scope["field_id"] = IntelligenceService.for_workspace(workspace).resolve_field(
                    args.field, session=own
                )
    if getattr(args, "project", None):
        wanted = str(args.project)
        with workspace.database.session() as own:
            row = own.get(Project, wanted) or own.scalar(
                select(Project).where(Project.name == wanted)
            )
            if row is None:
                known = [item.name for item in own.execute(select(Project)).scalars()]
                raise DrillingIntelligenceError(
                    f"no project matches {wanted!r}",
                    hint=("known projects: " + ", ".join(known))
                    if known
                    else "none registered yet",
                )
            scope["project_id"] = str(row.id)
    for key in list(scope):
        if not scope[key]:
            del scope[key]
    if not scope and required:
        raise DrillingIntelligenceError(
            "this command needs a scope",
            hint="pass --well, --field or --project (a name works as well as an id)",
        )
    return scope


def _picked(row: Any, keys: Sequence[tuple[str, int]] = ()) -> dict[str, Any]:
    """The named columns of a stored row - the projection a text table needs, and nothing more.

    No keys means the whole record: a command that prints one row in full (``patterns confirm``) should
    not have to repeat every column name to say "everything".
    """
    from ..database.serialize import record_to_dict

    full = record_to_dict(row)
    return {key: full.get(key) for key, _width in keys} if keys else dict(full)


# --------------------------------------------------------------------------- domain commands
def command_records(args: argparse.Namespace) -> int:
    """``drillintel records``: the operational tables, read, promoted and corrected from a terminal.

    Three verbs because those are the three things a person does with a promoted daily report before
    trusting a number: look at the rows it wrote, ask what the workspace holds in total, and mark the rows
    a human has checked.  Promotion is explicit and never implicit - ``ingest`` indexes files, ``records
    promote`` turns a version's tables into operations, events, NPT and problems, and a row that is
    already there is reported as unchanged rather than overwritten.

    Nothing here sums anything: the field-level totals belong to ``drillintel fields summary``, so the same
    hours are never added up twice by two pieces of code that can drift apart.
    """
    workspace = _open_workspace(args)
    try:
        from ..operations.repository import OperationsRepository
        from ..operations.service import OperationalService

        if args.action == "promote":
            scope = _scope(args, workspace, required=False)
            service = OperationalService.for_workspace(workspace)
            if args.document:
                outcome = service.promote(document_id=args.document, version_id=args.version or "")
                payload = outcome.to_dict()
                _emit(
                    payload,
                    as_json=args.json,
                    lines=[
                        f"promoted {args.document}: "
                        + ", ".join(
                            f"{key} {value}"
                            for key, value in sorted((payload.get("counts") or {}).items())
                        ),
                        "skipped: "
                        + (
                            ", ".join(
                                f"{key} {value}"
                                for key, value in sorted((payload.get("skipped") or {}).items())
                            )
                            or "nothing"
                        ),
                    ],
                )
                return 1 if (payload.get("totals") or {}).get("conflict") else 0
            with workspace.database.session() as session:
                summary = service.promote_workspace(session=session, **scope)
                session.commit()
            totals = summary.get("totals") or {}
            _emit(
                summary,
                as_json=args.json,
                lines=[
                    "scope: "
                    + (
                        ", ".join(f"{key}={value}" for key, value in sorted(scope.items())) or "all"
                    ),
                    # Said out loud because "0 new, 0 already there" has two readings - everything was
                    # already promoted, or this scope holds no report-shaped version at all - and only one
                    # of them is a job that is done.
                    f"{summary.get('versions', 0)} report-shaped version(s) in scope"
                    + (
                        f" ({summary.get('versions_with_records', 0)} with records to write)"
                        if summary.get("versions")
                        else ""
                    ),
                    "rows written: "
                    f"{totals.get('created', 0)} new, {totals.get('unchanged', 0)} already there, "
                    f"{totals.get('conflict', 0)} conflicting",
                    ", ".join(
                        f"{key} {value['created']} new/{value['unchanged']} unchanged"
                        for key, value in sorted((summary.get("counts") or {}).items())
                    ),
                    "skipped: "
                    + (
                        ", ".join(
                            f"{key} {value}"
                            for key, value in sorted((summary.get("skipped") or {}).items())
                        )
                        or "nothing"
                    ),
                    "a conflict is reported, never resolved by force: `drillintel knowledge conflicts`",
                ],
            )
            return 1 if totals.get("conflict") else 0

        if args.action == "summary":
            # A well scope is accepted here because ``record_summary`` answers it: the flag exists on the
            # parser, so refusing it after parsing would be a CLI that argues with itself.
            scope = _scope(args, workspace)
            with workspace.database.session() as session:
                payload = OperationsRepository(session).record_summary(**scope)
            npt = payload.get("npt") or {}
            _emit(
                payload,
                as_json=args.json,
                lines=[
                    "scope: "
                    + (
                        ", ".join(f"{key}={value}" for key, value in sorted(scope.items())) or "all"
                    ),
                    "counts   "
                    + ", ".join(
                        f"{key} {payload[key]}"
                        for key in ("reports", "operations", "events", "problems")
                        if payload.get(key) is not None
                    ),
                    "npt      "
                    + (
                        f"{npt.get('rows', 0)} row(s), {npt.get('promoted', 0)} promoted, "
                        f"{npt.get('with_duration', 0)} with a duration "
                        f"({npt.get('unknown_duration', 0)} without), {npt.get('undated', 0)} undated, "
                        f"{npt.get('total_hours', 0):g} h total"
                        if npt
                        else "nothing recorded"
                    ),
                    "status   "
                    + (
                        ", ".join(
                            f"{key} {value}"
                            for key, value in sorted((payload.get("npt_by_status") or {}).items())
                        )
                        or "no rows to attribute"
                    ),
                    "by category "
                    + (
                        ", ".join(
                            f"{row.get('category')} {row.get('rows', row.get('records', 0))} row(s)"
                            f" {row.get('hours', 0):g} h"
                            for row in (payload.get("npt_by_category") or [])
                        )
                        or "none"
                    ),
                    "a duration without a basis is counted, never zeroed: `drillintel records list "
                    "--table npt` shows the rows",
                ],
            )
            return 0

        # ``records list`` (the default): one query, one table, the filters that table understands.
        scope = _scope(args, workspace)
        with workspace.database.session() as session:
            repository = OperationsRepository(session)
            rows = _list_records(repository, args, scope)
        columns = _LIST_COLUMNS.get(args.table or "npt", _LIST_COLUMNS["npt"])
        payload = [_picked(row, columns) for row in rows]
        _emit(
            {"table": args.table or "npt", "scope": scope, "count": len(payload), "rows": payload},
            as_json=args.json,
            lines=[
                f"{len(payload)} {args.table or 'npt'} row(s)",
                *(_table(payload, columns) or ["nothing recorded"]),
            ],
        )
        return 0
    finally:
        workspace.close()


#: Which columns each operational table is listed with.  A screen cannot show forty, and the ones chosen
#: here are the ones that decide whether a row is trustworthy: what it is, when, how much, and whether a
#: person has confirmed it or explained its cause.
_LIST_COLUMNS: dict[str, list[tuple[str, int]]] = {
    "report": [
        ("id", 30),
        ("report_number", 12),
        ("report_date", 12),
        ("report_date_text", 18),
        ("status", 12),
    ],
    "operation": [
        ("id", 30),
        ("operation_type", 16),
        ("started_at", 17),
        ("ended_at", 17),
        ("depth_md_value", 12),
        ("status", 12),
    ],
    "event": [
        ("id", 30),
        ("event_type", 18),
        ("category", 12),
        ("occurred_at", 17),
        ("severity_level", 12),
        ("status", 12),
    ],
    "npt": [
        ("id", 30),
        ("category", 16),
        ("started_at", 17),
        ("duration_hours", 12),
        ("duration_basis", 10),
        ("root_cause_status", 14),
        ("status", 12),
    ],
    "problem": [
        ("id", 30),
        ("problem_type", 18),
        ("occurred_at", 17),
        ("immediate_cause_status", 14),
        ("root_cause_status", 14),
        ("status", 12),
    ],
}


def _list_records(repository: Any, args: argparse.Namespace, scope: dict[str, str]) -> list[Any]:
    """The one call that lists any of the five tables, with only the filters that table supports."""
    table = args.table or "npt"
    common: dict[str, Any] = {"limit": args.limit, "status": args.status or ""}
    if table == "report":
        return repository.list_reports(since=args.since, until=args.until, **common, **scope)
    if table == "operation":
        # Operations are filed per well, so a field or project scope is read as "each well in that scope"
        # - and the wells come from the field view, which is the one place in this codebase that decides
        # what "the wells of a field" means.  Re-deriving it here would be a second answer.
        if scope.get("well_id"):
            return repository.list_operations(well_id=scope["well_id"], **common)
        from ..intelligence.field import FieldIntelligence

        numbers = FieldIntelligence(repository.session).wells(**scope)
        return [
            item
            for row in numbers["wells"]
            for item in repository.list_operations(well_id=str(row["id"]), **common)
        ]
    if table == "event":
        return repository.list_events(
            since=args.since,
            until=args.until,
            category=args.category or "",
            **common,
            **scope,
        )
    if table == "npt":
        return repository.list_npt(
            since=args.since,
            until=args.until,
            category=args.category or "",
            root_cause_status=args.cause or "",
            **common,
            **scope,
        )
    return repository.list_problems(
        problem_type=args.category or "", root_cause_status=args.cause or "", **common, **scope
    )


def command_timeline(args: argparse.Namespace) -> int:
    """``drillintel timeline``: one well's history, in the order the records state it.

    Derived on every call and never stored, which is the only way it stays true after the next ingest.  A
    record with no date is listed at the end with the wording its source used: a missing date is a fact
    about the data, and reporting it as one is more useful than inventing a day for it.  ``--since``/
    ``--until`` answer with what can be placed inside them, and ``--include-undated`` brings the rest back
    when the question is "what is missing" rather than "what happened".
    """
    workspace = _open_workspace(args)
    try:
        from ..intelligence.service import IntelligenceService

        scope = _scope(args, workspace)
        service = IntelligenceService.for_workspace(workspace)
        entries = service.timeline(
            well_id=scope.get("well_id", ""),
            field_id=scope.get("field_id", ""),
            project_id=scope.get("project_id", ""),
            kinds=tuple(args.kind or ()),
            since=args.since,
            until=args.until,
            include_undated=args.include_undated,
            limit=args.limit,
        )
        payload = [entry.to_dict() for entry in entries]
        lines = [
            (entry.at.isoformat(sep=" ", timespec="minutes") if entry.at else "(undated)").ljust(17)
            + f" {entry.kind:<10} {entry.title}"
            + (f" - {entry.detail}" if entry.detail else "")
            for entry in entries
        ]
        _emit(
            {"scope": scope, "count": len(payload), "entries": payload},
            as_json=args.json,
            lines=[f"{len(payload)} record(s)", *(lines or ["nothing recorded for this scope"])],
        )
        return 0
    finally:
        workspace.close()


def command_fields(args: argparse.Namespace) -> int:
    """``drillintel fields``: the field's numbers, and what it resembles elsewhere.

    ``list`` is the index of what the workspace knows; ``summary`` is the answer to "how did this field
    go" (hours, records, occurrences, affected wells); ``offsets`` ranks other wells by the attributes
    their records actually share - problem types and hole sizes, not a similarity score out of a model.
    The numbers are grouped in SQL over the rows the timeline reads, so there is no running total here to
    go stale.
    """
    from ..intelligence.service import IntelligenceService

    if args.action == "list":
        workspace = _open_workspace(args)
        try:
            with workspace.database.session() as session:
                rows = _field_rows(session)
            _emit(
                {"count": len(rows), "fields": rows},
                as_json=args.json,
                lines=[
                    f"{len(rows)} field(s) in this workspace",
                    *(
                        _table(
                            rows,
                            [("name", 24), ("wells", 6), ("id", 38)],
                        )
                        or ["none yet: `drillintel ingest` associates documents with a well"]
                    ),
                ],
            )
            return 0
        finally:
            workspace.close()

    workspace = _open_workspace(args)
    try:
        service = IntelligenceService.for_workspace(workspace)
        with workspace.database.session() as session:
            if args.action == "offsets":
                well = _resolve_well_id(workspace, args.well or "")
                if not well:
                    raise DrillingIntelligenceError(
                        f"no well matches {args.well!r}", hint="pass --well <name or id>"
                    )
                rows = service.offsets(
                    well, same_field_only=not args.everywhere, limit=args.limit, session=session
                )
                _emit(
                    {"well_id": well, "count": len(rows), "offsets": rows},
                    as_json=args.json,
                    lines=[
                        f"{len(rows)} offset candidate(s) for {args.well}",
                        *(
                            _table(
                                rows,
                                [
                                    ("name", 16),
                                    ("problems", 9),
                                    ("npt_hours", 10),
                                    ("shared_problem_types", 30),
                                ],
                            )
                            or ["no other well shares a recorded problem or hole size with it"]
                        ),
                        "ranked on recorded attributes; the rows behind each one are the argument",
                    ],
                )
                return 0
            scope = _scope(args, workspace, allow_well=False, session=session)
            payload = service.summary(
                field_id=scope.get("field_id", ""),
                project_id=scope.get("project_id", ""),
                since=args.since,
                until=args.until,
                session=session,
            )
        categories = payload.get("npt_by_category") or {}
        problems = payload.get("problem_types") or {}
        _emit(
            payload,
            as_json=args.json,
            lines=[
                f"field: {payload.get('field') or '-'} ({payload.get('wells', 0)} well(s))",
                f"non-productive time: {payload.get('npt_hours', 0):g} h over {payload.get('npt_rows', 0)} "
                f"record(s); {payload.get('npt_undated', 0)} undated, "
                f"{payload.get('npt_unknown_duration', 0)} without a duration",
                "by category: "
                + (
                    ", ".join(
                        f"{key} {value['hours']:g} h in {value['records']} row(s) on {value['wells']} well(s)"
                        for key, value in sorted(categories.items())
                    )
                    or "none"
                ),
                f"problems: {payload.get('problems', 0)} occurrence(s) of "
                + ", ".join(
                    f"{key} x{value['occurrences']} ({value['wells']} well(s))"
                    for key, value in sorted(problems.items())
                )
                or "none",
                f"events: {payload.get('events', 0)}, lessons: {payload.get('lessons', 0)}, "
                f"reports: {payload.get('reports', 0)}",
                "hours are summed per record: if two files describe one event, both are counted, and the "
                "record that says so is `drillintel records list --table npt`",
            ],
        )
        return 0
    finally:
        workspace.close()


def _field_rows(session: Session) -> list[dict[str, Any]]:
    """Every field with its well count: the list a ``--field`` flag is chosen from."""
    from sqlalchemy import func

    from ..database.models import Field
    from ..database.models import Well as WellModel

    counts = dict(
        session.execute(select(WellModel.field_id, func.count()).group_by(WellModel.field_id)).all()
    )
    return [
        {
            "id": row.id,
            "name": row.name,
            "project_id": row.project_id,
            "wells": int(counts.get(row.id, 0)),
            "created_at": row.created_at,
        }
        for row in session.execute(select(Field).order_by(Field.name, Field.id)).scalars()
    ]


def command_patterns(args: argparse.Namespace) -> int:
    """``drillintel patterns``: what recurs, and the snapshot a person can review.

    ``find`` answers from the rows, every time, and reports nothing as stable.  ``snapshot`` is the only
    write in this package: it stores the grouping *with the query that produced it*, so ``stale`` can
    re-run that query and report what moved instead of quietly re-deriving a figure somebody accepted.  A
    pattern needs at least two wells to be a pattern at all - one well had one bad day - and that threshold
    is a flag rather than a hidden default because the right number depends on how many wells a field has.
    """
    workspace = _open_workspace(args)
    try:
        from ..intelligence.service import IntelligenceService

        service = IntelligenceService.for_workspace(workspace)
        with workspace.database.session() as session:
            if args.action in {"stale", "confirm", "recommend"}:
                if args.action == "stale":
                    report = service.pattern_staleness(args.pattern, session=session)
                    session.commit()
                    differences = report.get("differences") or {}
                    _emit(
                        report,
                        as_json=args.json,
                        lines=[
                            (
                                "the numbers moved: "
                                + ", ".join(
                                    f"{key} {value['stored']} -> {value['now']}"
                                    for key, value in sorted(differences.items())
                                )
                                if report.get("stale")
                                else "the snapshot still matches the records"
                            ),
                            "the stored row is not rewritten - re-snapshot once a person has read this",
                        ],
                    )
                    return 1 if report.get("stale") else 0
                if args.action == "confirm":
                    if not args.by:
                        raise DrillingIntelligenceError(
                            "a decision on a pattern has to be attributed",
                            hint="pass --by <who reviewed it>",
                        )
                    row = service.confirm_pattern(
                        args.pattern,
                        args.status,
                        by=args.by,
                        reason=args.reason or "",
                        session=session,
                    )
                    session.commit()
                    _emit(
                        _picked(row),
                        as_json=args.json,
                        lines=[f"pattern {row.id} -> {row.status}"],
                    )
                    return 0
                if not args.statement:
                    raise DrillingIntelligenceError(
                        "a recommendation needs its wording",
                        hint="pass --statement <what to do differently>",
                    )
                payload = service.recommend(
                    args.pattern,
                    statement=args.statement,
                    reason=args.reason or "",
                    session=session,
                )
                session.commit()
                _emit(
                    payload,
                    as_json=args.json,
                    lines=[
                        f"proposed {payload.get('id')}: {payload.get('statement')}",
                        "it stays PROPOSED until a person decides it on the record",
                    ],
                )
                return 0

            if args.action == "snapshot":
                outcome = service.snapshot_patterns(
                    min_occurrences=args.min_occurrences,
                    min_wells=args.min_wells,
                    detected_by=args.by or "cli",
                    **_scope(args, workspace, allow_well=False, session=session),
                )
                session.commit()
                _emit(
                    outcome,
                    as_json=args.json,
                    lines=[
                        f"{outcome['created']} snapshot(s) taken, {outcome['refreshed']} refreshed, from "
                        f"{outcome['candidates']} grouping(s)",
                        "read them with `drillintel patterns list`",
                    ],
                )
                return 0

            scope = _scope(args, workspace, allow_well=False, session=session)
            if args.action == "find":
                rows = service.patterns(
                    min_occurrences=args.min_occurrences,
                    min_wells=args.min_wells,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                    session=session,
                    **scope,
                )
                columns = [
                    ("problem_type", 20),
                    ("occurrence_count", 12),
                    ("well_count", 10),
                    ("total_npt_hours", 14),
                    ("last_seen_at", 20),
                ]
            else:
                rows = [
                    _picked(row, [])
                    for row in service.list_patterns(
                        status=args.status or "",
                        stale_only=args.stale_only,
                        limit=args.limit,
                        session=session,
                        **scope,
                    )
                ]
                columns = [
                    ("id", 38),
                    ("problem_type", 18),
                    ("occurrence_count", 12),
                    ("status", 12),
                    ("stale_at", 20),
                ]
            _emit(
                {"scope": scope, "count": len(rows), "patterns": rows},
                as_json=args.json,
                lines=[
                    f"{len(rows)} grouping(s)"
                    + ("" if args.action == "find" else f" for {scope.get('field_id') or scope}"),
                    *(_table(rows, columns) or ["nothing meets the threshold"]),
                    "counts of stored rows, not predictions; the query that made them is stored with them",
                ],
            )
            return 0
    finally:
        workspace.close()


def command_lessons(args: argparse.Namespace) -> int:
    """``drillintel lessons``: what the field has learned, and who said so.

    Reading is free, and approval is not offered as a flag on this command: a lesson becomes quotable only
    when a person has reviewed its evidence, and that review is worth recording as a dated event with a
    name on it - which the repository does, and a terminal shortcut would only make it easier to do
    without.  ``show`` prints the evidence pointers and the provenance count, so the reason a lesson is
    still a candidate is visible from the same command that lists it.
    """
    from ..lessons.repository import LessonRepository

    workspace = _open_workspace(args)
    try:
        with workspace.database.session() as session:
            repository = LessonRepository(session)
            if args.action == "counts":
                payload = repository.counts(
                    **_scope(args, workspace, allow_well=False, session=session)
                )
                _emit(
                    payload,
                    as_json=args.json,
                    lines=[
                        f"{key}: "
                        + ", ".join(f"{name} {value}" for name, value in sorted(group.items()))
                        for key, group in sorted(payload.items())
                        if isinstance(group, dict)
                    ]
                    or ["nothing recorded"],
                )
                return 0
            if args.action == "show":
                row = repository.get_lesson(args.lesson)
                evidence = repository.evidence(row.id)
                payload = _picked(row) | {"evidence": evidence}
                _emit(
                    payload,
                    as_json=args.json,
                    lines=[
                        f"{row.code or row.id}: {row.title or '(untitled)'} [{row.status}]",
                        f"lesson: {row.lesson or '-'}",
                        f"observation: {row.observation or '-'}",
                        f"conditions: {row.conditions or '-'}",
                        "evidence: "
                        + (
                            ", ".join(
                                f"{key} {len(value)}"
                                for key, value in sorted(evidence.items())
                                if value
                            )
                            or "none - which is why this cannot be approved"
                        ),
                        f"provenance entries: {len(row.provenance or [])}",
                    ],
                )
                return 0
            if args.action == "practices":
                rows = repository.list_practices(
                    status=args.status or "",
                    include_superseded=args.include_superseded,
                    limit=args.limit,
                    **_scope(args, workspace, allow_well=False, session=session),
                )
                columns = [
                    ("id", 30),
                    ("code", 12),
                    ("title", 34),
                    ("practice_type", 14),
                    ("status", 12),
                ]
            else:
                rows = repository.list_lessons(
                    status=args.status or "",
                    approved_only=bool(args.approved_only and not args.unapproved),
                    include_superseded=args.include_superseded,
                    search=args.search or "",
                    limit=args.limit,
                    **_scope(args, workspace, session=session),
                )
                columns = [
                    ("id", 30),
                    ("code", 12),
                    ("title", 34),
                    ("status", 12),
                    ("problem_type", 18),
                    ("revision", 9),
                ]
            payload = [_picked(row, columns) for row in rows]
        _emit(
            {"count": len(payload), "rows": payload},
            as_json=args.json,
            lines=[f"{len(payload)} row(s)", *(_table(payload, columns) or ["nothing recorded"])],
        )
        return 0
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
    ingest.add_argument(
        "--promote",
        action="store_true",
        help="after ingesting, promote report-like versions into operations, events, NPT and problems",
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

    timeline = sub.add_parser(
        "timeline",
        help="one well's history, derived from the records and never stored",
        parents=[common],
    )
    timeline.add_argument("--well", help="this well (id or name)")
    timeline.add_argument("--field", help="every well in this field (id or name)")
    timeline.add_argument("--project", help="every well in this project (id or name)")
    timeline.add_argument(
        "--kind",
        action="append",
        help="only these records (repeatable): well, program, procedure, report, operation, event, "
        "npt, problem, lesson",
    )
    timeline.add_argument("--since", help="records dated on or after this ISO date")
    timeline.add_argument("--until", help="records dated on or before this ISO date")
    timeline.add_argument(
        "--include-undated",
        dest="include_undated",
        action="store_const",
        const=True,
        default=None,
        help="with --since/--until, also list the records that carry no date",
    )
    timeline.add_argument(
        "--dated-only",
        dest="include_undated",
        action="store_const",
        const=False,
        help="even without a window, leave out the records that carry no date",
    )
    timeline.add_argument("--limit", type=int, default=0, help="at most N entries (0 = no limit)")
    timeline.set_defaults(handler=command_timeline)

    records = sub.add_parser(
        "records",
        help="the operational tables: list rows, summarise a scope, promote a version",
        parents=[common],
    )
    records_sub = records.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("list", "the rows a promotion wrote, one table at a time"),
        ("summary", "how many rows of each kind a scope holds, and how many are promoted"),
        ("promote", "turn a document version's tables into operations, events, NPT and problems"),
    ):
        action = records_sub.add_parser(name, help=help_text, parents=[common])
        if name != "promote":
            action.add_argument(
                "--table",
                choices=sorted(_LIST_COLUMNS),
                default="npt",
                help="which operational table to read (default: npt)",
            )
        action.add_argument("--well", help="restrict to this well (id or name)")
        action.add_argument("--field", help="restrict to this field (id or name)")
        action.add_argument("--project", help="restrict to this project (id or name)")
        if name != "summary":
            action.add_argument("--since", help="records dated on or after this ISO date")
            action.add_argument("--until", help="records dated on or before this ISO date")
            action.add_argument("--status", help="only rows in this confirmation state")
            action.add_argument("--category", help="the category or type token, e.g. stuck_pipe")
            action.add_argument(
                "--cause", help="root-cause state: KNOWN, INFERRED, UNKNOWN, CONFLICTED"
            )
            action.add_argument("--limit", type=int, default=50, help="at most N rows (default 50)")
        if name == "promote":
            action.add_argument(
                "--document", help="promote one document id instead of a whole scope"
            )
            action.add_argument(
                "--version", help="the version of that document (default: the current one)"
            )
        action.set_defaults(handler=command_records)

    fields = sub.add_parser(
        "fields",
        help="the field's numbers: hours, occurrences, affected wells, offset candidates",
        parents=[common],
    )
    fields_sub = fields.add_subparsers(dest="action", required=True)
    fields_list = fields_sub.add_parser(
        "list", help="the fields this workspace knows", parents=[common]
    )
    fields_list.set_defaults(handler=command_fields)
    for name, help_text in (
        ("summary", "hours, records and occurrences for one field or project"),
        ("offsets", "other wells whose records say the same kind of thing happened"),
    ):
        action = fields_sub.add_parser(name, help=help_text, parents=[common])
        if name == "offsets":
            action.add_argument(
                "--well", required=True, help="the well to find offsets for (id or name)"
            )
            action.add_argument(
                "--everywhere",
                action="store_true",
                help="look beyond this field: an offset that learned something elsewhere still counts",
            )
            action.add_argument(
                "--limit", type=int, default=10, help="at most N candidates (default 10)"
            )
        else:
            action.add_argument("--field", help="this field (id or name)")
            action.add_argument("--project", help="every field in this project (id or name)")
            action.add_argument("--since", help="count records dated on or after this ISO date")
            action.add_argument("--until", help="count records dated on or before this ISO date")
        action.set_defaults(handler=command_fields)

    patterns = sub.add_parser(
        "patterns",
        help="recurring problems: find them, snapshot them, re-check a snapshot",
        parents=[common],
    )
    patterns_sub = patterns.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("find", "the groupings the records support right now"),
        ("snapshot", "store those groupings as reviewable records"),
        ("list", "the snapshots already stored"),
    ):
        action = patterns_sub.add_parser(name, help=help_text, parents=[common])
        action.add_argument("--field", help="this field (id or name)")
        action.add_argument("--project", help="every field in this project (id or name)")
        if name != "list":
            action.add_argument(
                "--min-occurrences",
                type=int,
                default=2,
                help="how many rows make a pattern (default 2)",
            )
            action.add_argument(
                "--min-wells", type=int, default=2, help="how many wells must share it (default 2)"
            )
            action.add_argument("--since", help="count records dated on or after this ISO date")
            action.add_argument("--until", help="count records dated on or before this ISO date")
            action.add_argument(
                "--limit", type=int, default=50, help="at most N groupings (default 50)"
            )
        if name == "snapshot":
            action.add_argument("--by", help="who asked for the snapshot (recorded as detected_by)")
        if name == "list":
            action.add_argument(
                "--status", help="only snapshots in this state (CANDIDATE, CONFIRMED...)"
            )
            action.add_argument(
                "--stale-only", action="store_true", help="only snapshots the records moved"
            )
            action.add_argument(
                "--limit", type=int, default=50, help="at most N snapshots (default 50)"
            )
        action.set_defaults(handler=command_patterns)
    for name, help_text in (
        ("stale", "re-run a snapshot's own query and report what moved"),
        ("confirm", "record that a person reviewed a snapshot"),
        ("recommend", "propose advice from a snapshot, for a person to decide"),
    ):
        action = patterns_sub.add_parser(name, help=help_text, parents=[common])
        action.add_argument("pattern", help="the pattern id, as `patterns list` prints it")
        if name == "confirm":
            action.add_argument(
                "--status", default="CONFIRMED", help="the state to record (default: CONFIRMED)"
            )
            action.add_argument("--by", help="who decided (required)")
            action.add_argument("--reason", help="why, when rejecting")
        if name == "recommend":
            action.add_argument("--statement", help="what to do differently (required)")
            action.add_argument("--reason", help="why, from the records that support it")
        action.set_defaults(handler=command_patterns)

    lessons = sub.add_parser(
        "lessons",
        help="lessons learned, best practices and the evidence under them",
        parents=[common],
    )
    lessons_sub = lessons.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("list", "the lessons this workspace holds, with their state"),
        ("practices", "the best practices promoted out of them"),
        ("counts", "how many lessons, practices and recommendations are in each state"),
    ):
        action = lessons_sub.add_parser(name, help=help_text, parents=[common])
        action.add_argument("--field", help="this field (id or name)")
        action.add_argument("--project", help="every field in this project (id or name)")
        if name != "counts":
            action.add_argument("--well", help="restrict to this well (id or name)")
            action.add_argument("--status", help="only rows in this lifecycle state")
            action.add_argument("--limit", type=int, default=50, help="at most N rows (default 50)")
            action.add_argument(
                "--include-superseded", action="store_true", help="show what earlier revisions said"
            )
        if name == "list":
            action.add_argument(
                "--approved-only", action="store_true", help="only lessons somebody has approved"
            )
            action.add_argument(
                "--unapproved", action="store_true", help="only the ones still waiting on a review"
            )
            action.add_argument("--search", help="substrings of the title, lesson or observation")
        action.set_defaults(handler=command_lessons)
    show = lessons_sub.add_parser(
        "show", help="one lesson, with the records behind it", parents=[common]
    )
    show.add_argument("lesson", help="the lesson id, as `lessons list` prints it")
    show.set_defaults(handler=command_lessons)

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
