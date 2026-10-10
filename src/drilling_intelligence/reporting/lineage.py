"""Traceability for a built engineering report.

This module reads a ``ReportPack`` payload and says, for each section, table and metric, which
certified source reference supports it. It does not open a database, does not resolve a row, and
does not decide whether a file citation still matches. Those are a separate, explicit audit.

A shared number, label or unit is never treated as a link. The only link is the domain (and, for
aggregates, the method and scope) already stored on the pack's evidence references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.hashing import sha256_obj

TRACE_SCHEMA = "report-traceability/1"

# Relationship states. These are not citation outcomes.
REL_AGGREGATE = "AGGREGATE_METHOD"
REL_SAMPLE = "BOUNDED_SAMPLE"
REL_TRUNCATED = "TRUNCATED_SAMPLE"
REL_COUNT_ONLY = "COUNT_ONLY"
REL_DIRECT = "DIRECT_RECORD"
REL_UNSUPPORTED = "UNSUPPORTED_LINKAGE"
REL_UNLINKED = "UNLINKED_SOURCE"
REL_MISSING = "MISSING_REFERENCE"
REL_SCOPE = "SCOPE_STATEMENT"

# Metric prefix -> evidence domain. Must stay aligned with the exhibit domain map.
METRIC_DOMAIN: dict[str, str] = {
    "npt": "npt_record",
    "problems": "problem_occurrence",
    "well_control": "well_control_event",
    "hse": "hse_incident",
    "cost": "cost_item",
    "risk": "risk_record",
    "lessons": "lesson_learned",
    "practices": "best_practice",
    "recommendations": "recommendation",
    "patterns": "field_pattern",
    "calculations": "calculation",
    "plan": "well_section",
    "conflicts": "knowledge_conflict",
    "profile": "well",
}

SECTION_DOMAINS: dict[str, tuple[str, ...]] = {
    "npt": ("npt_record",),
    "problems": ("problem_occurrence",),
    "well_control": ("well_control_event",),
    "hse": ("hse_incident",),
    "economics": ("cost_item",),
    "risk": ("risk_record",),
    "learning": ("lesson_learned", "best_practice"),
    "recommendations": ("recommendation",),
    "patterns": ("field_pattern",),
    "calculations": ("calculation",),
    "execution": ("well_section",),
    "conflicts": ("knowledge_conflict",),
    "operations": ("npt_record", "problem_occurrence", "well_control_event", "hse_incident"),
}

_UNSUPPORTED_SECTIONS = frozenset({"timeline"})
_UNSUPPORTED_EXHIBITS = frozenset({"exhibit-depth-series"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _relationship(ref: dict[str, Any]) -> str:
    kind = str(ref.get("kind") or "")
    sample = _items(ref.get("sample"))
    truncated = bool(ref.get("truncated"))
    count = ref.get("count")
    if kind == "aggregate" and str(ref.get("method") or ""):
        return REL_AGGREGATE
    if kind == "records" or not sample:
        return REL_COUNT_ONLY
    if truncated:
        return REL_TRUNCATED
    if isinstance(count, int) and count == len(sample):
        return REL_DIRECT
    return REL_SAMPLE


def _ref_id(index: int) -> str:
    return f"src-{index:04d}"


@dataclass(frozen=True)
class TraceabilityManifest:
    """A deterministic reading of one report payload. Identity excludes itself."""

    schema: str
    report_identity: str
    source_pack_schema: str
    source_pack_identity: str
    sources: tuple[dict[str, Any], ...]
    targets: tuple[dict[str, Any], ...]
    links: tuple[dict[str, Any], ...]
    unlinked: tuple[str, ...]
    identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "report_identity": self.report_identity,
            "source_pack_schema": self.source_pack_schema,
            "source_pack_identity": self.source_pack_identity,
            "sources": [dict(item) for item in self.sources],
            "targets": [dict(item) for item in self.targets],
            "links": [dict(item) for item in self.links],
            "unlinked": list(self.unlinked),
        }
        body["identity"] = self.identity or sha256_obj(
            {key: value for key, value in body.items() if key != "identity"}
        )
        return body


def traceability_manifest(payload: dict[str, Any]) -> TraceabilityManifest:
    """Build the manifest from a report payload. No database access."""
    report = _mapping(payload)
    packs = [item for item in _items(report.get("source_packs")) if isinstance(item, dict)]
    evidence = [item for item in _items(report.get("evidence")) if isinstance(item, dict)]
    sources: list[dict[str, Any]] = []
    by_domain: dict[str, list[str]] = {}
    for index, ref in enumerate(evidence):
        ref_id = _ref_id(index)
        domain = str(ref.get("domain") or "")
        relationship = _relationship(ref)
        sample = [str(item) for item in _items(ref.get("sample"))]
        record = {
            "ref_id": ref_id,
            "section": str(ref.get("section") or ""),
            "domain": domain,
            "kind": str(ref.get("kind") or ""),
            "identity_kind": str(ref.get("identity") or ""),
            "count": ref.get("count"),
            "sample": sample,
            "truncated": bool(ref.get("truncated")),
            "method": str(ref.get("method") or ""),
            "scope": _mapping(ref.get("scope")),
            "relationship": relationship,
            "resolvable": relationship != REL_AGGREGATE and _has_structured_sample(ref, sample),
        }
        sources.append(record)
        by_domain.setdefault(domain, []).append(ref_id)

    targets: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    linked: set[str] = set()

    def _add_target(
        target_id: str,
        kind: str,
        title: str,
        domains: tuple[str, ...],
        *,
        forced: str = "",
        counts: bool = True,
    ) -> None:
        ref_ids: list[str] = []
        for domain in domains:
            for ref_id in by_domain.get(domain, ()):
                if ref_id not in ref_ids:
                    ref_ids.append(ref_id)
        if forced:
            state = forced
        elif ref_ids:
            states = [str(item["relationship"]) for item in sources if item["ref_id"] in ref_ids]
            state = states[0] if len(set(states)) == 1 else "MIXED"
        elif kind == "scope":
            state = REL_SCOPE
        else:
            state = REL_MISSING
        targets.append(
            {
                "target_id": target_id,
                "kind": kind,
                "title": title,
                "state": state,
                "ref_ids": ref_ids,
            }
        )
        for ref_id in ref_ids:
            relationship = next(
                item["relationship"] for item in sources if item["ref_id"] == ref_id
            )
            links.append({"target_id": target_id, "ref_id": ref_id, "relationship": relationship})
            if counts:
                linked.add(ref_id)

    _add_target("scope", "scope", "Report scope", (), forced=REL_SCOPE, counts=False)
    for section in _items(report.get("sections")):
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "")
        unsupported = (
            section_id in _UNSUPPORTED_SECTIONS or str(section.get("state") or "") == "UNSUPPORTED"
        )
        domains = SECTION_DOMAINS.get(section_id, ())
        if not domains and section_id != "evidence":
            found: list[str] = []
            for table in _items(section.get("tables")):
                for domain in _table_domains(table if isinstance(table, dict) else {}):
                    if domain not in found:
                        found.append(domain)
            domains = tuple(found)
        if section_id == "evidence":
            domains = tuple(by_domain)
        _add_target(
            f"section:{section_id}",
            "section",
            str(section.get("title") or section_id),
            () if unsupported else domains,
            forced=REL_UNSUPPORTED if unsupported else "",
            counts=section_id != "evidence",
        )
        for table in _items(section.get("tables")):
            if not isinstance(table, dict):
                continue
            table_id = str(table.get("table_id") or "")
            table_domains = _table_domains(table)
            _add_target(
                f"table:{table_id}",
                "table",
                str(table.get("title") or table_id),
                table_domains,
            )
            for row in _items(table.get("rows")):
                if not isinstance(row, dict):
                    continue
                metric = str(row.get("metric") or "")
                if not metric:
                    continue
                prefix = metric.split(".", 1)[0]
                domain = METRIC_DOMAIN.get(prefix, "")
                _add_target(
                    f"metric:{metric}",
                    "metric",
                    str(row.get("label") or metric),
                    (domain,) if domain else (),
                )
    for exhibit in _items(report.get("exhibits")):
        if not isinstance(exhibit, dict):
            continue
        exhibit_id = str(exhibit.get("exhibit_id") or "")
        forced = (
            REL_UNSUPPORTED
            if exhibit_id in _UNSUPPORTED_EXHIBITS
            or str(exhibit.get("state") or "") == "UNSUPPORTED"
            else ""
        )
        metric = str(exhibit.get("metric") or "")
        prefix = metric.split(".", 1)[0]
        domain = METRIC_DOMAIN.get(prefix, "")
        _add_target(
            f"exhibit:{exhibit_id}",
            "exhibit",
            str(exhibit.get("title") or exhibit_id),
            (domain,) if domain and not forced else (),
            forced=forced,
        )

    unlinked = tuple(item["ref_id"] for item in sources if item["ref_id"] not in linked)
    if unlinked:
        targets.append(
            {
                "target_id": "ledger:unlinked",
                "kind": "ledger",
                "title": "Source references not linked to a report target",
                "state": REL_UNLINKED,
                "ref_ids": list(unlinked),
            }
        )
        for ref_id in unlinked:
            relationship = next(
                item["relationship"] for item in sources if item["ref_id"] == ref_id
            )
            links.append(
                {
                    "target_id": "ledger:unlinked",
                    "ref_id": ref_id,
                    "relationship": relationship,
                }
            )

    body = TraceabilityManifest(
        schema=TRACE_SCHEMA,
        report_identity=str(report.get("identity") or ""),
        source_pack_schema=",".join(str(item.get("schema") or "") for item in packs),
        source_pack_identity=",".join(str(item.get("identity") or "") for item in packs),
        sources=tuple(sources),
        targets=tuple(targets),
        links=tuple(links),
        unlinked=unlinked,
    )
    rendered = body.to_dict()
    return TraceabilityManifest(
        schema=body.schema,
        report_identity=body.report_identity,
        source_pack_schema=body.source_pack_schema,
        source_pack_identity=body.source_pack_identity,
        sources=body.sources,
        targets=body.targets,
        links=body.links,
        unlinked=body.unlinked,
        identity=str(rendered["identity"]),
    )


def _has_structured_sample(ref: dict[str, Any], sample: list[str]) -> bool:
    if str(ref.get("identity") or "") != "structured" and not any(
        item.startswith("structured:") for item in sample
    ):
        return False
    return bool(sample)


def _table_domains(table: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    for row in _items(table.get("rows")):
        if not isinstance(row, dict):
            continue
        prefix = str(row.get("metric") or "").split(".", 1)[0]
        domain = METRIC_DOMAIN.get(prefix, "")
        if domain and domain not in found:
            found.append(domain)
    return tuple(found)
