"""Survey Import & Review page (Phase I / D-21).

Real desktop workflow for synthetic/source survey CSVs: pick a CSV, pick a well,
validate (units explicit, ordered MD, no duplicates), persist a CANDIDATE
SurveyRun with per-station provenance, and inspect the normalized station table
side by side with the source rows. Trajectory calculations stay OWNER-GATED and
are never computed here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import DrillingIntelligenceError
from ..operations.survey_import import import_survey_csv, parse_survey_csv


class SurveyImportPage(QWidget):
    def __init__(self, workspace_getter: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_getter = workspace_getter
        self._csv_path: Path | None = None
        self._build_ui()
        self._reload_wells()
        self._reload_runs()

    def _workspace(self) -> Any:
        workspace = self._workspace_getter()
        if workspace is None:
            raise DrillingIntelligenceError("no workspace is open")
        return workspace

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "<b>Survey Import &amp; Review</b> — CSV schema: "
                "md, md_unit, inclination, inclination_unit, azimuth, azimuth_unit "
                "(synthetic fixtures must be labelled synthetic). No trajectory "
                "calculation is performed; imported runs stay CANDIDATE."
            )
        )
        row = QHBoxLayout()
        self.file_button = QPushButton("Choose survey CSV…")
        self.file_button.clicked.connect(self._choose_file)
        self.file_label = QLabel("no file selected")
        self.well_combo = QComboBox()
        self.label_input = QLineEdit()
        self.label_input.setPlaceholderText("run label (optional)")
        self.import_button = QPushButton("Validate & import")
        self.import_button.clicked.connect(self._import)
        row.addWidget(self.file_button)
        row.addWidget(self.file_label)
        row.addWidget(QLabel("Well:"))
        row.addWidget(self.well_combo)
        row.addWidget(self.label_input)
        row.addWidget(self.import_button)
        root.addLayout(row)

        self.source_view = QTextEdit()
        self.source_view.setReadOnly(True)
        self.source_view.setPlaceholderText("Source CSV rows appear here after selection.")
        root.addWidget(QLabel("Source (read-only):"))
        root.addWidget(self.source_view, 1)

        self.station_table = QTableWidget(0, 7)
        # Phase C: every dimensional column shows its own explicit unit — MD, inclination
        # and azimuth units are three separate visible columns, never merged or hidden.
        self.station_table.setHorizontalHeaderLabels(
            ["#", "MD", "MD unit", "Incl", "Incl unit", "Az", "Az unit"]
        )
        self.station_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(QLabel("Normalized stations (persisted, CANDIDATE):"))
        root.addWidget(self.station_table, 2)

        self.run_combo = QComboBox()
        self.run_combo.currentIndexChanged.connect(self._show_run)
        root.addWidget(QLabel("Imported survey runs:"))
        root.addWidget(self.run_combo)
        self.status_label = QLabel("")
        root.addWidget(self.status_label)

    def _reload_wells(self) -> None:
        self.well_combo.clear()
        try:
            from ..wells.repository import WellRepository

            with self._workspace().database.read_only() as session:
                for well in WellRepository(session).list_wells():
                    self.well_combo.addItem(well.name, well.id)
        except DrillingIntelligenceError:
            pass

    def _reload_runs(self) -> None:
        self.run_combo.clear()
        try:
            from ..operations.repository import OperationsRepository

            with self._workspace().database.read_only() as session:
                runs = OperationsRepository(session).list_survey_runs()
            for run in runs:
                self.run_combo.addItem(
                    f"{run.run_label} — {run.station_count} stations [{run.status}]", run.id
                )
        except DrillingIntelligenceError:
            pass

    def _choose_file(self) -> None:
        try:
            self._workspace()
        except DrillingIntelligenceError as exc:
            self.status_label.setText(f"Error: {exc}")
            return
        paths, _ = QFileDialog.getOpenFileName(self, "Select survey CSV", "", "CSV files (*.csv)")
        if not paths:
            return
        self._csv_path = Path(paths)
        self.file_label.setText(self._csv_path.name)
        try:
            self.source_view.setPlainText(self._csv_path.read_text(encoding="utf-8"))
        except OSError as exc:
            self.status_label.setText(f"Error reading file: {exc}")

    def _import(self) -> None:
        if self._csv_path is None:
            self.status_label.setText("Choose a survey CSV first.")
            return
        well_id = self.well_combo.currentData()
        if not well_id:
            self.status_label.setText("Choose a registered well.")
            return
        try:
            text = self._csv_path.read_text(encoding="utf-8")
            stations, _sha = parse_survey_csv(text)  # validate before touching the DB
            with self._workspace().database.unit_of_work() as session:
                result = import_survey_csv(
                    session,
                    well_id=well_id,
                    source_path=self._csv_path,
                    text=text,
                    run_label=self.label_input.text().strip(),
                )
            self._fill_stations(stations)
            self._reload_runs()
            state = "already present (idempotent)" if result.already_existing else "imported"
            self.status_label.setText(
                f"Survey {state}: run {result.survey_run_id[:12]}… "
                f"{result.station_count} stations, md unit {result.md_unit!r}, CANDIDATE."
            )
        except Exception as exc:  # noqa: BLE001 - surfaced non-modally
            self.status_label.setText(f"Error: {exc}")

    def _fill_stations(self, stations: list[dict[str, Any]]) -> None:
        self.station_table.setRowCount(0)
        for st in stations:
            row = self.station_table.rowCount()
            self.station_table.insertRow(row)
            for col, value in enumerate(
                [
                    st["sequence"],
                    st["md"],
                    st["md_unit"],
                    st["inclination"],
                    st["inclination_unit"],
                    st["azimuth"],
                    st["azimuth_unit"],
                ]
            ):
                self.station_table.setItem(row, col, QTableWidgetItem(str(value)))

    def _show_run(self) -> None:
        run_id = self.run_combo.currentData()
        if not run_id:
            return
        try:
            from ..operations.repository import OperationsRepository

            with self._workspace().database.read_only() as session:
                stations = OperationsRepository(session).list_survey_stations(survey_run_id=run_id)
            self._fill_stations(
                [
                    {
                        "sequence": s.sequence,
                        "md": s.md_value,
                        "md_unit": s.md_unit,
                        "inclination": s.inclination_value,
                        "inclination_unit": s.inclination_unit,
                        "azimuth": s.azimuth_value,
                        "azimuth_unit": s.azimuth_unit,
                    }
                    for s in stations
                ]
            )
            self.status_label.setText(
                f"Loaded persisted run {run_id[:12]}… ({len(stations)} stations)."
            )
        except DrillingIntelligenceError as exc:
            self.status_label.setText(f"Error: {exc}")
