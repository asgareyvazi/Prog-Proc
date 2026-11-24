"""D-21 service-level tests for survey CSV import."""

import pytest

from drilling_intelligence.core.errors import ValidationError
from drilling_intelligence.operations.survey_import import import_survey_csv, parse_survey_csv
from drilling_intelligence.wells.repository import WellRepository

GOOD = (
    "md,md_unit,inclination,inclination_unit,azimuth,azimuth_unit\n"
    "10,m,1.0,deg,10,deg\n20,m,2.0,deg,20,deg\n"
)


def test_parse_requires_all_columns():
    with pytest.raises(ValidationError):
        parse_survey_csv("md,inclination\n10,1\n")


def test_parse_rejects_missing_unit():
    with pytest.raises(ValidationError, match="units"):
        parse_survey_csv(
            "md,md_unit,inclination,inclination_unit,azimuth,azimuth_unit\n10,,1,deg,20,deg\n"
        )


def test_parse_rejects_unordered_md():
    with pytest.raises(ValidationError, match="ordered"):
        parse_survey_csv(
            "md,md_unit,inclination,inclination_unit,azimuth,azimuth_unit\n20,m,2,deg,20,deg\n10,m,1,deg,10,deg\n"
        )


def test_import_is_idempotent(workspace):
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="W1")
        r1 = import_survey_csv(session, well_id=well.id, source_path="s.csv", text=GOOD)
        assert r1.ok and not r1.already_existing
        r2 = import_survey_csv(session, well_id=well.id, source_path="s.csv", text=GOOD)
        assert r2.already_existing and r2.survey_run_id == r1.survey_run_id


# ---------------------------------------------------------------- Phase I contract
def _header() -> str:
    return "md,md_unit,inclination,inclination_unit,azimuth,azimuth_unit\n"


def test_duplicate_header_rejected():
    with pytest.raises(ValidationError, match="duplicate header"):
        parse_survey_csv(_header().replace("md,", "md,md,") + "1,m,1,deg,2,deg\n")


def test_unknown_column_rejected():
    with pytest.raises(ValidationError, match="unknown column"):
        parse_survey_csv(
            "md,md_unit,inclination,inclination_unit,azimuth,azimuth_unit,tvd\n1,m,1,deg,2,deg,3\n"
        )


def test_ragged_row_rejected():
    with pytest.raises(ValidationError, match="ragged"):
        parse_survey_csv(_header() + "1,m,1,deg\n")


def test_empty_file_and_empty_rows():
    with pytest.raises(ValidationError, match="empty"):
        parse_survey_csv("   \n")
    with pytest.raises(ValidationError, match="no station rows"):
        parse_survey_csv(_header() + "\n\n")


def test_inconsistent_units_rejected():
    with pytest.raises(ValidationError, match="inconsistent units"):
        parse_survey_csv(_header() + "1,m,1,deg,2,deg\n2,ft,1,deg,2,deg\n")


def test_unknown_unit_preserved_not_normalized(workspace):
    from drilling_intelligence.wells.repository import WellRepository

    text = _header() + "1,furlong,1,deg,2,deg\n2,furlong,1,deg,2,deg\n"
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="U1")
        result = import_survey_csv(session, well_id=well.id, source_path="u.csv", text=text)
        assert result.ok
    from drilling_intelligence.operations.repository import OperationsRepository

    with workspace.database.read_only() as session:
        stations = OperationsRepository(session).list_survey_stations(well_id=well.id)
    assert stations[0].md_unit == "furlong"  # source token preserved verbatim


# ------------------------------------------------- 2026-11-24B Phase I: byte-level SHA
import hashlib  # noqa: E402

from drilling_intelligence.operations.survey_import import (  # noqa: E402
    import_survey_csv_file,
    read_survey_file_bytes,
)


def test_phase_i_sha_is_over_original_bytes(tmp_path, workspace):
    raw = GOOD.encode("utf-8") + b"\r\n"  # CRLF on disk must change the digest
    p = tmp_path / "s.csv"
    p.write_bytes(raw)
    _, sha = read_survey_file_bytes(p)
    assert sha == hashlib.sha256(raw).hexdigest()
    with workspace.database.unit_of_work() as session:
        well = WellRepository(session).create_well(name="WB")
        r = import_survey_csv_file(session, well_id=well.id, source_path=p)
        assert r.ok and r.sha256 == sha


def test_phase_i_bounded_read_before_size_check(tmp_path):
    from drilling_intelligence.operations.survey_import import MAX_CSV_BYTES

    big = tmp_path / "big.csv"
    with big.open("wb") as fh:
        fh.write(b"x" * (MAX_CSV_BYTES + 100))
    with pytest.raises(ValidationError, match="byte limit"):
        read_survey_file_bytes(big)


def test_phase_i_symlink_source_refused(tmp_path):
    real = tmp_path / "real.csv"
    real.write_text(GOOD)
    link = tmp_path / "link.csv"
    link.symlink_to(real)
    with pytest.raises(ValidationError):
        read_survey_file_bytes(link)
