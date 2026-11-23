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
