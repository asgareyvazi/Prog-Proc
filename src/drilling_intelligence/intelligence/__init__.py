"""Derived views over the records: a well's timeline, a field's numbers, and recurring problems.

Three kinds of answer, none of which is stored twice:

*   :func:`~drilling_intelligence.intelligence.timeline.build_timeline` merges every dated record of a
    well into one ordered list, with the undated ones listed at the end carrying the wording their source
    actually used - a record with no date is never given one;
*   :class:`~drilling_intelligence.intelligence.field.FieldIntelligence` answers the field questions in
    SQL (NPT hours and rows by category, problem occurrences and affected wells, events by type and
    severity, lessons with their evidence), scoped to a well, a field or a project;
*   :mod:`~drilling_intelligence.intelligence.patterns` groups the same problem rows into what recurs and
    - only when a person asks - takes a *snapshot* of the grouping, storing the exact query beside the
    numbers so a later re-run can report what moved instead of quietly rewriting an accepted figure.

No inference lives here.  There is no forecast, no similarity score invented from a hunch, no pattern that
was not counted from rows: an offset candidate is a list of wells whose recorded problem types and hole
sizes overlap, and every number in it comes back from the database with the query that produced it.
"""

from .field import FieldIntelligence
from .patterns import find_recurring, signature_for, snapshot, staleness
from .service import IntelligenceService
from .timeline import TimelineEntry, build_timeline

__all__ = [
    "FieldIntelligence",
    "IntelligenceService",
    "TimelineEntry",
    "build_timeline",
    "find_recurring",
    "signature_for",
    "snapshot",
    "staleness",
]
