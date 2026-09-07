"""Cost items as records, not as a cost engine.

The platform holds what a source stated about money: a planned figure, an actual figure, the currency each
was stated in, and the WBS/CBS codes that place the line in a structure.  It deliberately does not compute a
cost.  There is no rate table, no currency conversion, no inflation index, no AFE builder and no
"estimated remaining" - every one of those needs an input the database does not have, and a number invented
here would be indistinguishable from a number a person entered.

The one arithmetic this module performs is addition, under one rule: **figures in different currencies are
never added together**.  A field that reports in USD and NOK has two cost structures that happen to share a
well, and a total across both is a number that means nothing - it is not even wrong, because there is no
exchange rate implied by the rows.  So every grouping returns per-currency figures plus a flag saying the
scope is mixed, and a caller who wants one number has to choose a rate and say so out loud.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.enums import ConfirmationStatus, KnowledgeOrigin, RecordState
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..core.lifecycle import CONFIRMATION_LIFECYCLE
from ..core.vocabulary import cost_category
from ..database.models import CostItem, Well
from ..operations.repository import set_record_status

__all__ = ["COST_SCOPE_KEYS", "CostRepository", "currency_of"]

#: The columns a cost line can be scoped to.  Scope is not free: a row with none of these belongs to
#: nobody's programme, and that is stated rather than guessed.
COST_SCOPE_KEYS = ("project_id", "field_id", "well_id", "program_id")

#: The fields that identify a cost line well enough that a second mention of it is a duplicate rather
#: than a new one.  The description is excluded: the same line re-worded is the same line.
_IDENTITY_KEYS = (
    "wbs_code",
    "cbs_code",
    "cbs_path",
    "category",
    "planned_value",
    "planned_unit",
    "actual_value",
    "actual_unit",
    "npt_id",
    *COST_SCOPE_KEYS,
)


def currency_of(unit: object) -> str:
    """The currency a stored unit means, folding case and nothing else.

    ``usd``, ``USD `` and ``UsD`` are one currency.  ``USD/t`` is not: it is a rate, and a rate is not a
    currency - which matters here only because the grouping key decides what can be added to what.
    """
    text = str(unit or "").strip().upper()
    return text or "USD"


class CostRepository:
    """Planned and actual cost lines, with their codes and their currencies.

    Creation is create-or-return, like every other repository in this package: re-reading the same
    programme sheet must not double-count a line, and a row whose figures have moved is corrected by the
    person who owns the cost, not by whoever happened to re-import the file.
    """

    def __init__(self, session: Session) -> None:
        if session is None:
            raise ValidationError("the cost repository needs the registry session")
        self.session = session

    # -- writing ---------------------------------------------------------------
    def record_item(
        self,
        *,
        description: str,
        planned_value: float | None = None,
        planned_unit: str = "USD",
        actual_value: float | None = None,
        actual_unit: str = "USD",
        category: str = "",
        wbs_code: str = "",
        cbs_code: str = "",
        cbs_path: str = "",
        npt_id: str = "",
        record_state: RecordState | str = RecordState.CURRENT,
        status: ConfirmationStatus | str = ConfirmationStatus.CANDIDATE,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        origin: str = KnowledgeOrigin.MANUAL.value,
        created_by: str = "system",
        attributes: Mapping[str, Any] | None = None,
        **scope: Any,
    ) -> tuple[CostItem, bool]:
        """One cost line, and whether this call is what created it.

        ``provenance`` is required in spirit, not by a constraint: a number about money with no source is
        the kind of figure that ends up in a report nobody can defend.  Nothing here checks that a provenance
        entry exists - an engineer typing a line at a terminal legitimately has the paper in front of them -
        but ``summary`` counts unattributed rows so the balance is visible.
        """
        unknown = sorted(set(scope) - set(COST_SCOPE_KEYS))
        if unknown:
            raise ValidationError(
                f"unknown cost scope {', '.join(unknown)}", known=list(COST_SCOPE_KEYS)
            )
        text = str(description or "").strip()
        if not text:
            raise ValidationError("a cost line needs a description")
        state = RecordState(str(record_state).upper())
        # A category the vocabulary knows becomes the canonical token; one it does not is kept as the
        # source wrote it, because "RIG MOVE" in a spreadsheet is a real category and inventing
        # "other" for it would erase the only evidence of what the sheet called it.
        wanted = str(category or "").strip()
        resolved = "other"
        wording = ""
        if wanted:
            # ``token`` is a slug of whatever the caller wrote, and ``recognised`` says whether the
            # vocabulary knew it; both cases store the slug, and an unfamiliar category also keeps the
            # sheet's own wording in ``attributes`` so nothing is lost by the normalisation.
            match = cost_category(wanted)
            resolved = match.token
            if not match.recognised:
                wording = wanted
        stored_attributes = dict(attributes or {})
        if wording:
            stored_attributes.setdefault("source_wording", {})["category"] = wording
        payload = {
            "wbs_code": str(wbs_code or "").strip() or None,
            "cbs_code": str(cbs_code or "").strip() or None,
            "cbs_path": str(cbs_path or "").strip() or None,
            "category": resolved,
            "npt_id": str(npt_id or "").strip() or None,
            **{key: (str(scope[key]).strip() or None) for key in COST_SCOPE_KEYS if key in scope},
            "planned_value": None if planned_value is None else float(planned_value),
            "planned_unit": currency_of(planned_unit),
            "actual_value": None if actual_value is None else float(actual_value),
            "actual_unit": currency_of(actual_unit),
        }
        identity = self._identity(payload)
        existing = self.session.execute(
            select(CostItem).where(CostItem.identity_key == identity).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        row = CostItem(
            id=new_id("cost"),
            description=text,
            record_state=state.value,
            status=str(CONFIRMATION_LIFECYCLE.parse(status)),
            provenance=[dict(item) for item in provenance or ()],
            origin=str(origin),
            created_by=str(created_by or "system"),
            identity_key=identity,
            attributes=stored_attributes,
            **payload,
        )
        self.session.add(row)
        self.session.flush()
        return row, True

    def _identity(self, payload: Mapping[str, Any]) -> str:
        from ..core.hashing import sha256_obj

        return sha256_obj({key: payload.get(key) for key in _IDENTITY_KEYS})

    def set_status(
        self,
        cost_id: str,
        new_status: ConfirmationStatus | str,
        *,
        by: str = "",
        reason: str = "",
    ) -> CostItem:
        """Confirm or reject a cost line, attributing the decision."""
        row = self.get(cost_id)
        set_record_status(
            self.session,
            row,
            new_status,
            by=by,
            reason=reason,
            lifecycle=CONFIRMATION_LIFECYCLE,
        )
        return row

    def link_to_npt(self, cost_id: str, npt_id: str) -> CostItem:
        """Attach a cost line to the NPT event it was spent on.

        The column *is* the link: ``cost_item.npt_id`` is how every cost-per-event query in this
        repository reads, and an edge in the relation table beside it would be a second copy of one fact,
        free to disagree with the first.  The graph is for relationships the schema cannot express - a
        lesson citing a pattern, a procedure referenced by a report - not for a foreign key with a name.
        """
        row = self.get(cost_id)
        if not str(npt_id or "").strip():
            raise ValidationError("linking a cost line needs an npt_id")
        row.npt_id = str(npt_id)
        row.identity_key = self._identity({key: getattr(row, key) for key in _IDENTITY_KEYS})
        self.session.flush()
        return row

    # -- reading ---------------------------------------------------------------
    def get(self, cost_id: str) -> CostItem:
        row = self.session.get(CostItem, str(cost_id))
        if row is None:
            raise ValidationError(f"no cost item {cost_id!r}")
        return row

    def list_items(
        self,
        *,
        status: str = "",
        category: str = "",
        record_state: str = "",
        limit: int = 200,
        **scope: Any,
    ) -> list[CostItem]:
        unknown = sorted(set(scope) - set(COST_SCOPE_KEYS))
        if unknown:
            raise ValidationError(
                f"unknown cost scope {', '.join(unknown)}", known=list(COST_SCOPE_KEYS)
            )
        statement = select(CostItem)
        for key, value in scope.items():
            if value:
                statement = statement.where(getattr(CostItem, key) == str(value))
        if status:
            statement = statement.where(CostItem.status == str(status).upper())
        if category:
            statement = statement.where(CostItem.category == str(category).strip().lower())
        if record_state:
            statement = statement.where(CostItem.record_state == str(record_state).upper())
        statement = statement.order_by(CostItem.wbs_code.asc().nulls_last(), CostItem.id)
        if limit and limit > 0:
            statement = statement.limit(int(limit))
        return list(self.session.execute(statement).scalars())

    def _scope_clauses(self, scope: Mapping[str, Any]) -> list[Any]:
        """The condition that says which cost lines belong to a scope.

        A line usually names a programme or a well rather than a field, and the field is a property of the
        well - so a field scope reaches those rows through ``well`` instead of a denormalised ``field_id``
        copy that would go stale the day a well is re-assigned.  The same clauses are used by every read
        here, which is what makes a count and a total agree.
        """
        unknown = sorted(set(scope) - set(COST_SCOPE_KEYS))
        if unknown:
            raise ValidationError(
                f"unknown cost scope {', '.join(unknown)}", known=list(COST_SCOPE_KEYS)
            )
        clauses: list[Any] = []
        for column, key, model, model_column in (
            (CostItem.field_id, "field_id", Well, Well.field_id),
            (CostItem.project_id, "project_id", Well, Well.project_id),
        ):
            if scope.get(key):
                wanted = str(scope[key])
                clauses.append(column == wanted)
                clauses.append(CostItem.well_id.in_(select(model.id).where(model_column == wanted)))
        for name in ("well_id", "program_id"):
            if scope.get(name):
                clauses.append(getattr(CostItem, name) == str(scope[name]))
        return clauses

    def _scope_statement(self, scope: Mapping[str, Any]) -> Any:
        statement = select(CostItem)
        clauses = self._scope_clauses(scope)
        if clauses:
            statement = statement.where(or_(*clauses))
        return statement

    def summary(self, **scope: Any) -> dict[str, Any]:
        """Per-currency totals for a scope, with everything unpriced still counted.

        ``items`` is never equal to ``priced`` by accident: a programme whose sheet has thirty lines and
        figures against nine of them is a thirty-line programme, and a total that quietly describes nine of
        them is a number someone will repeat.
        """
        unknown = sorted(set(scope) - set(COST_SCOPE_KEYS))
        if unknown:
            raise ValidationError(
                f"unknown cost scope {', '.join(unknown)}", known=list(COST_SCOPE_KEYS)
            )
        # Nothing is filtered out by record state.  A line marked FORECAST still states a planned figure
        # that belongs in the planned total, and a summary that quietly dropped it would report a cheaper
        # well than the sheet does; ``by_state`` is where a reader separates them.
        statement = self._scope_statement(scope)
        rows = list(self.session.execute(statement.order_by(CostItem.id)).scalars())
        by_state: dict[str, int] = {}
        by_currency: dict[str, dict[str, Any]] = {}
        by_category: dict[str, dict[str, Any]] = {}
        mixed_lines = 0
        for row in rows:
            by_state[str(row.record_state)] = by_state.get(str(row.record_state), 0) + 1
            planned_currency = currency_of(row.planned_unit)
            actual_currency = currency_of(row.actual_unit)
            for currency, value, name in (
                (planned_currency, row.planned_value, "planned"),
                (actual_currency, row.actual_value, "actual"),
            ):
                bucket = by_currency.setdefault(
                    currency,
                    {
                        "planned": 0.0,
                        "actual": 0.0,
                        "planned_lines": 0,
                        "actual_lines": 0,
                        "mixed_unit_lines": 0,
                    },
                )
                if value is None:
                    continue
                bucket[name] = round(float(bucket[name]) + float(value), 4)
                bucket[f"{name}_lines"] += 1
            if planned_currency != actual_currency:
                # A line stated in two currencies is not a total, it is a question; it is reported rather
                # than resolved because the answer - which currency the well was actually paid in - is not
                # in this database.
                mixed_lines += 1
            entry = by_category.setdefault(
                str(row.category or "other"),
                {"lines": 0, "planned": 0.0, "actual": 0.0, "currencies": set()},
            )
            entry["lines"] += 1
            entry["currencies"].add(planned_currency)
            entry["currencies"].add(actual_currency)
            if row.planned_value is not None:
                entry["planned"] = round(entry["planned"] + float(row.planned_value), 4)
            if row.actual_value is not None:
                entry["actual"] = round(entry["actual"] + float(row.actual_value), 4)
        priced = sum(
            1 for row in rows if row.planned_value is not None or row.actual_value is not None
        )
        return {
            "scope": {key: (str(scope[key]) or None) for key in COST_SCOPE_KEYS if key in scope}
            or dict.fromkeys(COST_SCOPE_KEYS),
            "items": len(rows),
            "by_state": dict(sorted(by_state.items())),
            "priced": priced,
            "unpriced": len(rows) - priced,
            "unattributed": sum(1 for row in rows if not (row.provenance or [])),
            "currencies": sorted(by_currency),
            "mixed_currency": len(by_currency) > 1,
            "mixed_currency_lines": mixed_lines,
            "by_currency": {
                key: {
                    **value,
                    "variance": (
                        round(float(value["actual"]) - float(value["planned"]), 4)
                        if value["planned_lines"] and value["actual_lines"]
                        else None
                    ),
                }
                for key, value in sorted(by_currency.items())
            },
            "by_category": {
                key: {
                    "lines": entry["lines"],
                    "planned": entry["planned"],
                    "actual": entry["actual"],
                    "currencies": sorted(entry["currencies"]),
                }
                for key, entry in sorted(
                    by_category.items(), key=lambda item: (-item[1]["lines"], item[0])
                )
            },
        }

    def rollup(self, *, by: str = "wbs", **scope: Any) -> list[dict[str, Any]]:
        """Group the lines by their structure codes, per currency, without adding currencies together.

        ``by="cbs"`` groups on the full path, which is how a cost-breakdown structure is actually read
        (``1.2.3`` is meaningless without ``1.2`` above it); ``by="wbs"`` groups on the work package.
        """
        key = str(by or "wbs").strip().lower()
        if key not in {"wbs", "cbs", "cbs_path"}:
            raise ValidationError(
                f"cannot roll costs up by {by!r}", known=["wbs", "cbs", "cbs_path"]
            )
        column = (
            CostItem.wbs_code
            if key == "wbs"
            else (CostItem.cbs_path if key == "cbs_path" else CostItem.cbs_code)
        )
        rows = list(
            self.session.execute(
                self._scope_statement(scope).order_by(column.asc().nulls_last(), CostItem.id)
            ).scalars()
        )
        groups: dict[tuple[str | None, str], dict[str, Any]] = {}
        for row in rows:
            code = getattr(row, column.key) or None
            for currency, value, name in (
                (currency_of(row.planned_unit), row.planned_value, "planned"),
                (currency_of(row.actual_unit), row.actual_value, "actual"),
            ):
                if value is None:
                    continue
                entry = groups.setdefault(
                    (code, currency),
                    {"code": code, "currency": currency, "planned": 0.0, "actual": 0.0, "lines": 0},
                )
                entry[name] = round(float(entry[name]) + float(value), 4)
                entry["lines"] += 1
        return [
            {
                **entry,
                "variance": (
                    round(entry["actual"] - entry["planned"], 4)
                    if entry["planned"] and entry["actual"]
                    else None
                ),
            }
            for entry in sorted(
                groups.values(), key=lambda item: (str(item["code"] or ""), item["currency"])
            )
        ]

    def counts(self, **scope: Any) -> dict[str, Any]:
        """How many lines there are, and how many of them a person has confirmed.

        Counted through the same scope clauses as :meth:`summary`, so the line count on a screen and the
        totals underneath it cannot disagree - which is the only reason either of them is useful.
        """
        statement = select(CostItem.status, func.count()).group_by(CostItem.status)
        clauses = self._scope_clauses(scope)
        if clauses:
            statement = statement.where(or_(*clauses))
        rows = self.session.execute(statement).all()
        return {
            "by_status": {str(status): int(count) for status, count in sorted(rows)},
            "total": sum(int(count) for _, count in rows),
        }
