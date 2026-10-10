"""Build a report from the certified decision and comparison reads.

This is the only reporting module that opens a session. Renderers never call it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..core.errors import ValidationError
from ..intelligence.comparison import UNDATED_BEHAVIOR, WINDOWED_DOMAINS, ComparisonIntelligence
from ..intelligence.decision import DecisionIntelligence
from .compose import compose_comparison, compose_single_well
from .contract import (
    MODE_DISCOVERED_OFFSETS,
    MODE_EXPLICIT_WELL_SET,
    MODE_NAMED_OFFSETS,
    MODE_SINGLE_WELL,
    ReportPack,
    ReportRequest,
)


def resolve_request(
    *,
    well_ids: Sequence[str] = (),
    anchor: str = "",
    offsets: Sequence[str] = (),
    since: Any = None,
    until: Any = None,
    detail: int = 1,
    evidence_limit: int = 10,
    offset_limit: int = 10,
) -> ReportRequest:
    """Choose one report mode. Contradictory selection is an error, not a guess."""
    anchor_value = str(anchor or "").strip()
    named = [str(value).strip() for value in offsets if str(value or "").strip()]
    explicit = [str(value).strip() for value in well_ids if str(value or "").strip()]
    has_explicit = any(str(value or "").strip() for value in well_ids)
    if has_explicit and (anchor_value or named):
        raise ValidationError(
            "contradictory scope: --well cannot be combined with --anchor/--offsets",
            hint="choose one selection style",
        )
    if named and not anchor_value:
        raise ValidationError(
            "--offsets requires --anchor",
            hint="name the anchor well, or pass --well",
        )
    if not has_explicit and not anchor_value:
        raise ValidationError(
            "a report needs a well or an anchor",
            hint="pass --well, or --anchor to discover offsets",
        )
    try:
        detail_value = int(detail)
        evidence_value = int(evidence_limit)
        offset_value = int(offset_limit)
    except (TypeError, ValueError) as exc:
        raise ValidationError("detail, evidence_limit and offset_limit must be integers") from exc
    if anchor_value and named:
        mode = MODE_NAMED_OFFSETS
        wells: tuple[str, ...] = ()
    elif anchor_value:
        mode = MODE_DISCOVERED_OFFSETS
        wells = ()
    elif len(explicit) == 1:
        mode = MODE_SINGLE_WELL
        wells = tuple(explicit)
    else:
        mode = MODE_EXPLICIT_WELL_SET
        wells = tuple(explicit)
    return ReportRequest(
        mode=mode,
        well_ids=wells,
        anchor=anchor_value,
        offsets=tuple(named),
        since=since,
        until=until,
        detail=detail_value,
        evidence_limit=evidence_value,
        offset_limit=offset_value,
    )


def build_report(
    session: Any,
    *,
    well_ids: Sequence[str] = (),
    anchor: str = "",
    offsets: Sequence[str] = (),
    since: Any = None,
    until: Any = None,
    detail: int = 1,
    evidence_limit: int = 10,
    offset_limit: int = 10,
) -> ReportPack:
    """Compose one report. Selection and folds stay in the certified services."""
    request = resolve_request(
        well_ids=well_ids,
        anchor=anchor,
        offsets=offsets,
        since=since,
        until=until,
        detail=detail,
        evidence_limit=evidence_limit,
        offset_limit=offset_limit,
    )
    if request.mode == MODE_SINGLE_WELL:
        decision = DecisionIntelligence(session).pack(
            well_id=request.well_ids[0],
            since=since,
            until=until,
            detail=request.detail,
            evidence_limit=request.evidence_limit,
        )
        return compose_single_well(
            decision.to_dict(),
            request,
            windowed_domains=WINDOWED_DOMAINS,
            undated_behavior=UNDATED_BEHAVIOR,
        )
    comparison = ComparisonIntelligence(session).compare(
        well_ids=request.well_ids,
        anchor=request.anchor,
        offsets=request.offsets,
        since=since,
        until=until,
        detail=request.detail,
        evidence_limit=request.evidence_limit,
        offset_limit=request.offset_limit,
    )
    return compose_comparison(comparison.to_dict(), request)
