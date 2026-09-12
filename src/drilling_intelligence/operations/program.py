"""What a drilling program commits a hole section to, read from the artefact the extractor already made.

A daily report says what happened; a *program* says what was going to happen, and until something
promotes it the planned half of every comparison is missing - :meth:`plan_actual_summary` can only
ever answer ``NO_TARGET``.  This module is the reader that closes that gap: it turns one stored
artefact into the plan it states, and refuses when the artefact does not state one.

It does no parsing of its own.  The PDF was read once, at ingestion, by
:mod:`drilling_intelligence.extraction.pdf_text`, and what came out - typed fields with units, a
quality verdict and a page/bbox locator - is the only input here.  A second parser would be a second
opinion about the same bytes, and the two would disagree the first time either changed.

The contract is deliberately narrow, because a plan that is *wrong* is worse than a plan that is
absent: a wrong planned depth turns into a variance report somebody schedules work against.  So a
field is used only when the extractor marked it ``VALID``, a section is planned only when the
artefact names exactly one hole size, and every number the program did not state stays ``None``
rather than becoming a zero that reads as "planned nothing".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.enums import DocumentClassification

__all__ = [
    "PROGRAM_CLASSIFICATIONS",
    "ProgramPlan",
    "SectionPlan",
    "find_program_plan",
    "section_name_from",
]

#: The classifications whose documents state a plan.  Kept beside
#: :data:`~drilling_intelligence.operations.repository.REPORT_CLASSIFICATIONS` and deliberately
#: separate from it: a program is not a day's work, and promoting it through the report path would
#: file a plan as history.
PROGRAM_CLASSIFICATIONS: frozenset[str] = frozenset({DocumentClassification.DRILLING_PROGRAM.value})

#: The artefact field that identifies *which* section a program is planning.  A program with no hole
#: size has not said what it is planning, and a target that names no section cannot be compared with
#: one.
SECTION_FIELDS: tuple[str, ...] = ("hole_size_in",)
#: The field naming how deep the section is drilled.  ``total_depth`` is deliberately absent: in this
#: corpus it carries a TVD, and filing a TVD as measured depth would compare two different distances.
DEPTH_FIELDS: tuple[str, ...] = ("depth_md",)
#: The designed mud weight.  ``equivalent_mud_weight`` is excluded for the same reason: an ECD target
#: is a dynamic density, not the mud the plan specifies to mix.
MUD_WEIGHT_FIELDS: tuple[str, ...] = ("mud_weight",)

#: The verdict the extractor puts on a field it could read and check.  Anything else - unverified,
#: invalid, absent - is not planned data.
VALID_QUALITY = "VALID"

#: Words a document puts *after* a hole size when it is naming the section ("12 1/4 in section").
#: Stripped so the target is named the way :class:`~drilling_intelligence.database.models.WellSection`
#: rows are named, which is what :meth:`EngineeringRepository._match_target` falls back to comparing.
_NAME_SUFFIX = re.compile(r"(?i)\s*\b(section|hole|interval)\b\s*$")


@dataclass(frozen=True)
class SectionPlan:
    """One hole section's planned numbers, each with the field that stated it.

    ``provenance`` is a list because the numbers come from different sentences on the page: the depth
    from the objectives paragraph, the mud weight from the one below it.  Keeping all of them is what
    lets a reader ask "where does this planned 10,450 ft come from" and get a page and a box back.
    """

    name: str
    hole_size_in: float | None = None
    planned_depth_md_value: float | None = None
    planned_depth_md_unit: str = ""
    planned_mud_weight_value: float | None = None
    planned_mud_weight_unit: str = ""
    provenance: tuple[dict[str, Any], ...] = ()
    #: ``{"planned_depth_md_value": "depth_md", ...}`` - which artefact field produced which column,
    #: so the stored row can say how it was derived without the reader re-deriving it.
    sources: dict[str, str] = field(default_factory=dict)

    def values(self) -> dict[str, Any]:
        """The target columns this plan states, omitting the ones it does not."""
        stated: dict[str, Any] = {}
        if self.hole_size_in is not None:
            stated["hole_size_in"] = self.hole_size_in
        if self.planned_depth_md_value is not None:
            stated["planned_depth_md_value"] = self.planned_depth_md_value
            stated["planned_depth_md_unit"] = self.planned_depth_md_unit
        if self.planned_mud_weight_value is not None:
            stated["planned_mud_weight_value"] = self.planned_mud_weight_value
            stated["planned_mud_weight_unit"] = self.planned_mud_weight_unit
        return stated


@dataclass(frozen=True)
class ProgramPlan:
    """The plan one artefact states: its sections, and why anything was left out.

    ``skipped`` is part of the result rather than a log line, because "the program stated two hole
    sizes and this reader would not guess which depth belonged to which" is an answer a caller has to
    be able to show, not a silence it has to interpret.
    """

    sections: tuple[SectionPlan, ...] = ()
    skipped: tuple[dict[str, str], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.sections)


def section_name_from(excerpt: str, hole_size_in: float | None) -> str:
    """Name the section the way the program named it, falling back to the size it stated.

    The excerpt is the document's own words ("12 1/4 in section"), so the trailing noun is dropped and
    nothing else is reformatted - ``12 1/4`` stays ``12 1/4`` rather than becoming ``12.25``, because
    the name is what a person reading the program would call the section, and a match against a
    ``WellSection`` row is made on exactly that spelling.
    """
    cleaned = _NAME_SUFFIX.sub("", re.sub(r"\s+", " ", str(excerpt or "")).strip())
    if cleaned:
        return cleaned[:200]
    if hole_size_in is None:
        return ""
    rendered = f"{float(hole_size_in):g}"
    return f"{rendered} in"


def _valid_fields(payload: Mapping[str, Any], names: Sequence[str]) -> list[dict[str, Any]]:
    """Every ``VALID`` artefact field with one of these names, in the order the extractor found them."""
    wanted = {str(name).strip().lower() for name in names}
    found: list[dict[str, Any]] = []
    for raw in payload.get("extracted_fields") or []:
        item = dict(raw)
        if str(item.get("name") or "").strip().lower() not in wanted:
            continue
        if str(item.get("quality") or "").strip().upper() != VALID_QUALITY:
            continue
        if item.get("value") is None:
            continue
        found.append(item)
    return found


def _number(item: Mapping[str, Any]) -> float | None:
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _excerpt(item: Mapping[str, Any]) -> str:
    provenance = item.get("provenance")
    if not isinstance(provenance, Mapping):
        return ""
    return str(provenance.get("excerpt") or "")


def _provenance(item: Mapping[str, Any]) -> dict[str, Any] | None:
    provenance = item.get("provenance")
    return dict(provenance) if isinstance(provenance, Mapping) else None


def find_program_plan(payload: Mapping[str, Any]) -> ProgramPlan:
    """Read the plan a stored artefact states, or say why it states none.

    One section per distinct hole size, and *only* when there is exactly one: a program that names a
    12 1/4 in and an 8 1/2 in section states two plans, and deciding which paragraph's depth belongs to
    which of them is reading a layout, not reading a number.  This reader refuses that and says so,
    because a mis-attributed planned depth would be compared against the wrong section's actual for
    the life of the well.

    The planned depth is the *deepest* measured depth the artefact states for the section.  A program
    gives the shoe it starts from and the depth it drills to in the same sentence, and the section's
    TD is the deeper of them - an order-independent rule, so two extractions that found the same two
    numbers in a different order still produce the same plan.
    """
    skipped: list[dict[str, str]] = []
    section_fields = _valid_fields(payload, SECTION_FIELDS)
    if not section_fields:
        return ProgramPlan(
            skipped=(
                {
                    "reason": "NO_SECTION_STATED",
                    "detail": "the artefact states no hole size, so it plans no identifiable section",
                },
            )
        )
    sizes = {_number(item) for item in section_fields} - {None}
    if len(sizes) > 1:
        rendered = ", ".join(f"{size:g}" for size in sorted(sizes))  # type: ignore[type-var]
        return ProgramPlan(
            skipped=(
                {
                    "reason": "AMBIGUOUS_SECTIONS",
                    "detail": (
                        f"the artefact states {len(sizes)} hole sizes ({rendered} in) and this reader "
                        "will not guess which planned number belongs to which section"
                    ),
                },
            )
        )
    anchor = section_fields[0]
    hole_size = _number(anchor)
    name = section_name_from(_excerpt(anchor), hole_size)
    if not name:
        return ProgramPlan(
            skipped=(
                {
                    "reason": "UNNAMED_SECTION",
                    "detail": "the hole size the artefact states cannot be turned into a section name",
                },
            )
        )

    provenance: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    anchor_provenance = _provenance(anchor)
    if anchor_provenance is not None:
        provenance.append(anchor_provenance)
    if hole_size is not None:
        sources["hole_size_in"] = str(anchor.get("name") or "")

    depth_value: float | None = None
    depth_unit = ""
    depths = _valid_fields(payload, DEPTH_FIELDS)
    if depths:
        deepest = max(depths, key=lambda item: _number(item) or float("-inf"))
        depth_value = _number(deepest)
        depth_unit = str(deepest.get("unit") or "")
        if depth_value is not None and not depth_unit:
            # A depth with no unit is a number nobody can compare; the column would silently take the
            # model's default and claim metres for a value read off a foot-denominated program.
            skipped.append(
                {
                    "reason": "DEPTH_WITHOUT_UNIT",
                    "detail": "the artefact states a measured depth with no unit, so it was not planned",
                }
            )
            depth_value = None
        elif depth_value is not None:
            item_provenance = _provenance(deepest)
            if item_provenance is not None:
                provenance.append(item_provenance)
            sources["planned_depth_md_value"] = str(deepest.get("name") or "")

    mud_value: float | None = None
    mud_unit = ""
    muds = _valid_fields(payload, MUD_WEIGHT_FIELDS)
    if muds:
        stated = muds[0]
        mud_value = _number(stated)
        mud_unit = str(stated.get("unit") or "")
        if mud_value is not None and not mud_unit:
            skipped.append(
                {
                    "reason": "MUD_WEIGHT_WITHOUT_UNIT",
                    "detail": "the artefact states a mud weight with no unit, so it was not planned",
                }
            )
            mud_value = None
        elif mud_value is not None:
            item_provenance = _provenance(stated)
            if item_provenance is not None:
                provenance.append(item_provenance)
            sources["planned_mud_weight_value"] = str(stated.get("name") or "")

    section = SectionPlan(
        name=name,
        hole_size_in=hole_size,
        planned_depth_md_value=depth_value,
        planned_depth_md_unit=depth_unit,
        planned_mud_weight_value=mud_value,
        planned_mud_weight_unit=mud_unit,
        provenance=tuple(provenance),
        sources=sources,
    )
    if not section.values():
        return ProgramPlan(
            skipped=(
                *skipped,
                {
                    "reason": "NO_PLANNED_VALUES",
                    "detail": f"the artefact names section {name!r} but states no planned number for it",
                },
            )
        )
    return ProgramPlan(sections=(section,), skipped=tuple(skipped))
