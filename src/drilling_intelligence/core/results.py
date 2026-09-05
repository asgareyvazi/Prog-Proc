"""Small shared value objects: extracted fields, QA findings, generic outcomes.

``DataField`` implements the data-quality contract of master spec section 58:
every extracted value carries value + unit + source + provenance + confidence +
status.  Engineering inputs, program requirements and QA checks all reuse it so
that "how good is this number?" has one answer everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from .enums import CheckResult, DataQuality
from .errors import CalculationInputError
from .provenance import Provenance
from .units import Dimension, Quantity, resolve_unit


@dataclass
class DataField:
    """A single extracted/derived value with its full quality envelope."""

    name: str
    value: Any = None
    unit: str = ""
    dimension: str = ""
    quality: DataQuality = DataQuality.UNVERIFIED
    provenance: Provenance | None = None
    confidence: float | None = None
    #: Where the value came from, in plain words (e.g. "cell C14 (value cache)").
    method: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.quality, str):
            self.quality = DataQuality(self.quality)

    @property
    def is_usable(self) -> bool:
        return self.value is not None and self.quality in (DataQuality.VALID, DataQuality.INFERRED)

    def quantity(self) -> Quantity | None:
        """Typed quantity if this field is numeric with a known unit."""
        if self.value is None or not self.unit:
            return None
        try:
            return Quantity.of(float(self.value), resolve_unit(self.unit))
        except Exception:  # noqa: BLE001 - a non-numeric field simply has no quantity
            return None

    def require_quantity(self, dimension: Dimension | str) -> Quantity:
        """Return the value as a :class:`Quantity` of the expected dimension, or fail loudly."""
        expected = dimension if isinstance(dimension, Dimension) else Dimension(dimension)
        if self.value is None:
            raise CalculationInputError(
                f"Required input missing: {self.name}",
                field=self.name,
                quality=self.quality.value,
            )
        if not self.unit:
            raise CalculationInputError(
                f"Required input has no unit: {self.name}. The platform does not guess units.",
                field=self.name,
            )
        quantity = self.quantity()
        if quantity is None:
            raise CalculationInputError(
                f"Required input is not numeric: {self.name}={self.value!r}",
                field=self.name,
            )
        if quantity.dimension is not expected:
            raise CalculationInputError(
                f"Input {self.name} has dimension {quantity.dimension.value}, expected {expected.value}",
                field=self.name,
                unit=self.unit,
            )
        return quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "dimension": self.dimension or (resolve_unit(self.unit).dimension.value if self.unit else ""),
            "quality": self.quality.value,
            "confidence": self.confidence,
            "method": self.method,
            "note": self.note,
            "provenance": self.provenance.to_dict() if self.provenance else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DataField:
        provenance = payload.get("provenance")
        return cls(
            name=payload["name"],
            value=payload.get("value"),
            unit=payload.get("unit", "") or "",
            dimension=payload.get("dimension", "") or "",
            quality=DataQuality(payload.get("quality", DataQuality.UNVERIFIED.value)),
            provenance=Provenance.from_dict(provenance) if provenance else None,
            confidence=payload.get("confidence"),
            method=payload.get("method", "") or "",
            note=payload.get("note", "") or "",
        )


@dataclass
class Finding:
    """One QA/QC or validation result (section 30)."""

    code: str
    result: CheckResult
    message: str
    field: str = ""
    expected: Any = None
    actual: Any = None
    severity_rank: int = 0
    provenance: list[Provenance] = dc_field(default_factory=list)
    remediation: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.result, str):
            self.result = CheckResult(self.result)

    @property
    def is_blocking(self) -> bool:
        return self.result in (CheckResult.ERROR, CheckResult.CONFLICT, CheckResult.MISSING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "result": self.result.value,
            "message": self.message,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "remediation": self.remediation,
            "provenance": [p.to_dict() for p in self.provenance],
        }


#: Ordering used to roll up a set of findings into a single verdict.
_RESULT_RANK = {
    CheckResult.PASS: 0,
    CheckResult.WARNING: 1,
    CheckResult.CONFLICT: 3,
    CheckResult.MISSING: 4,
    CheckResult.ERROR: 5,
}


def worst_result(results: list[CheckResult]) -> CheckResult:
    if not results:
        return CheckResult.PASS
    return max(results, key=lambda r: _RESULT_RANK[r])


@dataclass
class FindingSet:
    """Aggregated findings with a deterministic rollup."""

    checks: list[Finding] = dc_field(default_factory=list)

    def add(self, finding: Finding) -> Finding:
        self.checks.append(finding)
        return finding

    def add_all(self, findings: list[Finding]) -> None:
        self.checks.extend(findings)

    @property
    def result(self) -> CheckResult:
        return worst_result([c.result for c in self.checks])

    def by_code(self, code: str) -> list[Finding]:
        return [c for c in self.checks if c.code == code]

    @property
    def blocking(self) -> list[Finding]:
        return [c for c in self.checks if c.is_blocking]

    def counts(self) -> dict[str, int]:
        tally = {r.value: 0 for r in CheckResult}
        for check in self.checks:
            tally[check.result.value] += 1
        return tally

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result.value, "counts": self.counts(), "checks": [c.to_dict() for c in self.checks]}


@dataclass
class Outcome:
    """Uniform "did it work, what came out, what went wrong" envelope.

    Used by ingestion, extraction, calculation and the AI tools so the UI can
    present every service result the same way.
    """

    ok: bool
    data: Any = None
    findings: FindingSet = dc_field(default_factory=FindingSet)
    error: str = ""
    error_code: str = ""
    hint: str = ""
    duration_ms: float = 0.0

    @classmethod
    def success(cls, data: Any = None, **kwargs: Any) -> Outcome:
        return cls(ok=True, data=data, **kwargs)

    @classmethod
    def failure(cls, error: str, code: str = "ERROR", hint: str = "", data: Any = None) -> Outcome:
        return cls(ok=False, error=error, error_code=code, hint=hint, data=data)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "duration_ms": round(self.duration_ms, 1)}
        if self.error:
            payload["error"] = self.error
            payload["error_code"] = self.error_code
            payload["hint"] = self.hint
        if self.data is not None:
            payload["data"] = self.data
        if self.findings.checks:
            payload["findings"] = self.findings.to_dict()
        return payload


__all__ = ["DataField", "Finding", "FindingSet", "Outcome", "worst_result"]
