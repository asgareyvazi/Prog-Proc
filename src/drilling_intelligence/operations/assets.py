"""Rigs and service companies: the assets an operation used, and nothing more.

Both tables exist because a well's history is incomplete without them - "the top drive failed" and
"we were on Rig 42 with Company X's fishing crew" are the same kind of fact, and an aggregation that
cannot ask "which rig was this on" cannot answer "does this rig have a problem with 8½ in holes".

What is deliberately *not* here is any kind of performance figure.  A rate of penetration per rig, a
"days lost per campaign", a utilisation score: each of those is an aggregate over operations and
events, and the platform computes them on request from those rows
(:meth:`~drilling_intelligence.intelligence.aggregation.FieldIntelligence.rig_summary`).  Storing a
number on the rig would create a second answer to that question, one with no provenance and no
refresh rule, and the first disagreement between the two would be settled by whoever shouted
loudest.  So a rig row owns its identity and its specification - what it *is* - and its capability
is a query.

Name matching is exact and case-sensitive, as it is for wells
(:meth:`~drilling_intelligence.wells.repository.WellRepository.find_well`): "Rig 42" and "rig 42"
in one workspace are either the same rig entered twice or two rigs, and a repository that folded the
case would decide which without telling anybody.  `get_or_create_*` therefore re-finds only what it
can name exactly, and the audit trail records the id of the row it settled on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import KnowledgeRelationType
from ..core.errors import ValidationError
from ..core.ids import new_id
from ..database.integrity import create_knowledge_relation
from ..database.models import (
    Company,
    KnowledgeRelation,
    NptRecord,
    Rig,
    ServiceCompany,
    Well,
    WellOperation,
)
from ..wells.repository import WellRepository
from .repository import OperationsRepository, _stamp

__all__ = ["RIG_STATUSES", "SERVICE_STATUSES", "AssetRepository"]

#: What a rig can be described as doing.  ``AVAILABLE`` is not ``ACTIVE``: a stack of iron in a yard
#: is available, and only a schedule says whether it is drilling.
RIG_STATUSES: frozenset[str] = frozenset({"ACTIVE", "STANDBY", "MAINTENANCE", "DECOMMISSIONED"})
SERVICE_STATUSES: frozenset[str] = frozenset({"ACTIVE", "INACTIVE"})


def _status(raw: object, allowed: frozenset[str], label: str) -> str:
    value = str(getattr(raw, "value", raw) or "").strip().upper()
    if value not in allowed:
        raise ValidationError(
            f"{label} status {value!r} is not a status this platform knows",
            allowed=sorted(allowed),
        )
    return value


class AssetRepository:
    """The rig and vendor tables: identity, specification, and the links that hold history together.

    Deliberately small.  These are the two "foundations" in the brief whose whole job is to be
    referable, so the repository offers lookup, creation, a whitelisted update and the assignment
    edges - and stops, because the moment this class starts computing a rig's performance it is a
    second analytics service with a worse view of the data.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.wells = WellRepository(session)
        #: For the ``link`` path a caller may want after assigning an asset to an operation.
        self.records = OperationsRepository(session)

    # -- rigs -----------------------------------------------------------------
    def find_rig(self, name: str) -> Rig | None:
        return self.session.execute(
            select(Rig).where(Rig.name == str(name or "").strip()).order_by(Rig.created_at, Rig.id)
        ).scalar_one_or_none()

    def get_rig(self, rig_id: str) -> Rig:
        row = self.session.get(Rig, str(rig_id))
        if row is None:
            raise ValidationError(f"no rig {rig_id!r}")
        return row

    def get_or_create_rig(
        self,
        name: str,
        *,
        company_id: str = "",
        operator: str = "",
        model: str = "",
        status: str = "ACTIVE",
        horsepower: float | None = None,
        drift_length: tuple[float, str] | None = None,
        specifications: Mapping[str, Any] | None = None,
        source_id: str = "",
        notes: str = "",
        created_by: str = "system",
        created_at: object = None,
    ) -> tuple[Rig, bool]:
        """The rig called ``name``, created if the workspace has never heard of it.

        Returns the row and whether it was created, because a caller reconciling a report needs to
        know whether it just added an asset to the portfolio or found one it could have asked about.
        """
        key = str(name or "").strip()
        if not key:
            raise ValidationError("a rig needs a name", hint="rigs are found by name, by design")
        found = self.find_rig(key)
        if found is not None:
            return found, False
        if company_id and self.session.get(Company, str(company_id)) is None:
            raise ValidationError(f"no company {company_id!r} to own the rig")
        if horsepower is not None and float(horsepower) < 0:
            raise ValidationError("rig horsepower cannot be negative", horsepower=horsepower)
        row = Rig(
            id=new_id("rig"),
            name=key,
            company_id=company_id or None,
            model=str(model or "") or None,
            status=_status(status, RIG_STATUSES, "rig"),
            horsepower=None if horsepower is None else float(horsepower),
            specifications=dict(specifications or {}),
            source_id=source_id or None,
            notes=str(notes or "") or None,
            created_by=created_by or "system",
            attributes={},
        )
        if drift_length is not None:
            value, unit = drift_length
            # Rigs predate this layer and carry no depth-capability columns; the figure a report
            # gave is kept as a specification rather than inventing a column for one field.
            row.specifications = {
                **row.specifications,
                "drift_length": {"value": float(value), "unit": str(unit)},
            }
        if operator:
            # An operator named in a report is not necessarily a `company` row in this workspace, so
            # the wording is kept rather than coerced into a foreign key that would have to be minted.
            row.specifications = {**row.specifications, "operator": str(operator)}
        if created_at is not None:
            # A rig a report mentioned as already drilling has to be enterable with its history, so
            # that the timeline does not show a rig appearing the day the workspace learned of it.
            stamp = _stamp(created_at)
            if stamp is None:
                raise ValidationError("created_at must be an ISO-8601 date or datetime")
            row.created_at = stamp
            row.updated_at = stamp
        self.session.add(row)
        self.session.flush()
        return row, True

    def list_rigs(self, *, company_id: str = "", status: str = "", limit: int = 200) -> list[Rig]:
        statement = select(Rig)
        if company_id:
            statement = statement.where(Rig.company_id == company_id)
        if status:
            statement = statement.where(Rig.status == _status(status, RIG_STATUSES, "rig"))
        return list(
            self.session.execute(
                statement.order_by(Rig.name, Rig.id).limit(max(0, int(limit)))
            ).scalars()
        )

    #: The rig fields a caller may change.  Anything else is either plumbing or somebody else's row.
    RIG_FIELDS: frozenset[str] = frozenset(
        {
            "name",
            "company_id",
            "model",
            "status",
            "horsepower",
            "specifications",
            "source_id",
            "notes",
        }
    )

    def update_rig(self, rig_id: str, **changes: Any) -> tuple[Rig, dict[str, Any]]:
        """Apply the allowed subset of ``changes``; return the row and what was applied.

        The applied dict is returned rather than ignored because the CLI has to say what it did: a
        caller who passed ``perf_score`` needs to be told it was dropped, not left to assume a rig
        now carries one.
        """
        row = self.get_rig(rig_id)
        unknown = sorted(set(changes) - self.RIG_FIELDS)
        if unknown:
            raise ValidationError(
                f"rig has no updatable field named {', '.join(unknown)}",
                allowed=sorted(self.RIG_FIELDS),
                hint="a rig stores identity and specification; capability is computed from operations",
            )
        applied = {key: self._clean_rig_field(key, value) for key, value in changes.items()}
        for key, value in applied.items():
            setattr(row, key, value)
        self.session.flush()
        return row, applied

    def _clean_rig_field(self, key: str, value: Any) -> Any:
        """Normalise and validate one field before it reaches a rig row.

        A method rather than a branch in the loop: the same rules have to hold for a create and an
        update, and an update path that forgot one of them is how a negative horsepower gets in.
        """
        if key == "status":
            return _status(value, RIG_STATUSES, "rig")
        if key == "horsepower":
            if value is None:
                return None
            number = float(value)
            if number < 0:
                raise ValidationError("rig horsepower cannot be negative", horsepower=number)
            return number
        if key == "name":
            name = str(value or "").strip()
            if not name:
                raise ValidationError("a rig needs a name")
            return name
        if key == "company_id":
            if not value:
                return None
            if self.session.get(Company, str(value)) is None:
                raise ValidationError(f"no company {value!r}")
            return str(value)
        if key in {"specifications", "attributes"}:
            return dict(value or {})
        return value

    # -- service companies ----------------------------------------------------
    def find_service_company(self, name: str, *, service_type: str = "") -> ServiceCompany | None:
        """A vendor by name, exact, as rigs and wells are found.

        ``service_type`` is compared lower-case because it is a *category* a person types once and
        filters on often; the name is not, because it is what identifies the company.
        """
        key = str(name or "").strip()
        if not key:
            return None
        statement = select(ServiceCompany).where(ServiceCompany.name == key)
        if service_type:
            statement = statement.where(
                ServiceCompany.service_type == str(service_type).strip().lower()
            )
        return self.session.execute(
            statement.order_by(ServiceCompany.created_at, ServiceCompany.id).limit(1)
        ).scalar_one_or_none()

    def get_service_company(self, company_row_id: str) -> ServiceCompany:
        row = self.session.get(ServiceCompany, str(company_row_id))
        if row is None:
            raise ValidationError(f"no service company {company_row_id!r}")
        return row

    def get_or_create_service_company(
        self,
        name: str,
        *,
        company_id: str = "",
        service_type: str = "",
        status: str = "ACTIVE",
        contract_reference: str = "",
        notes: str = "",
        created_by: str = "system",
    ) -> tuple[ServiceCompany, bool]:
        key = str(name or "").strip()
        if not key:
            raise ValidationError("a service company needs a name")
        found = self.find_service_company(key)
        if found is not None:
            return found, False
        row = ServiceCompany(
            id=new_id("svc"),
            company_id=company_id or None,
            name=key,
            service_type=str(service_type or "").strip().lower() or None,
            status=_status(status, SERVICE_STATUSES, "service company"),
            contract_reference=str(contract_reference or "") or None,
            notes=str(notes or "") or None,
            created_by=created_by or "system",
            attributes={},
        )
        self.session.add(row)
        self.session.flush()
        return row, True

    def list_service_companies(
        self, *, service_type: str = "", status: str = "", limit: int = 200
    ) -> list[ServiceCompany]:
        statement = select(ServiceCompany)
        if service_type:
            statement = statement.where(
                ServiceCompany.service_type == str(service_type).strip().lower()
            )
        if status:
            statement = statement.where(
                ServiceCompany.status == _status(status, SERVICE_STATUSES, "service company")
            )
        return list(
            self.session.execute(
                statement.order_by(ServiceCompany.name, ServiceCompany.id).limit(max(0, int(limit)))
            ).scalars()
        )

    SERVICE_FIELDS: frozenset[str] = frozenset(
        {"name", "company_id", "service_type", "status", "contract_reference", "notes"}
    )

    def update_service_company(
        self, company_row_id: str, **changes: Any
    ) -> tuple[ServiceCompany, dict[str, Any]]:
        row = self.get_service_company(company_row_id)
        unknown = sorted(set(changes) - self.SERVICE_FIELDS)
        if unknown:
            raise ValidationError(
                f"service company has no updatable field named {', '.join(unknown)}",
                allowed=sorted(self.SERVICE_FIELDS),
                hint="performance belongs to the operations and events this vendor took part in",
            )
        applied = {key: self._clean_service_field(key, value) for key, value in changes.items()}
        for key, value in applied.items():
            setattr(row, key, value)
        self.session.flush()
        return row, applied

    def _clean_service_field(self, key: str, value: Any) -> Any:
        """Normalise one vendor field: the same rules on create and update, in one place."""
        if key == "status":
            return _status(value, SERVICE_STATUSES, "service company")
        if key == "name":
            name = str(value or "").strip()
            if not name:
                raise ValidationError("a service company needs a name")
            return name
        if key == "service_type":
            return str(value or "").strip().lower() or None
        if key == "company_id":
            if not value:
                return None
            if self.session.get(Company, str(value)) is None:
                raise ValidationError(f"no company {value!r}")
            return str(value)
        return value

    # -- assignment: the edges that make an asset's history a query ----------
    def assign_rig_to_well(
        self,
        *,
        well_id: str,
        rig_id: str,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        note: str = "",
    ) -> None:
        """Say which rig drilled a well, as a graph edge rather than a column.

        A well moves rig mid-hole, so "the well's rig" cannot be one row's property without lying
        about the rest of the hole; the operation rows carry ``rig_id`` for the periods that did, and
        this edge is the summary the timeline builds.
        """
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        self.get_rig(rig_id)
        create_knowledge_relation(
            self.session,
            source_type="well",
            source_id=well.id,
            relation=KnowledgeRelationType.WELL_USED_RIG.value,
            target_type="rig",
            target_id=str(rig_id),
            provenance=[dict(item) for item in provenance or ()],
            note=note or "rig assignment",
        )

    def assign_service_company_to_well(
        self,
        *,
        well_id: str,
        service_company_id: str,
        provenance: Sequence[Mapping[str, Any]] | None = None,
        note: str = "",
    ) -> None:
        well = self.session.get(Well, str(well_id))
        if well is None:
            raise ValidationError(f"no well {well_id!r}")
        self.get_service_company(service_company_id)
        create_knowledge_relation(
            self.session,
            source_type="well",
            source_id=well.id,
            relation=KnowledgeRelationType.WELL_USED_SERVICE.value,
            target_type="service_company",
            target_id=str(service_company_id),
            provenance=[dict(item) for item in provenance or ()],
            note=note or "service assignment",
        )

    def _targets(self, *, source_type: str, source_id: str, relation: str) -> list[str]:
        """The ids on the far side of one kind of edge, in the order they were written."""
        return list(
            self.session.execute(
                select(KnowledgeRelation.target_id)
                .where(
                    KnowledgeRelation.source_type == source_type,
                    KnowledgeRelation.source_id == str(source_id),
                    KnowledgeRelation.relation == relation,
                )
                .order_by(KnowledgeRelation.created_at, KnowledgeRelation.id)
            ).scalars()
        )

    def rigs_for_well(self, well_id: str) -> list[Rig]:
        """The rigs this well was assigned to, read out of the graph - not guessed from names."""
        ids = self._targets(
            source_type="well",
            source_id=well_id,
            relation=KnowledgeRelationType.WELL_USED_RIG.value,
        )
        if not ids:
            return []
        return list(
            self.session.execute(
                select(Rig).where(Rig.id.in_(ids)).order_by(Rig.name, Rig.id)
            ).scalars()
        )

    def service_companies_for_well(self, well_id: str) -> list[ServiceCompany]:
        ids = self._targets(
            source_type="well",
            source_id=well_id,
            relation=KnowledgeRelationType.WELL_USED_SERVICE.value,
        )
        if not ids:
            return []
        return list(
            self.session.execute(
                select(ServiceCompany)
                .where(ServiceCompany.id.in_(ids))
                .order_by(ServiceCompany.name, ServiceCompany.id)
            ).scalars()
        )

    def operations_using(self, *, rig_id: str = "", service_company_id: str = "") -> list[Any]:
        """The operations recorded on one rig or with one vendor: the asset's actual history.

        A list of operation rows rather than a count, because the count anybody wants (days, NPT
        hours) is an aggregation over these and belongs to the intelligence service, which can also
        say what it excluded.
        """
        if bool(rig_id) == bool(service_company_id):
            raise ValidationError(
                "name exactly one of rig_id or service_company_id",
                hint="a joined query over both is a question about the intersection, not about either",
            )
        column = WellOperation.rig_id if rig_id else WellOperation.service_company_id
        wanted = rig_id or service_company_id
        return list(
            self.session.execute(
                select(WellOperation)
                .where(column == str(wanted))
                .order_by(WellOperation.started_at.nulls_last(), WellOperation.id)
            ).scalars()
        )

    def npt_on_rig(self, rig_id: str) -> list[Any]:
        """The NPT rows recorded against one rig, for the summary a rig screen shows."""
        return list(
            self.session.execute(
                select(NptRecord)
                .where(NptRecord.rig_id == str(rig_id))
                .order_by(NptRecord.started_at.nulls_last(), NptRecord.id)
            ).scalars()
        )
