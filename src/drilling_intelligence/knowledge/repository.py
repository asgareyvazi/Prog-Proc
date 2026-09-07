"""Persistence and queries for knowledge - the layer that owns the ``knowledge_*`` tables.

Three rules shape everything here, and each one exists because the alternative was worse:

*   **Idempotence by identity, not by deletion.**  A stored fact's primary key is derived from
    (the version it was read from, the subject/property/record-state key, the source's own
    wording), so re-running extraction over an unchanged file updates nothing and creates
    nothing.  The alternative - "delete all derived facts and rewrite" - churns every id on every
    run, which would break any edge or conflict that points at a fact, and it makes an
    interrupted rebuild lose data.
*   **A different value is never an update.**  Two sources that disagree about the same property
    produce two rows with the same :meth:`~drilling_intelligence.knowledge.facts.KnowledgeFact.lookup_key`,
    because overwriting is how a conflict disappears without anybody deciding to resolve it.
    Conflict status is a *separate* write (:mod:`drilling_intelligence.knowledge.conflicts`).
*   **Only ``EXTRACTED`` rows are ever deleted.**  :meth:`KnowledgeRepository.delete_derived`
    refuses to remove what a person typed, which is what makes ``knowledge rebuild`` a command a
    user can run without fear.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import Select, and_, delete, func, not_, or_, select

from ..core.enums import ConflictResolution, KnowledgeOrigin, KnowledgeStatus
from ..database.integrity import create_knowledge_relation, find_knowledge_relation
from ..database.models import (
    Document,
    DocumentVersion,
    KnowledgeConflict,
    KnowledgeItem,
    KnowledgeRelation,
)
from ..documents.repository import DocumentRepository
from .entities import EntityRef, KnowledgeError, entity_spec
from .facts import KnowledgeFact

__all__ = ["KnowledgeRepository", "fact_id_for"]


def fact_id_for(*, version_id: str, lookup_key: str, original_value: str) -> str:
    """The deterministic id of a fact: same source wording, same row.

    The *source wording* is part of the key on purpose.  Two different values for one property
    must never collide onto one row (that would be the silent overwrite), and the same value read
    twice must collide exactly once.  A digest rather than a counter, so two machines indexing
    the same workspace agree without talking to each other.
    """
    from ..core.hashing import sha256_text

    digest = sha256_text(f"{version_id}|{lookup_key}|{original_value}")
    return f"ki-{digest[:24]}"


class KnowledgeRepository:
    """Reads and writes for facts, edges and conflicts, on one registry session."""

    def __init__(self, session: Any, *, documents: DocumentRepository | None = None) -> None:
        self.session = session
        #: Edges and sources are written through the components that already own them, so there
        #: is exactly one place that knows how a ``source`` row or a validated edge is made.
        self.documents = documents if documents is not None else DocumentRepository(session)

    # -- writes -------------------------------------------------------------
    def put_fact(self, fact: KnowledgeFact) -> tuple[KnowledgeItem, str]:
        """Store one fact; ``CREATED``, ``UPDATED`` or ``UNCHANGED`` (never a duplicate).

        The version-scoped id means the same bytes read twice are the same row.  ``UPDATED`` is
        reserved for a fact whose *evidence* changed under an identical key - re-extraction with a
        new parser version - and the value itself is recomputed by the caller, not patched here:
        a fact is replaced by a new one, never edited into a different answer.
        """
        if fact.provenance is None and fact.origin == KnowledgeOrigin.EXTRACTED.value:
            # Re-asserted here (and not only in the constructor) because a hand-built fact can
            # arrive from JSON; a stored engineering value without provenance is the failure mode
            # the whole platform exists to avoid.
            raise KnowledgeError("an extracted fact needs provenance before it can be stored")
        version_id = fact.document_version_id
        lookup_key = fact.lookup_key()
        row_id = fact_id_for(
            version_id=version_id, lookup_key=lookup_key, original_value=fact.original_value
        )
        kwargs = fact.to_item(item_id=row_id, source_id=self._source_id_for(fact))
        existing = self.session.get(KnowledgeItem, row_id)
        if existing is None:
            row = KnowledgeItem(**kwargs)
            self.session.add(row)
            self.session.flush()
            return row, "CREATED"
        before = _digest_of(existing)
        for column, value in kwargs.items():
            setattr(existing, column, value)
        self.session.flush()
        return existing, "UNCHANGED" if before == _digest_of(existing) else "UPDATED"

    def put_facts(self, facts: Iterable[KnowledgeFact]) -> dict[str, int]:
        tally = {"created": 0, "updated": 0, "unchanged": 0}
        for fact in facts:
            _row, action = self.put_fact(fact)
            tally[
                {"CREATED": "created", "UPDATED": "updated", "UNCHANGED": "unchanged"}[action]
            ] += 1
        return tally

    def manual_fact(self, fact: KnowledgeFact) -> KnowledgeItem:
        """Store a fact a person wrote, marked ``MANUAL`` so no rebuild may delete it.

        Manual is not a licence to be vague: if the note cites a document it should carry the
        provenance too, and the status follows from that rather than from whoever typed it.

        The key is re-compared afterwards.  A note that contradicts the corpus *is* an argument, and
        an argument has to be visible the moment it exists - not the next time an ingest or a rebuild
        happens to run detection - because the alternative is a workspace where two answers sit in
        the table side by side and nothing says so.
        """
        from .conflicts import detect_conflicts

        row, _action = self.put_fact(replace(fact, origin=KnowledgeOrigin.MANUAL.value))
        if row.lookup_key:
            detect_conflicts(self, keys=[str(row.lookup_key)])
        return row

    def set_status(
        self, item_id: str, *, status: str, superseded_by: str = "", note: str = ""
    ) -> bool:
        """Move one fact to a lifecycle status.  Returns whether anything changed."""
        row = self.session.get(KnowledgeItem, item_id)
        if row is None:
            return False
        try:
            # An unknown status is an error, not a typo kept: a lifecycle word nobody recognises
            # would silently disable every query that filters on it.
            wanted = KnowledgeStatus(status).value
        except ValueError as exc:
            raise KnowledgeError(
                f"unknown knowledge status {status!r}",
                hint="one of: " + ", ".join(member.value for member in KnowledgeStatus),
            ) from exc
        if row.status == wanted and (row.superseded_by or "") == superseded_by:
            return False
        row.status = wanted
        row.superseded_by = superseded_by or None
        if note:
            payload = dict(row.payload or {})
            payload["status_note"] = note
            row.payload = payload
        self.session.flush()
        return True

    def supersede_previous_versions(self, *, document_id: str, version_id: str) -> int:
        """Mark the same document's older facts SUPERSEDED, without deleting one of them.

        "Revision 13 says 12.4 ppg" stays in the database and stays queryable, because the reason
        a value changed is engineering history; what changes is which statement the platform will
        answer with.  The pointer is the version row that replaces it, so the chain can be walked
        in either direction.
        """
        # The rule is "only the newest revision answers", not "this sync wins": the version passed
        # in is where the replacement came *from*, and re-deriving an old revision (a back-fill, a
        # repair run, an out-of-order rebuild) must not move the platform's answer backwards.  So
        # the current version of the document is what is kept, and every other version's facts are
        # marked - which also makes a repeated call idempotent.
        current_id = self._current_version_id(document_id, fallback=version_id)
        previous_ids = [
            str(row[0])
            for row in self.session.execute(
                select(DocumentVersion.id)
                .where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.id != current_id,
                )
                .order_by(DocumentVersion.version_number)
            ).all()
        ]
        if not previous_ids:
            return 0
        rows = list(
            self.session.execute(
                select(KnowledgeItem).where(
                    KnowledgeItem.document_version_id.in_(previous_ids),
                    KnowledgeItem.origin == KnowledgeOrigin.EXTRACTED.value,
                    KnowledgeItem.status != KnowledgeStatus.SUPERSEDED.value,
                )
            ).scalars()
        )
        for row in rows:
            row.status = KnowledgeStatus.SUPERSEDED.value
            # ``superseded_by`` stays empty: there is no one-to-one mapping from an old fact to a
            # new one (a revision may drop a property or split it), and a pointer to the wrong row
            # is worse than none.  The replacing *version* is what is recorded, in the payload.
            payload = dict(row.payload or {})
            payload["superseded_by_version_id"] = version_id
            payload["status"] = KnowledgeStatus.SUPERSEDED.value
            row.payload = payload
        self.session.flush()
        return len(rows)

    def _current_version_id(self, document_id: str, *, fallback: str = "") -> str:
        """The version of ``document_id`` the registry considers current, or ``fallback``."""
        row = self.session.execute(
            select(DocumentVersion.id)
            .where(DocumentVersion.document_id == document_id, DocumentVersion.is_current.is_(True))
            .order_by(DocumentVersion.version_number.desc())
            .limit(1)
        ).first()
        return str(row[0]) if row is not None else str(fallback)

    def link(
        self,
        *,
        source: EntityRef,
        relation: str,
        target: EntityRef,
        provenance: Sequence[Mapping | dict] | None = None,
        note: str = "",
        weight: float = 1.0,
    ) -> KnowledgeRelation:
        """Add (or strengthen) a typed edge between two entities.

        Endpoints are given as :class:`~drilling_intelligence.knowledge.entities.EntityRef`, and
        the *relation* endpoint token of each type is applied here - which is what stops "well →
        bit" from being stored as a reference to a table that does not exist, since a bit is a
        ``knowledge_item`` row wearing a ``bit`` label.
        """
        return create_knowledge_relation(
            self.session,
            source_type=entity_spec(source.entity_type).endpoint_type,
            source_id=source.entity_id,
            relation=relation,
            target_type=entity_spec(target.entity_type).endpoint_type,
            target_id=target.entity_id,
            weight=weight,
            provenance=[dict(entry) for entry in provenance] if provenance else None,
            note=note or None,
        )

    def record_conflict(
        self,
        *,
        lookup_key: str,
        property_name: str,
        candidates: Sequence[dict[str, Any]],
        record_state: str = "",
        well_id: str = "",
        compare_unit: str = "",
        status: str = ConflictResolution.OPEN.value,
        resolution: dict[str, Any] | None = None,
        note: str = "",
    ) -> KnowledgeConflict:
        """Store a disagreement between sources, with every candidate kept.

        The id is derived from the key, so re-running detection converges instead of piling up
        duplicate rows, and ``candidates`` is the whole record of what was disagreed about - which
        is why a resolved conflict keeps its candidates rather than collapsing to one value.
        """
        from ..core.hashing import sha256_text

        row_id = f"kc-{sha256_text(f'{lookup_key}|{record_state}')[:24]}"
        payload = {
            "id": row_id,
            "lookup_key": lookup_key[:300],
            "property_name": property_name[:120],
            "record_state": record_state or "ACTUAL",
            "well_id": well_id or None,
            "candidates": list(candidates),
            "status": status,
            "resolution": resolution,
            "compare_unit": compare_unit,
            "note": note or None,
            "detected_by": "knowledge.conflicts",
        }
        existing = self.session.get(KnowledgeConflict, row_id)
        if existing is None:
            row = KnowledgeConflict(**payload)
            self.session.add(row)
            self.session.flush()
            return row
        for column, value in payload.items():
            if column == "id":
                continue
            setattr(existing, column, value)
        self.session.flush()
        return existing

    def clear_conflict(self, lookup_key: str, *, record_state: str = "") -> int:
        """Remove conflict rows whose key no longer has anything to argue about.

        Only undecided rows go.  A row carrying a human decision is the record of that decision -
        who chose, what they chose, what the other side said at the time - and detection re-runs
        for many reasons, so letting it drop a settled row would quietly delete the audit trail of
        an engineering judgement.
        """
        statement = delete(KnowledgeConflict).where(
            KnowledgeConflict.lookup_key == lookup_key[:300],
            KnowledgeConflict.status == ConflictResolution.OPEN.value,
        )
        if record_state:
            statement = statement.where(KnowledgeConflict.record_state == record_state)
        result = self.session.execute(statement)
        return int(result.rowcount or 0)

    def delete_derived(
        self, *, document_version_id: str | None = None, workspace_id: str | None = None
    ) -> int:
        """Drop what extraction produced, and only that.

        Edges whose endpoints go away with them are deleted in the same call: a relation pointing
        at a removed fact is a dangling reference the integrity checker would rightly report, and
        leaving it behind would make every rebuild look like a data-integrity incident.
        """
        statement = select(KnowledgeItem.id).where(
            KnowledgeItem.origin == KnowledgeOrigin.EXTRACTED.value
        )
        if document_version_id:
            statement = statement.where(KnowledgeItem.document_version_id == document_version_id)
        if workspace_id:
            statement = statement.where(
                KnowledgeItem.document_id.in_(
                    select(Document.id).where(Document.workspace_id == workspace_id)
                )
            )
        ids = [str(row[0]) for row in self.session.execute(statement).all()]
        if not ids:
            return 0
        edges = self.session.execute(
            select(KnowledgeRelation).where(
                or_(
                    and_(
                        KnowledgeRelation.source_type == "knowledge_item",
                        KnowledgeRelation.source_id.in_(ids),
                    ),
                    and_(
                        KnowledgeRelation.target_type == "knowledge_item",
                        KnowledgeRelation.target_id.in_(ids),
                    ),
                )
            )
        ).scalars()
        for edge in list(edges):
            self.session.delete(edge)
        rows = list(
            self.session.execute(select(KnowledgeItem).where(KnowledgeItem.id.in_(ids))).scalars()
        )
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)

    # -- reads --------------------------------------------------------------
    def get_fact(self, item_id: str) -> KnowledgeFact | None:
        row = self.session.get(KnowledgeItem, item_id)
        return KnowledgeFact.from_item(row) if row is not None else None

    def fact_row(self, item_id: str) -> KnowledgeItem | None:
        return self.session.get(KnowledgeItem, item_id)

    def facts_for_well(
        self,
        well_id: str,
        *,
        predicate: str = "",
        status: str | None = None,
        include_superseded: bool = False,
        limit: int = 200,
    ) -> list[KnowledgeFact]:
        """Every fact asserted about one well, newest statements first.

        ``status=None`` means "everything that is not superseded" rather than "everything": a
        caller who wants the history asks for it, and a caller who wants the answer does not have
        to know that superseded rows are kept at all.
        """
        statement = select(KnowledgeItem).where(KnowledgeItem.well_id == well_id)
        return self._facts(
            statement,
            predicate=predicate,
            status=status,
            include_superseded=include_superseded,
            limit=limit,
        )

    def facts_for_document(self, document_id: str, **filters: Any) -> list[KnowledgeFact]:
        """Facts read from one document, across all of its versions."""
        statement = select(KnowledgeItem).where(KnowledgeItem.document_id == document_id)
        return self._facts(statement, **filters)

    def facts_for_version(self, document_version_id: str, **filters: Any) -> list[KnowledgeFact]:
        statement = select(KnowledgeItem).where(
            KnowledgeItem.document_version_id == document_version_id
        )
        return self._facts(statement, **filters)

    def facts_for_entity(self, ref: EntityRef, **filters: Any) -> list[KnowledgeFact]:
        """Facts whose *subject* is this entity, whatever kind of entity it is."""
        statement = select(KnowledgeItem).where(
            KnowledgeItem.entity_type == entity_spec(ref.entity_type).name,
            KnowledgeItem.entity_id == ref.entity_id,
        )
        return self._facts(statement, **filters)

    def facts_by_key(
        self, lookup_key: str, *, include_superseded: bool = True
    ) -> list[KnowledgeItem]:
        """Every row claiming the same subject/property/state - the conflict detector's input."""
        statement = select(KnowledgeItem).where(KnowledgeItem.lookup_key == lookup_key)
        if not include_superseded:
            statement = statement.where(KnowledgeItem.status != KnowledgeStatus.SUPERSEDED.value)
        return list(self.session.execute(self._ordered(statement)).scalars())

    def lookup_keys(
        self, *, origin: str = KnowledgeOrigin.EXTRACTED.value, include_superseded: bool = False
    ) -> list[str]:
        """The distinct subject/property keys knowledge is currently asserted about."""
        statement = select(KnowledgeItem.lookup_key).where(
            KnowledgeItem.lookup_key.is_not(None), KnowledgeItem.origin == origin
        )
        if not include_superseded:
            statement = statement.where(KnowledgeItem.status != KnowledgeStatus.SUPERSEDED.value)
        return sorted(
            {str(row[0]) for row in self.session.execute(statement.distinct()).all() if row[0]}
        )

    def relations_for_entity(
        self, ref: EntityRef, *, direction: str = "both", limit: int = 200
    ) -> list[KnowledgeRelation]:
        """Edges touching an entity.

        ``direction`` is ``out``, ``in`` or ``both``: "what does this well have" and "what was
        derived from this document" are different questions, and a graph query that always returns
        both is how a listing turns into a wall.
        """
        endpoint = entity_spec(ref.entity_type).endpoint_type
        out = and_(
            KnowledgeRelation.source_type == endpoint, KnowledgeRelation.source_id == ref.entity_id
        )
        inbound = and_(
            KnowledgeRelation.target_type == endpoint, KnowledgeRelation.target_id == ref.entity_id
        )
        clause = {"out": out, "in": inbound}.get(direction, or_(out, inbound))
        statement = (
            select(KnowledgeRelation)
            .where(clause)
            .order_by(KnowledgeRelation.relation, KnowledgeRelation.id)
            .limit(max(1, int(limit)))
        )
        return list(self.session.execute(statement).scalars())

    def relation_exists(self, *, source: EntityRef, relation: str, target: EntityRef) -> bool:
        return (
            find_knowledge_relation(
                self.session,
                source_type=entity_spec(source.entity_type).endpoint_type,
                source_id=source.entity_id,
                relation=relation,
                target_type=entity_spec(target.entity_type).endpoint_type,
                target_id=target.entity_id,
            )
            is not None
        )

    def conflicts(
        self,
        *,
        well_id: str = "",
        status: str | None = ConflictResolution.OPEN.value,
        limit: int = 100,
    ) -> list[KnowledgeConflict]:
        statement = select(KnowledgeConflict)
        if well_id:
            statement = statement.where(KnowledgeConflict.well_id == well_id)
        if status:
            statement = statement.where(KnowledgeConflict.status == status)
        statement = statement.order_by(KnowledgeConflict.lookup_key, KnowledgeConflict.id).limit(
            max(1, int(limit))
        )
        return list(self.session.execute(statement).scalars())

    def conflicts_for_entity(self, ref: EntityRef, *, limit: int = 100) -> list[KnowledgeConflict]:
        """Conflicts whose candidates include an item of this entity.

        Resolved through the candidate list rather than a column because a conflict is about a
        *property*, and the property may be asserted about a well by a document, about a section,
        or about a bit run: the rows that argue are what ties the conflict to the entity.
        """
        item_ids = [
            str(row[0])
            for row in self.session.execute(
                select(KnowledgeItem.id).where(
                    KnowledgeItem.entity_type == entity_spec(ref.entity_type).name,
                    KnowledgeItem.entity_id == ref.entity_id,
                )
            ).all()
        ]
        if not item_ids:
            return []
        wanted = set(item_ids)
        found: list[KnowledgeConflict] = []
        for row in self.conflicts(status=None, limit=max(500, limit * 10)):
            if any(
                str(candidate.get("item_id", "")) in wanted
                for candidate in list(row.candidates or [])
            ):
                found.append(row)
                if len(found) >= max(1, int(limit)):
                    break
        return found

    def counts(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        """How much knowledge there is, by origin and status, plus the edges and conflicts.

        ``knowledge_item`` holds two populations, and adding them together would make the headline
        disagree with its own breakdown: a *fact* asserts something and so carries a lookup key,
        while an *entity record* exists only for a derived subject to point at (a lesson, a mud
        system) and asserts nothing.  ``facts`` is the first population, ``entity_records`` the
        second.  Entity records are keyed on their label and carry no document or workspace link, so
        that count is over the whole file - the same set, because a workspace is a file.
        """
        asserts_something = and_(
            KnowledgeItem.lookup_key.is_not(None), KnowledgeItem.lookup_key != ""
        )
        scoped = select(KnowledgeItem.id).where(asserts_something)
        if workspace_id:
            scoped = scoped.where(
                KnowledgeItem.document_id.in_(
                    select(Document.id).where(Document.workspace_id == workspace_id)
                )
            )

        def tally(column: Any) -> dict[str, int]:
            statement = (
                select(column, func.count()).where(KnowledgeItem.id.in_(scoped)).group_by(column)
            )
            return {
                str(key or ""): int(count) for key, count in self.session.execute(statement).all()
            }

        def count(statement: Any) -> int:
            return int(self.session.execute(statement).scalar_one())

        facts = select(func.count()).select_from(KnowledgeItem).where(KnowledgeItem.id.in_(scoped))
        entity_records = (
            select(func.count()).select_from(KnowledgeItem).where(not_(asserts_something))
        )
        relations = select(func.count()).select_from(KnowledgeRelation)
        conflicts = (
            select(func.count())
            .select_from(KnowledgeConflict)
            .where(KnowledgeConflict.status == ConflictResolution.OPEN.value)
        )

        return {
            "facts": count(facts),
            "entity_records": count(entity_records),
            "by_origin": tally(KnowledgeItem.origin),
            "by_status": tally(KnowledgeItem.status),
            "by_value_type": tally(KnowledgeItem.value_type),
            "by_entity_type": tally(KnowledgeItem.entity_type),
            "relations": count(relations),
            "open_conflicts": count(conflicts),
        }

    # -- internals ----------------------------------------------------------
    def _facts(
        self,
        statement: Select,
        *,
        predicate: str = "",
        status: str | None = None,
        include_superseded: bool = False,
        limit: int = 200,
    ) -> list[KnowledgeFact]:
        narrowed = self._narrow(
            statement, predicate=predicate, status=status, include_superseded=include_superseded
        )
        rows = self.session.execute(self._ordered(narrowed).limit(max(1, int(limit)))).scalars()
        return [KnowledgeFact.from_item(row) for row in rows]

    def _narrow(
        self,
        statement: Select,
        *,
        predicate: str = "",
        status: str | None = None,
        include_superseded: bool = False,
    ) -> Select:
        if predicate:
            statement = statement.where(KnowledgeItem.predicate == _predicate_token(predicate))
        if status is not None:
            statement = statement.where(KnowledgeItem.status == KnowledgeStatus(status).value)
        elif not include_superseded:
            # SUPERSEDED and RETIRED are the two states that mean "this is not the answer any
            # more" - one because a newer revision replaced it, one because a person decided
            # against it - so both are held back until a caller asks for the history.  Every other
            # status stays visible: a candidate or a conflicted value is a gap in the platform's
            # answer, and hiding a gap is how a system starts looking more certain than its sources.
            statement = statement.where(
                KnowledgeItem.status.not_in(
                    {KnowledgeStatus.SUPERSEDED.value, KnowledgeStatus.RETIRED.value}
                )
            )
        return statement

    def _ordered(self, statement: Select) -> Select:
        """Newest revision first, then a stable tie-break, so two runs list the same order.

        Sorting by ``updated_at`` alone would shuffle rows written in the same transaction;
        ``(revision desc, id)`` is stable and still puts a newer statement first.
        """
        return statement.order_by(
            KnowledgeItem.revision.desc(), KnowledgeItem.updated_at.desc(), KnowledgeItem.id
        )

    def _source_id_for(self, fact: KnowledgeFact) -> str:
        """The ``source`` row a fact cites - created on first use, never duplicated.

        The citation key is the document *version*, because that is the thing that can be re-read
        and compared: ``mud_report.xlsx`` is ambiguous between revisions, ``version:ver-3`` is not.
        """
        if not fact.document_version_id:
            return ""
        version = self.session.get(DocumentVersion, fact.document_version_id)
        if version is None:
            raise KnowledgeError(
                f"fact cites document version {fact.document_version_id!r}, which is not in the registry",
                hint="the extraction was removed without removing its knowledge; `drillintel knowledge rebuild` re-derives from what is left",
            )
        document = self.session.get(Document, version.document_id)
        authority = (
            document.source_authority if document is not None else None
        ) or "general_knowledge"
        label = (
            (document.title or document.filename)
            if document is not None
            else version.source_relative_path
        )
        source = self.documents.get_or_create_source(
            kind="document",
            reference=f"version:{version.id}",
            label=str(label or version.source_relative_path or version.id),
            authority_tier=str(authority),
            document_id=version.document_id,
            document_version_id=version.id,
            revision=str(
                (document.revision if document is not None else None)
                or f"v{version.version_number}"
            ),
            # ``notes`` rather than a new column: the source row is shared with the documents
            # layer, and "issued when" belongs to the document, which already stores it.
            notes=(
                f"issued {document.document_date.date()}"
                if document is not None and document.document_date
                else None
            ),
            verified=bool(version.is_current),
        )
        return str(source.id)


def _predicate_token(value: str) -> str:
    token = str(value or "").strip().casefold().replace(" ", "_").replace("-", "_")
    return token


def _digest_of(row: KnowledgeItem) -> str:
    """What a stored row says, in a form safe to compare for "did anything change?".

    Deliberately excludes ``updated_at``: a row whose content is identical must report
    ``UNCHANGED`` even though SQLAlchemy touched its timestamp, or every rebuild would claim to
    have changed the whole workspace.
    """
    from ..core.hashing import sha256_obj

    return sha256_obj(
        {
            "status": row.status,
            "value": row.value,
            "unit": row.unit,
            "normalized": [row.normalized_value, row.normalized_unit],
            "original": [row.original_value, row.original_unit],
            "payload": row.payload,
            "provenance": row.provenance,
            "evidence": row.evidence,
            "confidence": row.confidence,
            "revision": row.revision,
            "source_id": row.source_id,
        }
    )
