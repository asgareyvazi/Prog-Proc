"""add the V4 BHA, bit-run and directional-survey domains

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-22

Three source-shaped domains become authoritative tables, following the shape the certified V3 mud
domain established rather than inventing a second one:

* ``bha_report`` / ``bha_component`` - one run and its ordered tally;
* ``bit_record`` - one row per bit run, because a replacement bit is a new run and not an edit;
* ``survey_run`` / ``survey_station`` - one survey set and its stations, so two surveys of the same
  well stay two surveys.

Every measurement column keeps the source's own text beside its parsed value and the source's own
unit beside both.  There are deliberately **no** computed columns: no assembly length, no bit footage
derived from depths in/out, and no TVD/northing/easting derived from inclination and azimuth.  A
number that is not in the source stays NULL.

``identity_key`` carries the semantic identity each domain's source actually states (BHA/run number,
bit/run number, survey set label plus station number) with the document version, so re-promotion is a
no-op and a genuinely new run is a new row.  ``is_current`` keeps source-version replacement separate
from human confirmation exactly as ``mud_report`` does.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bha_report",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("bha_number", sa.String(length=64), nullable=True),
        sa.Column("report_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report_date_text", sa.String(length=80), nullable=True),
        sa.Column("top_depth_value", sa.Float(), nullable=True),
        sa.Column("top_depth_unit", sa.String(length=16), nullable=False),
        sa.Column("bottom_depth_value", sa.Float(), nullable=True),
        sa.Column("bottom_depth_unit", sa.String(length=16), nullable=False),
        sa.Column("assembly_description", sa.Text(), nullable=True),
        sa.Column("component_count", sa.Integer(), nullable=False),
        sa.Column("section_resolution", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("document_status", sa.String(length=32), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=200), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_version.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_bha_report_identity")
    )
    op.create_index("ix_bha_report_number", "bha_report", ["well_id", "bha_number"], unique=False)
    op.create_index("ix_bha_report_version", "bha_report", ["document_version_id"], unique=False)
    op.create_index("ix_bha_report_well", "bha_report", ["well_id", "is_current"], unique=False)

    op.create_table(
        "bha_component",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bha_report_id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.Column("component_type", sa.String(length=48), nullable=False),
        sa.Column("stated_type", sa.String(length=120), nullable=False),
        sa.Column("manufacturer", sa.String(length=160), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("serial_number", sa.String(length=120), nullable=False),
        sa.Column("od_value", sa.Float(), nullable=True),
        sa.Column("od_unit", sa.String(length=16), nullable=False),
        sa.Column("od_text", sa.String(length=80), nullable=False),
        sa.Column("id_value", sa.Float(), nullable=True),
        sa.Column("id_unit", sa.String(length=16), nullable=False),
        sa.Column("id_text", sa.String(length=80), nullable=False),
        sa.Column("length_value", sa.Float(), nullable=True),
        sa.Column("length_unit", sa.String(length=16), nullable=False),
        sa.Column("length_text", sa.String(length=80), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("quality", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=200), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bha_report_id"], ["bha_report.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_version.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_bha_component_identity")
    )
    op.create_index("ix_bha_component_report", "bha_component", ["bha_report_id", "sequence"], unique=False)
    op.create_index("ix_bha_component_version", "bha_component", ["document_version_id"], unique=False)
    op.create_index("ix_bha_component_well", "bha_component", ["well_id", "component_type"], unique=False)

    op.create_table(
        "bit_record",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("bha_report_id", sa.String(length=36), nullable=True),
        sa.Column("bit_number", sa.String(length=64), nullable=False),
        sa.Column("run_number", sa.String(length=64), nullable=True),
        sa.Column("manufacturer", sa.String(length=160), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("bit_type", sa.String(length=120), nullable=False),
        sa.Column("iadc_code", sa.String(length=48), nullable=False),
        sa.Column("serial_number", sa.String(length=120), nullable=False),
        sa.Column("size_value", sa.Float(), nullable=True),
        sa.Column("size_unit", sa.String(length=16), nullable=False),
        sa.Column("size_text", sa.String(length=80), nullable=False),
        sa.Column("depth_in_value", sa.Float(), nullable=True),
        sa.Column("depth_in_unit", sa.String(length=16), nullable=False),
        sa.Column("depth_out_value", sa.Float(), nullable=True),
        sa.Column("depth_out_unit", sa.String(length=16), nullable=False),
        sa.Column("footage_value", sa.Float(), nullable=True),
        sa.Column("footage_unit", sa.String(length=16), nullable=False),
        sa.Column("rotating_hours", sa.Float(), nullable=True),
        sa.Column("drilling_hours", sa.Float(), nullable=True),
        sa.Column("pull_reason", sa.String(length=240), nullable=False),
        sa.Column("dull_grade", sa.String(length=120), nullable=False),
        sa.Column("nozzle_count", sa.Integer(), nullable=True),
        sa.Column("nozzle_size_text", sa.String(length=160), nullable=False),
        sa.Column("run_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_date_text", sa.String(length=80), nullable=True),
        sa.Column("section_resolution", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("document_status", sa.String(length=32), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=200), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bha_report_id"], ["bha_report.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_version.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_bit_record_identity")
    )
    op.create_index("ix_bit_record_number", "bit_record", ["well_id", "bit_number"], unique=False)
    op.create_index("ix_bit_record_version", "bit_record", ["document_version_id"], unique=False)
    op.create_index("ix_bit_record_well", "bit_record", ["well_id", "is_current"], unique=False)

    op.create_table(
        "survey_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("run_label", sa.String(length=80), nullable=False),
        sa.Column("survey_tool", sa.String(length=120), nullable=False),
        sa.Column("survey_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("survey_date_text", sa.String(length=80), nullable=True),
        sa.Column("station_count", sa.Integer(), nullable=False),
        sa.Column("min_md_value", sa.Float(), nullable=True),
        sa.Column("max_md_value", sa.Float(), nullable=True),
        sa.Column("md_unit", sa.String(length=16), nullable=False),
        sa.Column("station_identity", sa.String(length=24), nullable=False),
        sa.Column("section_resolution", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("document_status", sa.String(length=32), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=200), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_version.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_survey_run_identity")
    )
    op.create_index("ix_survey_run_version", "survey_run", ["document_version_id"], unique=False)
    op.create_index("ix_survey_run_well", "survey_run", ["well_id", "is_current"], unique=False)

    op.create_table(
        "survey_station",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("survey_run_id", sa.String(length=36), nullable=False),
        sa.Column("well_id", sa.String(length=36), nullable=False),
        sa.Column("section_id", sa.String(length=36), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("station_number_text", sa.String(length=48), nullable=False),
        sa.Column("md_value", sa.Float(), nullable=True),
        sa.Column("md_unit", sa.String(length=16), nullable=False),
        sa.Column("md_text", sa.String(length=80), nullable=False),
        sa.Column("inclination_value", sa.Float(), nullable=True),
        sa.Column("inclination_unit", sa.String(length=16), nullable=False),
        sa.Column("inclination_text", sa.String(length=80), nullable=False),
        sa.Column("azimuth_value", sa.Float(), nullable=True),
        sa.Column("azimuth_unit", sa.String(length=16), nullable=False),
        sa.Column("azimuth_text", sa.String(length=80), nullable=False),
        sa.Column("toolface_value", sa.Float(), nullable=True),
        sa.Column("toolface_unit", sa.String(length=16), nullable=False),
        sa.Column("toolface_text", sa.String(length=80), nullable=False),
        sa.Column("tvd_value", sa.Float(), nullable=True),
        sa.Column("tvd_unit", sa.String(length=16), nullable=False),
        sa.Column("tvd_text", sa.String(length=80), nullable=False),
        sa.Column("northing_value", sa.Float(), nullable=True),
        sa.Column("northing_unit", sa.String(length=16), nullable=False),
        sa.Column("northing_text", sa.String(length=80), nullable=False),
        sa.Column("easting_value", sa.Float(), nullable=True),
        sa.Column("easting_unit", sa.String(length=16), nullable=False),
        sa.Column("easting_text", sa.String(length=80), nullable=False),
        sa.Column("dls_value", sa.Float(), nullable=True),
        sa.Column("dls_unit", sa.String(length=24), nullable=False),
        sa.Column("dls_text", sa.String(length=80), nullable=False),
        sa.Column("quality", sa.String(length=24), nullable=False),
        sa.Column("record_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("identity_key", sa.String(length=200), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_version.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["section_id"], ["well_section.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["survey_run_id"], ["survey_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["well_id"], ["well.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_survey_station_identity")
    )
    op.create_index("ix_survey_station_run", "survey_station", ["survey_run_id", "sequence"], unique=False)
    op.create_index("ix_survey_station_version", "survey_station", ["document_version_id"], unique=False)
    op.create_index("ix_survey_station_well", "survey_station", ["well_id", "md_value"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_survey_station_well", table_name="survey_station")
    op.drop_index("ix_survey_station_version", table_name="survey_station")
    op.drop_index("ix_survey_station_run", table_name="survey_station")
    op.drop_table("survey_station")
    op.drop_index("ix_survey_run_well", table_name="survey_run")
    op.drop_index("ix_survey_run_version", table_name="survey_run")
    op.drop_table("survey_run")
    op.drop_index("ix_bit_record_well", table_name="bit_record")
    op.drop_index("ix_bit_record_version", table_name="bit_record")
    op.drop_index("ix_bit_record_number", table_name="bit_record")
    op.drop_table("bit_record")
    op.drop_index("ix_bha_component_well", table_name="bha_component")
    op.drop_index("ix_bha_component_version", table_name="bha_component")
    op.drop_index("ix_bha_component_report", table_name="bha_component")
    op.drop_table("bha_component")
    op.drop_index("ix_bha_report_well", table_name="bha_report")
    op.drop_index("ix_bha_report_version", table_name="bha_report")
    op.drop_index("ix_bha_report_number", table_name="bha_report")
    op.drop_table("bha_report")
