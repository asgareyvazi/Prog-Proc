"""The structured-index boundary, stated as an invariant rather than left implicit.

``search/structured.py`` keeps three collections that have to describe the same set of record types:
:data:`STRUCTURED_RECORD_TYPES` (the public vocabulary, used by callers to filter),
:data:`_RECORD_SOURCES` (which model each type is read from) and ``_BUILDERS`` (how a row becomes a
search unit).  Nothing in the production path compares them.  A type added to the vocabulary without
a source would simply never be projected - and, because ``structured_searchable_ids`` walks
``_RECORD_SOURCES`` rather than the vocabulary, it would not be *counted* as missing either.  The
index would report a clean bill of health over a domain nobody can search.

The second half of this suite pins the deliberate part of the design: child tables are excluded from
the vocabulary on purpose, and what makes that safe is that each parent builder folds its children's
values **and** provenance into the parent's unit.  If someone later indexes children directly, or
drops the fold, one of these tests fails and the reason has to be stated.
"""

from __future__ import annotations

import inspect

from drilling_intelligence.database.models import (
    BhaComponent,
    BhaReport,
    BitRecord,
    MudMeasurement,
    MudReport,
    NptRecord,
    ProblemOccurrence,
    SurveyRun,
    SurveyStation,
    WellEvent,
)
from drilling_intelligence.search import structured
from drilling_intelligence.search.structured import (
    _BUILDERS,
    _RECORD_SOURCES,
    STRUCTURED_RECORD_TYPES,
)

#: The child tables of the source-versioned domains: rows that are part of a parent's statement.
CHILD_TABLES = {
    BhaComponent.__tablename__,
    SurveyStation.__tablename__,
    MudMeasurement.__tablename__,
}

#: Parents whose children must be folded into the parent's own search unit.
PARENTS_WITH_CHILDREN = {
    BhaReport.__tablename__: ("bha_components", "component_evidence"),
    SurveyRun.__tablename__: ("survey_stations", "station_evidence"),
    MudReport.__tablename__: ("mud_measurements", "measurement_evidence"),
}


def test_the_public_vocabulary_the_sources_and_the_builders_describe_one_set() -> None:
    """A record type must be filterable, readable and formattable - all three, or none."""
    vocabulary = set(STRUCTURED_RECORD_TYPES)
    sources = {record_type for _model, record_type, _order in _RECORD_SOURCES}
    builders = set(_BUILDERS)

    assert vocabulary == sources, {
        "filterable_but_not_read": sorted(vocabulary - sources),
        "read_but_not_filterable": sorted(sources - vocabulary),
    }
    assert vocabulary == builders, {
        "filterable_but_not_formattable": sorted(vocabulary - builders),
        "formattable_but_not_filterable": sorted(builders - vocabulary),
    }


def test_no_record_type_is_listed_twice_with_a_different_ordering() -> None:
    """Two sources for one type would project the same row twice under one identity."""
    record_types = [record_type for _model, record_type, _order in _RECORD_SOURCES]
    assert len(record_types) == len(set(record_types)), record_types


def test_every_source_declares_a_deterministic_order_ending_in_its_primary_key() -> None:
    """A rebuild must produce the same units in the same order, so the order cannot be incidental."""
    for model, record_type, order_columns in _RECORD_SOURCES:
        assert order_columns, record_type
        assert order_columns[-1] == "id", (record_type, order_columns)
        for column in order_columns:
            assert hasattr(model, column), (record_type, column)


def test_child_tables_are_deliberately_outside_the_index_vocabulary() -> None:
    """Excluding children is a decision, not an omission - so it is asserted, not assumed."""
    vocabulary = set(STRUCTURED_RECORD_TYPES)
    assert not (vocabulary & CHILD_TABLES), sorted(vocabulary & CHILD_TABLES)


def test_every_parent_with_children_folds_them_into_its_own_unit() -> None:
    """The fold is what makes excluding children safe; without it a child fact is unreachable."""
    for parent, (accessor, evidence_key) in PARENTS_WITH_CHILDREN.items():
        builder = _BUILDERS[parent]
        source = inspect.getsource(builder)
        assert f"scope.{accessor}(" in source, f"{parent} no longer reads its children via {accessor}"
        # The child's own provenance must reach the unit, or the value is searchable but uncitable.
        assert f'"{evidence_key}"' in source, f"{parent} no longer carries {evidence_key}"


def test_indexed_types_include_the_rows_a_reader_asks_about_directly() -> None:
    """The boundary is top-level records - including ones that themselves have a parent row.

    ``npt_record``, ``well_event`` and ``problem_occurrence`` each carry a foreign key to a parent
    (a DDR report, an operation, an event) and ``bit_record`` carries one to a BHA report, yet all
    are indexed: a parent foreign key is not what excludes a table.  What excludes a table is being a
    *measurement inside* a parent's own statement.  Recording that here is what stops "has a parent"
    from being mistaken for the rule.
    """
    vocabulary = set(STRUCTURED_RECORD_TYPES)
    for model in (NptRecord, WellEvent, ProblemOccurrence, BitRecord):
        assert model.__tablename__ in vocabulary, model.__tablename__
        foreign_keys = {
            foreign_key.column.table.name
            for column in model.__table__.columns
            for foreign_key in column.foreign_keys
        }
        assert foreign_keys, f"{model.__tablename__} was expected to have a parent of its own"


def test_the_folded_children_are_read_in_one_pass_not_one_query_per_parent() -> None:
    """Folding must not turn a rebuild into an N+1 over every assembly, run and report."""
    scope_init = structured._Scope.__init__.__code__
    names = list(scope_init.co_names)
    # One grouped read per child table, keyed by its parent's id, rather than a query inside a loop.
    for accessor in ("_bha_components", "_survey_stations", "_mud_measurements"):
        assert accessor in names, accessor
