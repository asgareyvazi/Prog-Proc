"""The knowledge application service: extractions in, citable facts and edges out.

This is the layer the brief means by the move from "documents" to "searchable knowledge", and it
is deliberately boring: no parsing, no inference, no model calls.  It reads the artefacts the
extraction stage already stored (``extraction.document_json["extracted_fields"]``, each entry with
its own provenance), turns each one into a typed fact with the source wording preserved, links the
facts into the graph the registry already models, and lets the conflict detector mark the
arguments.  Reading artefacts rather than files is what makes a rebuild fast, repeatable and
identical on every machine - and what makes it impossible for the knowledge layer to disagree
with what was actually read from a document, because it never re-reads one.

The rules worth reading are the attribution ones:

``record_state``
    A drilling program states an *intention* (``PLANNED``) and a report states an observation
    (``ACTUAL``).  Those are different facts about the same property, so they must never collide
    into a conflict - which is exactly what ``knowledge_item.record_state`` exists for.

subject resolution
    A document linked to a well yields well facts.  A document that is not linked but *names* a
    well yields well facts too, plus a ``DOCUMENT_MENTIONS_WELL`` edge carrying the provenance of
    the field it came from, so the attribution is visible and arguable rather than silent.  A
    document that names nothing yields facts about its own version: still cited, never pretending
    to be about a well.

a field whose provenance was lost
    Is not stored.  An unprovable number in a table of engineering facts is worse than a missing
    one, because it looks authoritative.  The field is counted in ``skipped`` and named in the
    warnings instead, and the artefact still holds it, so nothing is lost - only quarantined where
    a person can decide.  A note someone types is the exception, and the reason ``origin`` exists:
    a ``MANUAL`` fact cites its author rather than a page, and is never mixed into what a document
    was said to assert.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import func, select

from ..core.enums import (
    DocumentClassification,
    KnowledgeOrigin,
    KnowledgeRelationType,
    KnowledgeStatus,
    RecordState,
)
from ..database.models import Document, DocumentVersion, Extraction, KnowledgeItem
from ..documents.repository import DocumentRepository
from .conflicts import detect_conflicts
from .entities import (
    ENTITY_TYPES,
    EntityRef,
    KnowledgeError,
    ensure_placeholder,
    entity_spec,
    find_well_ref,
    placeholder_id,
    subject_type_for_classification,
)
from .facts import KnowledgeFact, predicate_for_field
from .repository import KnowledgeRepository

__all__ = ["KnowledgeExtractionService", "SyncResult"]

#: Classifications whose facts state an intention rather than an observation.
_PLANNED_CLASSIFICATIONS = frozenset(
    {
        DocumentClassification.DRILLING_PROGRAM.value,
        DocumentClassification.PROCEDURE.value,
        DocumentClassification.STANDARD.value,
    }
)

#: Properties that describe the *document* rather than the well it talks about.
#:
#: "When was this report written" and "which revision am I reading" are facts about a file.
#: Attributing them to the well would make two documents that disagree about their own dates look
#: like a disagreement about the well - which is how a conflict list becomes wallpaper.  The
#: extractor reports these names for a document header, so the knowledge layer keeps them there.
_DOCUMENT_OWN_PROPERTIES = frozenset(
    {
        "approved_by",
        "author",
        "date",
        "date_iso",
        "date_text",
        "document_date",
        "document_title",
        "filename",
        "page_count",
        "prepared_by",
        "revision",
        "revision_key",
        "title",
    }
)

#: Fields that name the well a document is about, when the workspace has not linked them.
_WELL_NAME_FIELDS = ("well", "well_name", "wellbore", "api")

#: Fields that describe the mud system in words, so a ``well -> mud`` edge can be drawn.
_MUD_SYSTEM_FIELDS = ("mud_type", "mud_system", "fluid_type")

#: Fields that describe a problem or event, for a ``well -> event`` edge.
_EVENT_FIELDS = ("npt_event", "event", "problem", "mechanism", "lesson_category")


@dataclass
class SyncResult:
    """One derivation pass over one version: what it wrote, skipped and warned about."""

    document_id: str = ""
    version_id: str = ""
    subject: str = ""
    fields_seen: int = 0
    facts: dict[str, int] = field(
        default_factory=lambda: {"created": 0, "updated": 0, "unchanged": 0}
    )
    skipped: list[dict[str, str]] = field(default_factory=list)
    relations: int = 0
    superseded: int = 0
    conflicts: dict[str, Any] = field(default_factory=dict)
    #: Chunks the version owns in the searchable index after this pass - document text *and*
    #: fact chunks.  Zero alongside a successful sync means no index was wired in, which is a
    #: configuration fact, not a failure.
    index_chunks: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def fact_count(self) -> int:
        return sum(self.facts.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "version_id": self.version_id,
            "subject": self.subject,
            "fields_seen": self.fields_seen,
            "facts": dict(self.facts),
            "fact_count": self.fact_count,
            "skipped": list(self.skipped)[:50],
            "relations": self.relations,
            "superseded": self.superseded,
            "conflicts": dict(self.conflicts),
            "index_chunks": self.index_chunks,
            "warnings": list(self.warnings),
        }


class KnowledgeExtractionService:
    """Derives, links, compares and - when given one - indexes knowledge for a workspace."""

    def __init__(
        self, *, database: Any, index: Any = None, settings: Any = None, refresh_index: bool = True
    ) -> None:
        if database is None:
            raise ValueError(
                "the knowledge service needs the registry database; the index is the optional part"
            )
        self.database = database
        self.settings = settings
        #: The search sidecar, when there is one.  Facts are searchable because they are indexed,
        #: and the index stays disposable: losing it costs nothing a rebuild cannot restore.
        self.index = index
        self.refresh_index = bool(refresh_index and index is not None)

    @classmethod
    def for_workspace(
        cls, workspace: Any, *, index: Any = None, refresh_index: bool = True
    ) -> KnowledgeExtractionService:
        """Wire the service to an opened workspace, defaulting to its own search index."""
        if index is None and refresh_index:
            try:
                index = workspace.search_service().index
            except Exception:  # noqa: BLE001 - a workspace without a usable sidecar still gets knowledge
                index = None
        return cls(
            database=workspace.database,
            index=index,
            settings=getattr(workspace, "settings", None),
            refresh_index=refresh_index,
        )

    # -- derivation (read-only) --------------------------------------------
    def facts_for_payload(
        self,
        payload: dict[str, Any],
        *,
        document: Document,
        version: DocumentVersion,
        session: Any = None,
        subject: EntityRef | None = None,
    ) -> tuple[list[KnowledgeFact], list[str], list[dict[str, str]]]:
        """Turn a stored artefact into facts.  Writes nothing; returns ``(facts, warnings, skipped)``.

        This is the function to test when the question is "what does this document assert", and it
        is why re-deriving knowledge cannot drift: the input is the artefact's own
        ``extracted_fields``, provenance included.
        """
        entries = [dict(entry) for entry in ((payload or {}).get("extracted_fields") or [])]
        warnings: list[str] = []
        skipped: list[dict[str, str]] = []
        chosen = subject or self.subject_for(
            document=document, version=version, entries=entries, session=session
        )
        planned = str(document.classification) in _PLANNED_CLASSIFICATIONS
        record_state = RecordState.PLANNED.value if planned else RecordState.ACTUAL.value
        facts: list[KnowledgeFact] = []
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            if not str(entry.get("value") or "").strip() and str(
                entry.get("quality") or ""
            ).upper() in {"MISSING", ""}:
                skipped.append(
                    {"field": name, "reason": "the extractor recorded this field as missing"}
                )
                continue
            if not entry.get("provenance"):
                # Not stored, but not swallowed either: an unprovable value stays out of the
                # authoritative knowledge table (every source-derived fact cites a source) and the
                # report says which field it was and why.  The artefact still holds it, so nothing
                # is lost - only quarantined, where a person can decide.
                warnings.append(
                    f"field {name!r} has no provenance in the artefact; not stored as knowledge"
                )
                skipped.append({"field": name, "reason": "no provenance recorded for this field"})
                continue
            # A property of the file never becomes a property of the well, even in a well-linked
            # document - see ``_DOCUMENT_OWN_PROPERTIES``.
            entry_subject = (
                chosen
                if predicate_for_field(name)[0] not in _DOCUMENT_OWN_PROPERTIES
                else EntityRef("document_version", str(version.id), label=str(document.filename))
            )
            facts.append(
                KnowledgeFact.from_field(
                    entry,
                    subject=entry_subject,
                    origin=KnowledgeOrigin.EXTRACTED.value,
                    record_state=record_state,
                    document_id=document.id,
                    document_version_id=version.id,
                    project_id=document.project_id or "",
                    valid_from=document.document_date,
                )
            )
        return facts, warnings, skipped

    def subject_for(
        self,
        *,
        document: Document,
        version: DocumentVersion,
        entries: Iterable[dict[str, Any]] = (),
        session: Any = None,
    ) -> EntityRef:
        """What this document's facts are about, decided by lookup rather than invention.

        Three cases, in order of how much the platform is allowed to claim:

        *   the document is filed under a well - the facts are about **that well**;
        *   it is not filed but names a well in a field - still the well, and the sync writes a
            ``DOCUMENT_MENTIONS_WELL`` edge with that field's provenance so the inference is
            traceable instead of silent;
        *   it names nothing - the facts are about the entity type this kind of document describes
            (a lesson learned for a lessons file, a drilling parameter for a mud report), keyed to
            this version so "what does revision 3 assert" stays answerable.  A file with no
            classification falls back to the version itself rather than to a vague generic bucket.
        """
        owned = self._well_ref(str(document.well_id), session=session) if document.well_id else None
        if owned is not None:
            return owned
        named = _well_name(entries)
        if named:
            found = self._find_well(named, session=session)
            if found is not None:
                return found
        label = str(document.title or document.filename or version.id)
        described = subject_type_for_classification(document.classification)
        if described == "document_version" or entity_spec(described).table:
            # Either no classification of this kind is registered, or the type has a table of its
            # own - and a table-backed subject must already exist, which for a document version it
            # does.  Nothing is invented here.
            return EntityRef("document_version", str(version.id), label=str(document.filename))
        return EntityRef(
            described,
            placeholder_id(scope_id=str(version.id), entity_type=described, label=label),
            label=label,
        )

    def ensure_subjects(
        self, session: Any, facts: Sequence[KnowledgeFact], *, document: Document
    ) -> None:
        """Create the placeholder rows the derived subjects point at.

        Called from the write path only: :meth:`facts_for_payload` stays a pure function of the
        artefact, which is what lets a test ask "what would this document assert" without a
        database, and what makes a rebuild's answer depend on the artefact rather than on whether
        some table happened to be open.
        """
        seen: set[tuple[str, str]] = set()
        for fact in facts:
            key = (fact.subject.entity_type, fact.subject.entity_id)
            if key in seen or entity_spec(fact.subject.entity_type).table:
                continue
            seen.add(key)
            ensure_placeholder(
                session,
                entity_type=fact.subject.entity_type,
                label=fact.subject.label or str(document.filename),
                entity_id=fact.subject.entity_id,
                origin=KnowledgeOrigin.EXTRACTED.value,
            )

    def _well_ref(self, well_id: str, *, session: Any = None) -> EntityRef | None:
        from ..database.models import Well

        if session is not None:
            row = session.get(Well, str(well_id))
            return EntityRef("well", str(row.id), label=str(row.name)) if row is not None else None
        with self.database.session() as own:
            row = own.get(Well, str(well_id))
            return EntityRef("well", str(row.id), label=str(row.name)) if row is not None else None

    def _find_well(self, name_or_id: str, *, session: Any = None) -> EntityRef | None:
        if str(name_or_id).startswith("well-"):
            direct = self._well_ref(str(name_or_id), session=session)
            if direct is not None:
                return direct
        if session is not None:
            return find_well_ref(session, name_or_id)
        with self.database.session() as own:
            return find_well_ref(own, name_or_id)

    # -- writes -------------------------------------------------------------
    def sync_version(
        self, document_id: str, version_id: str, *, session: Any = None, detect: bool = True
    ) -> SyncResult:
        """Derive one version's facts, link them, and compare them with everything else.

        Idempotent by construction: re-running over an unchanged artefact reports everything
        ``unchanged`` and writes no duplicates, because a fact's identity is (version, subject +
        property + record state, source wording) rather than a row counter.

        ``session`` is for callers that already own a transaction - the ingestion pipeline, above
        all, whose rows for this version are not committed yet.  Handing the work to a second
        session there would read "no artefact yet" and index nothing while reporting success, so
        the caller keeps control of the commit and this method keeps control of the ordering.
        """
        result = SyncResult(document_id=document_id, version_id=version_id)
        with self._write_session(session) as active:
            owns_session = active is not session
            documents = DocumentRepository(active)
            repository = KnowledgeRepository(active, documents=documents)
            document = documents.get(document_id)
            version = documents.version(version_id)
            if document is None or version is None or version.document_id != document.id:
                raise KnowledgeError(
                    f"cannot derive knowledge for {document_id!r}/{version_id!r}: not a version of that document",
                    hint="the registry is the authority; `drillintel ingest` re-registers what is on disk",
                )
            extraction = documents.extraction_for_version(version.id)
            if extraction is None or not extraction.document_json:
                result.subject = "none"
                result.warnings.append(
                    "this version has no stored artefact to derive facts from; ingest or reprocess it first"
                )
                return result
            payload = dict(extraction.document_json)
            facts, warnings, unproven = self.facts_for_payload(
                payload, document=document, version=version, session=active
            )
            self.ensure_subjects(active, facts, document=document)
            result.fields_seen = len(list(payload.get("extracted_fields") or []))
            result.subject = facts[0].subject.key() if facts else "no citable fields"
            tally = {"created": 0, "updated": 0, "unchanged": 0}
            touched_keys: list[str] = []
            result.skipped.extend(unproven)
            stated_revision = max(1, int(version.version_number or 1))
            for original in facts:
                # "Which revision asserted this" is what the read paths sort on and what the
                # supersede rule acts on, so it is stamped here from the registry's own version
                # number rather than left to whatever the payload happened to carry.
                fact = replace(original, revision=stated_revision)
                if not fact.original_value.strip():
                    result.skipped.append(
                        {"field": fact.predicate, "reason": "no value in the source field"}
                    )
                    continue
                row, action = repository.put_fact(fact)
                tally[
                    {"CREATED": "created", "UPDATED": "updated", "UNCHANGED": "unchanged"}[action]
                ] += 1
                touched_keys.append(str(row.lookup_key or ""))
                repository.link(
                    source=EntityRef("document_version", version.id, label=document.filename),
                    relation=KnowledgeRelationType.VERSION_CONTAINS_KNOWLEDGE.value,
                    target=EntityRef("engineering_fact", str(row.id)),
                    provenance=[fact.provenance.to_dict()] if fact.provenance is not None else None,
                    note=f"{fact.predicate} read from this revision",
                )
                result.relations += 1
            result.facts = tally
            result.relations += self._structural_links(
                repository, document=document, version=version, facts=facts
            )
            result.superseded = repository.supersede_previous_versions(
                document_id=document.id, version_id=version.id
            )
            if detect and touched_keys:
                report = detect_conflicts(
                    repository, keys=sorted(key for key in touched_keys if key)
                )
                result.conflicts = report.to_dict()
            result.warnings.extend(warnings)
            if owns_session:
                active.commit()
        if self.refresh_index and owns_session:
            result.index_chunks = self._index_version(document_id, version_id, result)
        return result

    @contextmanager
    def _write_session(self, session: Any | None) -> Iterator[Any]:
        """Use the caller's session when there is one, and own the transaction when there is not."""
        if session is not None:
            yield session
            return
        with self.database.session() as own:
            yield own
            own.commit()

    def _structural_links(
        self,
        repository: KnowledgeRepository,
        *,
        document: Document,
        version: DocumentVersion,
        facts: Sequence[KnowledgeFact],
    ) -> int:
        """The edges that make the graph a graph: well ↔ document, well → mud, well → event.

        Each is either registry structure (a document already filed under a well) or is written
        with the provenance of the field that justified it, so "Well A-3 has mud system WBM" can be
        opened down to a cell.
        """
        if not facts:
            return 0
        first = facts[0]
        by_predicate = {str(fact.predicate): fact for fact in facts}
        provenance = [first.provenance.to_dict()] if first.provenance is not None else None
        written = 0
        if first.well_id and document.well_id:
            repository.link(
                source=EntityRef("well", str(document.well_id), label=first.subject.label),
                relation=KnowledgeRelationType.WELL_HAS_DOCUMENT.value,
                target=EntityRef("document", document.id, label=document.filename),
                note="the workspace holds this document under this well",
            )
            written += 1
        if first.well_id and not document.well_id:
            repository.link(
                source=EntityRef("document", document.id, label=document.filename),
                relation=KnowledgeRelationType.DOCUMENT_MENTIONS_WELL.value,
                target=EntityRef("well", first.well_id, label=first.subject.label),
                provenance=provenance,
                note="attributed to this well from a field in the document, not from the folder layout",
            )
            written += 1
        for names, entity, relation, note in (
            (
                _MUD_SYSTEM_FIELDS,
                "mud",
                KnowledgeRelationType.WELL_HAS_MUD,
                "the mud system named in this report",
            ),
            (
                _EVENT_FIELDS,
                "problem",
                KnowledgeRelationType.WELL_ENCOUNTERED_EVENT,
                "an event recorded against this well",
            ),
        ):
            label = _first_text(by_predicate, names)
            if not label or not first.well_id:
                continue
            target = ensure_placeholder(
                repository.session,
                entity_type=entity,
                label=label,
                origin=KnowledgeOrigin.EXTRACTED.value,
            )
            repository.link(
                source=EntityRef("well", first.well_id, label=first.subject.label),
                relation=relation.value,
                target=target,
                provenance=provenance,
                note=note,
            )
            written += 1
        return written

    def _index_version(self, document_id: str, version_id: str, result: SyncResult) -> int:
        """Rewrite one version's chunk set, so the fact chunks land beside the document's.

        A failing sidecar is a warning, not a rollback: the facts are already committed and remain
        queryable through the database, which is the authoritative store by design.
        """
        try:
            # A session of our own, opened after the facts were committed: the index reads the
            # registry, and passing it the writer's session would index a snapshot that may not be
            # visible to the sidecar's connection.
            with self.database.session() as session:
                return int(
                    self.index.upsert(
                        document_id, version_id, repository=DocumentRepository(session)
                    )
                    or 0
                )
        except Exception as exc:  # noqa: BLE001 - the index is disposable; the registry is not
            result.warnings.append(f"search index not refreshed: {type(exc).__name__}: {exc}")
            return 0

    # -- batches ------------------------------------------------------------
    def sync_all(
        self,
        *,
        workspace_id: str | None = None,
        well_id: str | None = None,
        limit: int | None = None,
        session: Any = None,
    ) -> dict[str, Any]:
        """Derive knowledge for every current version, then compare everything with everything.

        ``session`` runs the whole pass inside a caller's transaction (the desktop UI holds one; a
        test needs one).  Without it the service owns its sessions and commits them, which is what
        the CLI wants: one command, one durable result.
        """
        with self._write_session(session) as scoped:
            pairs = self._current_version_pairs(
                scoped, workspace_id=workspace_id, well_id=well_id, limit=limit
            )
        totals = {"created": 0, "updated": 0, "unchanged": 0}
        relations = 0
        warnings: list[str] = []
        results: list[dict[str, Any]] = []
        for document_id, version_id in pairs:
            result = self.sync_version(document_id, version_id, session=session)
            for key, value in result.facts.items():
                totals[key] += value
            relations += result.relations
            warnings.extend(result.warnings)
            results.append(result.to_dict())
        with self._write_session(session) as scoped:
            report = detect_conflicts(KnowledgeRepository(scoped))
        return {
            "versions": len(pairs),
            "facts": totals,
            "relations": relations,
            "skipped_fields": sum(len(item["skipped"]) for item in results),
            "conflicts": report.to_dict(),
            "warnings": warnings[:50],
            "results": results,
        }

    def rebuild(
        self,
        *,
        workspace_id: str | None = None,
        well_id: str | None = None,
        session: Any = None,
    ) -> dict[str, Any]:
        """Throw away what extraction produced and derive it again from the stored artefacts.

        Only ``EXTRACTED`` rows are removed - a note a person typed survives, which is the
        difference between a repair command and a data-loss command.  Facts whose document is gone
        disappear here rather than lingering as orphans, and the search index is rewritten from the
        same authoritative data.
        """
        with self._write_session(session) as scoped:
            removed = KnowledgeRepository(scoped).delete_derived(workspace_id=workspace_id)
        summary = self.sync_all(workspace_id=workspace_id, well_id=well_id, session=session)
        summary["removed"] = removed
        return summary

    # -- reporting ----------------------------------------------------------
    def status(self, *, workspace_id: str | None = None, session: Any = None) -> dict[str, Any]:
        """What knowledge exists, and whether it is behind the registry.

        Reads only.  ``session`` is accepted so a caller can ask about rows it has written but not
        committed - which is the difference between a UI that shows its own edits and one that shows
        a stale answer until something else commits.
        """
        with self._write_session(session) as scoped:
            repository = KnowledgeRepository(scoped)
            counts = repository.counts(workspace_id=workspace_id)
            counts.update(self._staleness(scoped, workspace_id=workspace_id))
            # The recommendation has to be actionable, so it fires on the two cases a rebuild
            # actually fixes: nothing was ever derived, or derived rows point at versions the
            # registry no longer considers current.  A version with *no* facts is not on that list
            # by itself - a scanned page with no fields in it is a correct result, and a doctor that
            # cries wolf is a doctor nobody runs.
            counts["needs_rebuild"] = bool(
                counts["detached_facts"]
                or (not counts["facts"] and counts["versions_with_artefacts"])
            )
        if self.index is not None:
            try:
                raw = self.index.stats()
                stats = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
                counts["index"] = {
                    key: stats.get(key)
                    for key in ("chunks", "knowledge_chunks", "documents", "versions")
                }
            except Exception as exc:  # noqa: BLE001 - a broken sidecar must not hide the registry's answer
                counts["index"] = {"error": f"{type(exc).__name__}: {exc}"}
        return counts

    def conflicts(
        self, *, well_id: str = "", status: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """The arguments on the table, with both sides and the ranking basis.

        ``status=None`` means the open ones - spelling the default out because "show me the
        conflicts" and "show me everything we ever argued about" are different requests, and a
        resolved row is history worth keeping.
        """
        from ..core.enums import ConflictResolution

        wanted = status if status is not None else ConflictResolution.OPEN.value
        with self.database.session() as session:
            repository = KnowledgeRepository(session)
            rows = repository.conflicts(well_id=well_id, status=wanted, limit=limit)
            return {
                "count": len(rows),
                "status_filter": wanted,
                "conflicts": [_conflict_dict(row) for row in rows],
            }

    def resolve(
        self,
        conflict_id: str,
        *,
        chosen_item_id: str,
        note: str = "",
        by: str = "operator",
        session: Any = None,
    ) -> dict[str, Any]:
        """Record a decision on one conflict, and re-compare the key it was about.

        Deciding is a human act and is written down as one: who chose, what they chose, what the
        other side said at that moment, and the note.  The losing facts are ``RETIRED``, never
        deleted, so a later revision can see what was known and when it was given up.
        """
        from ..core.enums import ConflictResolution
        from .conflicts import resolve_conflict

        with self._write_session(session) as active:
            documents = DocumentRepository(active)
            repository = KnowledgeRepository(active, documents=documents)
            conflict = resolve_conflict(
                repository,
                conflict_id,
                chosen_item_id=chosen_item_id,
                resolution=ConflictResolution.RESOLVED_MANUALLY.value,
                by=by,
                note=note,
            )
            # Re-compare the key now that one side is retired: a resolved conflict must not keep
            # its neighbours flagged, and "did the marking catch up?" is answered here rather than
            # left for the next rebuild to notice.
            report = detect_conflicts(repository, keys=[str(conflict.lookup_key or "")])
            documents.audit(
                action="knowledge.conflict_resolved",
                subject_type="knowledge_conflict",
                subject_id=str(conflict.id),
                detail={
                    "chosen_item_id": chosen_item_id,
                    "note": note,
                    "lookup_key": str(conflict.lookup_key or ""),
                    "candidates": len(conflict.candidates or []),
                },
                actor=by,
            )
            # The decision and its audit event are one thing: a session that reads the trail before
            # committing (the CLI prints the outcome, the UI reloads the conflict) must see both.
            active.flush()
            payload = {
                "conflict_id": str(conflict.id),
                "status": str(conflict.status),
                "lookup_key": str(conflict.lookup_key or ""),
                "chosen_item_id": chosen_item_id,
                "resolution": dict(conflict.resolution or {}),
                "recheck": report.to_dict(),
            }
        return payload

    def facts(
        self,
        *,
        well: str = "",
        entity: str = "",
        document_id: str = "",
        predicate: str = "",
        limit: int = 50,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        """The well-centric helpers (§10) behind one listing."""
        with self.database.session() as session:
            repository = KnowledgeRepository(session)
            filters: dict[str, Any] = {
                "predicate": predicate,
                "limit": limit,
                "include_superseded": include_superseded,
            }
            if well:
                ref = find_well_ref(session, well)
                if ref is None:
                    raise KnowledgeError(
                        f"no well matches {well!r} in this workspace",
                        hint="`drillintel knowledge facts` with no --well lists every fact that was derived",
                    )
                facts = repository.facts_for_well(ref.entity_id, **filters)
                scope = f"well {ref.label or ref.entity_id}"
            elif entity:
                entity_type, _, entity_id = str(entity).partition(":")
                ref = EntityRef(entity_type, entity_id)
                facts = repository.facts_for_entity(ref, **filters)
                scope = f"entity {ref.key()}"
            elif document_id:
                facts = repository.facts_for_document(document_id, **filters)
                scope = f"document {document_id}"
            else:
                facts = [
                    KnowledgeFact.from_item(row)
                    for row in self._all_rows(
                        session, limit=limit, include_superseded=include_superseded
                    )
                ]
                scope = "workspace"
            return {
                "scope": scope,
                "count": len(facts),
                "facts": [
                    fact.to_dict()
                    | {"item_id": fact.item_id or _fact_id_for(fact), "citation": fact.citation()}
                    for fact in facts
                ],
            }

    # -- internals ----------------------------------------------------------
    def _current_version_pairs(
        self, session: Any, *, workspace_id: str | None, well_id: str | None, limit: int | None
    ) -> list[tuple[str, str]]:
        statement = (
            select(DocumentVersion.document_id, DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.is_current.is_(True))
            .order_by(Document.identity_path, DocumentVersion.version_number)
        )
        if workspace_id:
            statement = statement.where(Document.workspace_id == workspace_id)
        if well_id:
            statement = statement.where(Document.well_id == well_id)
        if limit:
            statement = statement.limit(max(1, int(limit)))
        return [
            (str(document_id), str(version_id))
            for document_id, version_id in session.execute(statement).all()
        ]

    def _all_rows(
        self, session: Any, *, limit: int, include_superseded: bool
    ) -> list[KnowledgeItem]:
        statement = (
            select(KnowledgeItem)
            .where(KnowledgeItem.origin == KnowledgeOrigin.EXTRACTED.value)
            .order_by(KnowledgeItem.updated_at.desc(), KnowledgeItem.id)
        )
        if not include_superseded:
            statement = statement.where(KnowledgeItem.status != KnowledgeStatus.SUPERSEDED.value)
        return list(session.execute(statement.limit(max(1, int(limit)))).scalars())

    def _staleness(self, session: Any, workspace_id: str | None) -> dict[str, Any]:
        """Two counts that answer "is the knowledge behind the documents?", without a stamp table.

        ``versions_without_knowledge`` is a current version that has an artefact but no derived
        facts (the "never built, or built before this layer existed" case).  ``detached_facts`` is
        a fact that still claims to be current while its version is no longer the current one -
        what an interrupted supersede chain looks like from the outside.  Both are bounded queries
        over indexed columns, because ``status`` has to stay cheap on a large workspace.
        """
        current = select(DocumentVersion.id).where(DocumentVersion.is_current.is_(True))
        if workspace_id:
            scoped_documents = select(Document.id).where(Document.workspace_id == workspace_id)
            current = current.where(DocumentVersion.document_id.in_(scoped_documents))
        derived = (
            select(KnowledgeItem.document_version_id)
            .where(KnowledgeItem.origin == KnowledgeOrigin.EXTRACTED.value)
            .distinct()
        )
        with_artefact = select(Extraction.document_version_id).where(
            Extraction.document_version_id.in_(current)
        )
        artefacts = int(
            session.execute(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.id.in_(with_artefact))
            ).scalar_one()
        )
        missing = int(
            session.execute(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.id.in_(with_artefact), DocumentVersion.id.notin_(derived))
            ).scalar_one()
        )
        detached = int(
            session.execute(
                select(func.count())
                .select_from(KnowledgeItem)
                .where(
                    KnowledgeItem.origin == KnowledgeOrigin.EXTRACTED.value,
                    KnowledgeItem.status != KnowledgeStatus.SUPERSEDED.value,
                    KnowledgeItem.document_version_id.notin_(current),
                )
            ).scalar_one()
        )
        return {
            "versions_with_artefacts": artefacts,
            "versions_without_knowledge": missing,
            "detached_facts": detached,
        }


def _well_name(entries: Iterable[dict[str, Any]]) -> str:
    for entry in entries:
        if str(entry.get("name") or "").strip().casefold() in _WELL_NAME_FIELDS:
            text = str(entry.get("value") or "").strip()
            if text:
                return text
    return ""


def _first_text(facts_by_predicate: dict[str, KnowledgeFact], names: Sequence[str]) -> str:
    """The first named field that has readable text, as written by the source."""
    for name in names:
        fact = facts_by_predicate.get(
            str(name).strip().casefold().replace(" ", "_").replace("-", "_")
        )
        if fact is not None and (fact.text or fact.original_value):
            return fact.text or fact.original_value
    return ""


def _fact_id_for(fact: KnowledgeFact) -> str:
    from .repository import fact_id_for

    return fact_id_for(
        version_id=fact.document_version_id,
        lookup_key=fact.lookup_key(),
        original_value=fact.original_value,
    )


def _conflict_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "lookup_key": row.lookup_key,
        "property": row.property_name,
        "record_state": row.record_state,
        "compare_unit": row.compare_unit,
        # Which well this argument belongs to, so a listing can group by it without re-deriving the
        # subject from the lookup key.
        "well_id": str(row.well_id or ""),
        "status": row.status,
        "candidates": list(row.candidates or []),
        "resolution": dict(row.resolution or {}),
        "note": row.note or "",
    }


#: Exposed for tests and the CLI, which need the same vocabulary the service uses.
PLANNED_CLASSIFICATIONS = _PLANNED_CLASSIFICATIONS
ENTITY_VOCABULARY = tuple(sorted(ENTITY_TYPES))
