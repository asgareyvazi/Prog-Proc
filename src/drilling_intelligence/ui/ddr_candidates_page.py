"""DDR Candidates page (Phase G, 2026-11-24B): full source-to-DDR UI journey.

List candidates of a document version (real page locators, explicit units, live
candidate-level state), dispose each candidate (ACCEPTED / EDITED / REJECTED —
written to the append-only ddr_disposition table through the shared service), and
authorize a CANDIDATE report through the strengthened gates. The page only calls
:class:`DdrWorkflowService`, the same surface the CLI uses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import DrillingIntelligenceError


class DdrCandidatesPage(QWidget):
    """Desktop source-to-DDR candidate workflow."""

    def __init__(self, workspace_getter: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_getter = workspace_getter
        self._candidates: list[dict[str, Any]] = []
        self._build_ui()

    def _workspace(self) -> Any:
        workspace = self._workspace_getter()
        if workspace is None:
            raise DrillingIntelligenceError("no workspace is open")
        return workspace

    def _service(self) -> Any:
        from ..operations.ddr_workflow import DdrWorkflowService

        return DdrWorkflowService.for_workspace(self._workspace())

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "<b>DDR Candidates</b> — map a DDR document version to candidates, dispose each "
                "one (append-only), and authorize a CANDIDATE report."
            )
        )
        top = QHBoxLayout()
        top.addWidget(QLabel("Version ID:"))
        self.version_input = QLineEdit()
        self.version_input.setPlaceholderText("document version id")
        top.addWidget(self.version_input)
        self.load_button = QPushButton("Load candidates")
        self.load_button.clicked.connect(self._load_candidates)
        self.well_combo = QComboBox()
        self.refresh_wells_button = QPushButton("Refresh wells")
        self.refresh_wells_button.clicked.connect(self._reload_wells)
        top.addWidget(self.load_button)
        top.addWidget(QLabel("Well:"))
        top.addWidget(self.well_combo)
        top.addWidget(self.refresh_wells_button)
        root.addLayout(top)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Field", "Value", "Unit", "Page", "Line", "Status", "Note"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)

        act = QHBoxLayout()
        self.actor_input = QLineEdit("operator")
        self.accept_button = QPushButton("Accept selected")
        self.reject_button = QPushButton("Reject selected")
        self.edit_value = QLineEdit()
        self.edit_value.setPlaceholderText("edited value (for Edit)")
        self.edit_button = QPushButton("Edit selected")
        self.authorize_button = QPushButton("Authorize CANDIDATE report")
        self.accept_button.clicked.connect(lambda: self._dispose("ACCEPTED"))
        self.reject_button.clicked.connect(lambda: self._dispose("REJECTED"))
        self.edit_button.clicked.connect(lambda: self._dispose("EDITED"))
        self.authorize_button.clicked.connect(self._authorize)
        act.addWidget(QLabel("Actor:"))
        act.addWidget(self.actor_input)
        act.addWidget(self.accept_button)
        act.addWidget(self.reject_button)
        act.addWidget(self.edit_value)
        act.addWidget(self.edit_button)
        act.addWidget(self.authorize_button)
        root.addLayout(act)

        self.status_label = QLabel("Enter a document version id and load candidates.")
        root.addWidget(self.status_label)
        self._reload_wells()

    def _reload_wells(self) -> None:
        self.well_combo.clear()
        self.well_combo.addItem("(no well)", None)
        try:
            from ..wells.repository import WellRepository

            with self._workspace().database.read_only() as session:
                for row in WellRepository(session).list_wells():
                    self.well_combo.addItem(row.name, row.id)
        except DrillingIntelligenceError:
            pass

    def _load_candidates(self) -> None:
        version_id = self.version_input.text().strip()
        if not version_id:
            self.status_label.setText("Enter a document version id first.")
            return
        try:
            self._candidates = self._service().list_candidates(version_id)
        except DrillingIntelligenceError as exc:
            self.status_label.setText(f"Error: {exc}")
            return
        self.table.setRowCount(0)
        for cand in self._candidates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            loc = cand.get("locator") or {}
            for col, value in enumerate(
                [
                    cand.get("field", ""),
                    cand.get("extracted_value", ""),
                    cand.get("unit", ""),
                    str(loc.get("page", "")),
                    str(loc.get("line", "")),
                    cand.get("status", ""),
                    cand.get("note", ""),
                ]
            ):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        self.status_label.setText(f"{len(self._candidates)} candidate(s) loaded.")

    def _selected_candidate(self) -> dict[str, Any] | None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows or rows[0] >= len(self._candidates):
            return None
        return self._candidates[rows[0]]

    def _dispose(self, decision: str) -> None:
        cand = self._selected_candidate()
        if cand is None:
            self.status_label.setText("Select a candidate row first.")
            return
        version_id = self.version_input.text().strip()
        try:
            self._service().dispose(
                version_id,
                cand["candidate_id"],
                decision,
                actor=self.actor_input.text().strip() or "operator",
                edited_value=self.edit_value.text() if decision == "EDITED" else "",
            )
        except DrillingIntelligenceError as exc:
            QMessageBox.warning(self, "Disposition refused", str(exc))
            self.status_label.setText(f"Error: {exc}")
            return
        self._load_candidates()
        self.status_label.setText(f"{decision} appended for {cand['field']}.")

    def _authorize(self) -> None:
        version_id = self.version_input.text().strip()
        well_id = self.well_combo.currentData()
        if not version_id or not well_id:
            self.status_label.setText("Version id and a registered well are required.")
            return
        try:
            report = self._service().authorize(
                version_id,
                well_id=str(well_id),
                actor=self.actor_input.text().strip() or "operator",
            )
        except DrillingIntelligenceError as exc:
            QMessageBox.warning(self, "Authorization refused", str(exc))
            self.status_label.setText(f"Error: {exc}")
            return
        self.status_label.setText(
            f"CANDIDATE report {report.report_number} authorized (id {report.id}); "
            "it stays CANDIDATE until the review lifecycle approves it."
        )
