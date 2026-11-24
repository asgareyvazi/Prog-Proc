"""Survey CSV import service (Phase I / D-21).

Documented CSV column schema (header row required, case-insensitive):

    md, md_unit, inclination, inclination_unit, azimuth, azimuth_unit

- ``md`` (measured depth) is required and must be a finite number; ``md_unit`` is
  required and is stored verbatim — never guessed.
- ``inclination``/``azimuth`` are required finite numbers with explicit units.
- Duplicate or non-increasing MD values are validation errors.
- A unit column left empty is a validation error, not a guess.

The import persists a SurveyRun plus its SurveyStation rows with full provenance
(source file name, SHA-256, row locators). Re-importing identical bytes for the
same well is idempotent (same identity_key). No TVD/Northing/Easting/DLS values are
computed: trajectory calculations stay OWNER-GATED and disabled.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..database.models import SurveyRun, SurveyStation

#: Required CSV columns (lowercase). Extra columns are preserved in provenance only.
REQUIRED_COLUMNS = ("md", "md_unit", "inclination", "inclination_unit", "azimuth", "azimuth_unit")

#: Hard ingestion limits (resource bounds, not engineering values).
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_STATIONS = 100_000


@dataclass
class SurveyImportResult:
    survey_run_id: str = ""
    well_id: str = ""
    station_count: int = 0
    md_unit: str = ""
    sha256: str = ""
    already_existing: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _parse_float(raw: str, *, column: str, row: int) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"row {row}: column {column!r} is not a finite number", value=raw
        ) from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError(f"row {row}: column {column!r} is not finite", value=raw)
    return value


def parse_survey_csv(text: str) -> tuple[list[dict[str, Any]], str]:
    """Parse and validate survey CSV text. Returns (stations, sha256).

    Phase I contract: missing/duplicate/unknown headers, ragged or malformed rows,
    empty files, oversize inputs, excessive station counts, non-finite numbers,
    duplicate or non-increasing MD, and empty or inconsistent units are all refused
    with a specific error. Unknown units are preserved verbatim and never silently
    normalized or converted.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("survey CSV is empty")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValidationError(f"survey CSV exceeds the {MAX_CSV_BYTES}-byte limit")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        reader = csv.DictReader(io.StringIO(text))
        raw_header = list(reader.fieldnames or [])
        rows = list(enumerate(reader, start=2))  # header is line 1
    except csv.Error as exc:
        raise ValidationError(f"malformed CSV quoting: {exc}") from exc
    if not raw_header:
        raise ValidationError("survey CSV has no header row")
    header = [h.strip().lower() for h in raw_header]
    if len(header) != len(set(header)):
        dupes = sorted({h for h in header if header.count(h) > 1})
        raise ValidationError("survey CSV has duplicate header columns", duplicates=dupes)
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ValidationError(
            "survey CSV is missing required column(s)",
            missing=missing,
            expected=list(REQUIRED_COLUMNS),
        )
    unknown = [h for h in header if h not in REQUIRED_COLUMNS]
    if unknown:
        raise ValidationError(
            "survey CSV has unknown column(s); refusing to silently ignore them",
            unknown=unknown,
        )
    stations: list[dict[str, Any]] = []
    seen_md: set[float] = set()
    md_units: set[str] = set()
    inc_units: set[str] = set()
    az_units: set[str] = set()
    for row_number, row in rows:
        if row is None or all(not str(v or "").strip() for v in row.values()):
            continue  # empty rows are skipped, not treated as stations
        if None in row or any(v is None for v in row.values()):
            raise ValidationError(f"row {row_number}: ragged row (wrong column count)")
        raw = {k.strip().lower(): v for k, v in row.items() if k}
        md = _parse_float(raw.get("md", ""), column="md", row=row_number)
        md_unit = str(raw.get("md_unit") or "").strip()
        inc = _parse_float(raw.get("inclination", ""), column="inclination", row=row_number)
        inc_unit = str(raw.get("inclination_unit") or "").strip()
        az = _parse_float(raw.get("azimuth", ""), column="azimuth", row=row_number)
        az_unit = str(raw.get("azimuth_unit") or "").strip()
        if not md_unit or not inc_unit or not az_unit:
            raise ValidationError(
                f"row {row_number}: units must be explicit; refusing to guess them"
            )
        md_units.add(md_unit)
        inc_units.add(inc_unit)
        az_units.add(az_unit)
        if md in seen_md:
            raise ValidationError(f"row {row_number}: duplicate measured depth {md}")
        seen_md.add(md)
        stations.append(
            {
                "sequence": len(stations) + 1,
                "md": md,
                "md_unit": md_unit,
                "inclination": inc,
                "inclination_unit": inc_unit,
                "azimuth": az,
                "azimuth_unit": az_unit,
                "source_row": row_number,
            }
        )
        if len(stations) > MAX_STATIONS:
            raise ValidationError(f"survey CSV exceeds the {MAX_STATIONS}-station limit")
    if not stations:
        raise ValidationError("survey CSV contains no station rows")
    if len(md_units) > 1 or len(inc_units) > 1 or len(az_units) > 1:
        raise ValidationError(
            "inconsistent units across rows; refusing to convert or normalize",
            md_units=sorted(md_units),
            inclination_units=sorted(inc_units),
            azimuth_units=sorted(az_units),
        )
    mds = [s["md"] for s in stations]
    if mds != sorted(mds):
        raise ValidationError("stations must be ordered by increasing measured depth")
    return stations, sha


def import_survey_csv(
    session: Any,
    *,
    well_id: str,
    source_path: Path | str,
    text: str,
    run_label: str = "",
    created_by: str = "survey-import",
) -> SurveyImportResult:
    """Validate and persist one survey CSV as a CANDIDATE SurveyRun (never approved)."""
    from ..wells.repository import WellRepository

    well = WellRepository(session).get_well(well_id)
    if well is None:
        raise ValidationError("unknown well", well_id=well_id)
    stations, sha = parse_survey_csv(text)
    identity = f"survey-csv:{well_id}:{sha}"
    existing = session.query(SurveyRun).filter(SurveyRun.identity_key == identity).one_or_none()
    result = SurveyImportResult(
        well_id=well_id, station_count=len(stations), md_unit=stations[0]["md_unit"], sha256=sha
    )
    if existing is not None:
        result.survey_run_id = existing.id
        result.already_existing = True
        return result
    run = SurveyRun(
        id=f"sur-{uuid.uuid4().hex[:24]}",
        well_id=well_id,
        run_label=run_label or Path(source_path).name,
        survey_date_text=None,
        station_count=len(stations),
        min_md_value=min(s["md"] for s in stations),
        max_md_value=max(s["md"] for s in stations),
        md_unit=stations[0]["md_unit"],
        station_identity="NUMBERED",
        record_state="ACTUAL",
        status="CANDIDATE",
        origin="MANUAL",
        created_by=created_by,
        provenance=[
            {
                "source_file": Path(source_path).name,
                "sha256": sha,
                "format": "survey-csv/v1",
                "columns": list(REQUIRED_COLUMNS),
            }
        ],
        identity_key=identity,
        is_current=True,
    )
    session.add(run)
    session.flush()
    for st in stations:
        session.add(
            SurveyStation(
                id=f"sst-{uuid.uuid4().hex[:24]}",
                survey_run_id=run.id,
                well_id=well_id,
                sequence=st["sequence"],
                station_number_text=str(st["sequence"]),
                md_value=st["md"],
                md_unit=st["md_unit"],
                md_text=str(st["md"]),
                inclination_value=st["inclination"],
                inclination_unit=st["inclination_unit"],
                inclination_text=str(st["inclination"]),
                azimuth_value=st["azimuth"],
                azimuth_unit=st["azimuth_unit"],
                azimuth_text=str(st["azimuth"]),
                provenance=[{"source_row": st["source_row"], "sha256": sha}],
            )
        )
    session.flush()
    result.survey_run_id = run.id
    return result
