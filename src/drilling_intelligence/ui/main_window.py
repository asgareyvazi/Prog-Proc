"""Read-only desktop Review Workbench window.

The window is deliberately a presentation surface.  It receives ``DomainReview`` values from
``ReviewController`` and does not query the database, inspect source files, or calculate engineering
values.  A small QThread worker keeps review loading and optional citation auditing off the GUI event
loop.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt, QThread, QTimer, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import (
    ConfigurationError,
    DrillingIntelligenceError,
    ValidationError,
    WorkspaceError,
)
from ..database.integrity import KnowledgeIntegrityError
from ..review import DomainReview, ReviewConflict, ReviewRecord
from .controller import ReviewController, WellChoice
from .models import (
    MappingTableModel,
    ReviewRecordFilterProxy,
    ReviewRecordsModel,
    TableColumn,
)
from .worker import ReviewWorker, WorkerError

_NAVIGATION = (
    "Overview",
    "Sections",
    "Plan vs Actual",
    "Records",
    "Evidence",
    "Conflicts",
    "Relations",
    "Calculations",
    "History",
)


def _value_text(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    return str(value)


def _add_tree_value(parent: QTreeWidgetItem, key: str, value: Any) -> None:
    if isinstance(value, Mapping):
        item = QTreeWidgetItem(parent, [str(key), ""])
        for child_key, child_value in value.items():
            _add_tree_value(item, str(child_key), child_value)
        return
    if isinstance(value, (list, tuple)):
        item = QTreeWidgetItem(parent, [str(key), ""])
        for index, child_value in enumerate(value):
            _add_tree_value(item, str(index), child_value)
        return
    QTreeWidgetItem(parent, [str(key), _value_text(value)])


def _fill_tree(tree: QTreeWidget, payload: Mapping[str, Any] | None) -> None:
    tree.clear()
    if not payload:
        QTreeWidgetItem(tree.invisibleRootItem(), ["", "No detail selected."])
        return
    root = tree.invisibleRootItem()
    for key, value in payload.items():
        _add_tree_value(root, str(key), value)
    tree.expandToDepth(1)


def _configure_table(table: QTableView, model: Any) -> None:
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setSortingEnabled(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for column, definition in enumerate(getattr(model, "columns", ())):
        table.setColumnWidth(column, definition.width)


class MainWindow(QMainWindow):
    """The first read-only desktop consumer of the Domain Review contract."""

    def __init__(
        self,
        controller: ReviewController | None = None,
        *,
        startup_workspace: str = "",
        startup_well: str = "",
        config_path: str = "",
    ) -> None:
        super().__init__()
        self.controller = controller or ReviewController()
        self._review: DomainReview | None = None
        self._well_choices: tuple[WellChoice, ...] = ()
        self._review_thread: QThread | None = None
        self._review_worker: ReviewWorker | None = None
        self._config_path = config_path

        self.setWindowTitle("Prog-Proc — Review Workbench")
        self.resize(1500, 900)
        self._build_ui()
        self._set_busy(False)
        self._set_status("Open a workspace to begin.")
        if startup_workspace:
            QTimer.singleShot(0, lambda: self.open_workspace_path(startup_workspace, startup_well))

    # -- construction -----------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        controls = QHBoxLayout()
        self.open_button = QPushButton("Open workspace…")
        self.open_button.clicked.connect(self.choose_workspace)
        controls.addWidget(self.open_button)
        self.workspace_label = QLabel("No workspace open")
        self.workspace_label.setObjectName("workspaceLabel")
        controls.addWidget(self.workspace_label, 1)

        controls.addWidget(QLabel("Well"))
        self.well_combo = QComboBox()
        self.well_combo.setMinimumWidth(280)
        self.well_combo.currentIndexChanged.connect(self._well_changed)
        controls.addWidget(self.well_combo)

        controls.addWidget(QLabel("Review"))
        self.lifecycle_combo = QComboBox()
        self.lifecycle_combo.addItem("Current", "current")
        self.lifecycle_combo.addItem("History", "history")
        self.lifecycle_combo.currentIndexChanged.connect(self._lifecycle_changed)
        controls.addWidget(self.lifecycle_combo)

        self.load_button = QPushButton("Load review")
        self.load_button.clicked.connect(lambda: self.request_review(verify_citations=False))
        controls.addWidget(self.load_button)
        self.verify_button = QPushButton("Verify citations")
        self.verify_button.setToolTip(
            "Re-read recorded source-file citations through CitationAuditor"
        )
        self.verify_button.clicked.connect(lambda: self.request_review(verify_citations=True))
        controls.addWidget(self.verify_button)
        root.addLayout(controls)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        body = QSplitter(Qt.Orientation.Horizontal)
        self.navigation = QListWidget()
        self.navigation.setMaximumWidth(180)
        self.navigation.setMinimumWidth(150)
        for name in _NAVIGATION:
            self.navigation.addItem(QListWidgetItem(name))
        self.navigation.currentRowChanged.connect(self._navigation_changed)
        body.addWidget(self.navigation)

        self.pages = QStackedWidget()
        self._build_pages()
        body.addWidget(self.pages)
        body.setStretchFactor(1, 1)
        root.addWidget(body, 1)

        self.status_label = QLabel()
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.navigation.setCurrentRow(0)
        self.setStyleSheet(
            """
            QMainWindow { background: #20242a; color: #e7ebef; }
            QWidget { color: #e7ebef; }
            QLineEdit, QComboBox, QTableView, QTreeWidget, QListWidget {
                background: #292f36; border: 1px solid #48515c; border-radius: 3px;
                selection-background-color: #345e82;
            }
            QPushButton { padding: 5px 10px; }
            QLabel#workspaceLabel { font-weight: 600; }
            QLabel#statusLabel { color: #aeb9c4; }
            QLabel#warningBanner { background: #54441e; padding: 7px; }
            QGroupBox { border: 1px solid #48515c; margin-top: 9px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
            """
        )

    def _build_pages(self) -> None:
        self._build_overview_page()
        self._build_sections_page()
        self._build_plan_page()
        self._build_records_page()
        self._build_evidence_page()
        self._build_conflicts_page()
        self._build_relations_page()
        self._build_calculations_page()
        self._build_history_page()

    def _new_table(self, columns: Sequence[TableColumn]) -> tuple[QTableView, MappingTableModel]:
        table = QTableView()
        model = MappingTableModel(columns, parent=table)
        _configure_table(table, model)
        return table, model

    def _build_overview_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        identity = QGroupBox("Identity")
        form = QFormLayout(identity)
        self.overview_identity: dict[str, QLabel] = {}
        for key, title in (
            ("well", "Well"),
            ("well_id", "Well ID"),
            ("field", "Field"),
            ("project", "Project"),
            ("company", "Company"),
            ("schema", "Schema"),
        ):
            label = QLabel("—")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.overview_identity[key] = label
            form.addRow(title, label)
        layout.addWidget(identity)

        self.overview_counts = QLabel("No review loaded.")
        self.overview_counts.setWordWrap(True)
        layout.addWidget(self.overview_counts)
        self.observations_table, self.observations_model = self._new_table(
            (TableColumn("key", "Observation", 260), TableColumn("value", "Value", 420))
        )
        layout.addWidget(self.observations_table, 1)
        self.pages.addWidget(page)

    def _build_sections_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.sections_table, self.sections_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("sequence", "Seq", 60),
                    ("name", "Section", 180),
                    ("hole_size_in", "Hole size", 100),
                    ("casing_program", "Casing program", 210),
                    ("top_depth_value", "Top depth", 100),
                    ("bottom_depth_value", "Bottom depth", 110),
                    ("bottom_depth_unit", "Depth unit", 90),
                    ("actual_duration_days", "Actual days", 100),
                    ("actual_mud_weight_value", "Actual mud", 100),
                    ("actual_mud_weight_unit", "Mud unit", 90),
                )
            )
        )
        layout.addWidget(self.sections_table)
        self.pages.addWidget(page)

    def _build_plan_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plan_table, self.plan_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("section", "Section", 180),
                    ("section_id", "Section ID", 220),
                    ("metric", "Metric", 130),
                    ("status", "Status", 110),
                    ("planned", "Planned", 100),
                    ("actual", "Actual", 100),
                    ("variance", "Variance", 100),
                    ("unit", "Unit", 75),
                    ("program_id", "Program ID", 220),
                    ("target_id", "Target ID", 220),
                )
            )
        )
        layout.addWidget(self.plan_table)
        self.pages.addWidget(page)

    def _build_records_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Type"))
        self.record_type_filter = QComboBox()
        self.record_type_filter.currentTextChanged.connect(self._record_filters_changed)
        filters.addWidget(self.record_type_filter)
        filters.addWidget(QLabel("Status"))
        self.record_status_filter = QComboBox()
        self.record_status_filter.currentTextChanged.connect(self._record_filters_changed)
        filters.addWidget(self.record_status_filter)
        filters.addWidget(QLabel("State"))
        self.record_state_filter = QComboBox()
        self.record_state_filter.addItems(["All", "CURRENT", "HISTORICAL"])
        self.record_state_filter.currentTextChanged.connect(self._record_filters_changed)
        filters.addWidget(self.record_state_filter)
        self.record_text_filter = QLineEdit()
        self.record_text_filter.setPlaceholderText("Filter type, id, scope, or record data…")
        self.record_text_filter.textChanged.connect(self._record_filters_changed)
        filters.addWidget(self.record_text_filter, 1)
        layout.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.records_table = QTableView()
        self.records_model = ReviewRecordsModel(parent=self.records_table)
        self.records_proxy = ReviewRecordFilterProxy(parent=self.records_table)
        self.records_proxy.setSourceModel(self.records_model)
        _configure_table(self.records_table, self.records_proxy)
        self.records_table.selectionModel().currentChanged.connect(self._record_selection_changed)
        splitter.addWidget(self.records_table)
        self.record_detail = self._new_detail_tree()
        splitter.addWidget(self.record_detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)
        self.pages.addWidget(page)

    def _build_evidence_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.evidence_summary = QLabel(
            "Citation audit not run. Select Verify citations to re-read source files."
        )
        self.evidence_summary.setWordWrap(True)
        layout.addWidget(self.evidence_summary)
        self.evidence_table, self.evidence_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("record_type", "Type", 140),
                    ("record_id", "Record ID", 220),
                    ("provenance_count", "Provenance", 100),
                    ("evidence_count", "Evidence", 90),
                    ("citation_audit", "Citation", 130),
                )
            )
        )
        layout.addWidget(self.evidence_table, 1)
        self.audit_table, self.audit_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("identity", "Identity", 300),
                    ("source_type", "Source", 110),
                    ("citation", "Citation", 260),
                    ("status", "Status", 130),
                    ("check", "Check", 100),
                    ("detail", "Detail", 420),
                    ("expected_sha256", "Expected hash", 260),
                    ("actual_sha256", "Actual hash", 260),
                )
            )
        )
        layout.addWidget(self.audit_table, 1)
        self.pages.addWidget(page)

    def _build_conflicts_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.conflicts_table, self.conflicts_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("conflict_id", "Conflict ID", 220),
                    ("lookup_key", "Lookup key", 300),
                    ("property_name", "Property", 160),
                    ("status", "Status", 120),
                    ("current", "Current", 90),
                    ("candidate_count", "Candidates", 100),
                )
            )
        )
        splitter.addWidget(self.conflicts_table)
        self.conflict_detail = self._new_detail_tree()
        splitter.addWidget(self.conflict_detail)
        self.conflicts_table.selectionModel().currentChanged.connect(
            self._conflict_selection_changed
        )
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        self.pages.addWidget(page)

    def _build_relations_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.relations_table, self.relations_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("relation", "Relation", 180),
                    ("source_type", "Source type", 120),
                    ("source_id", "Source ID", 230),
                    ("target_type", "Target type", 120),
                    ("target_id", "Target ID", 230),
                    ("note", "Note", 260),
                    ("provenance", "Provenance", 260),
                )
            )
        )
        layout.addWidget(self.relations_table)
        self.pages.addWidget(page)

    def _build_calculations_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.calculations_table, self.calculations_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("record_id", "Calculation ID", 230),
                    ("method_id", "Method", 170),
                    ("method_version", "Version", 100),
                    ("calculation_type", "Type", 130),
                    ("status", "Status", 120),
                    ("current", "Current", 90),
                    ("indexed_inputs", "Indexed inputs", 110),
                    ("outputs", "Outputs", 260),
                    ("reproducibility", "Reproducibility", 250),
                )
            )
        )
        layout.addWidget(self.calculations_table)
        self.pages.addWidget(page)

    def _build_history_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.history_message = QLabel(
            "History mode shows preserved historical rows returned by DomainReviewService."
        )
        self.history_message.setWordWrap(True)
        layout.addWidget(self.history_message)
        self.history_table = QTableView()
        self.history_model = ReviewRecordsModel(parent=self.history_table)
        _configure_table(self.history_table, self.history_model)
        layout.addWidget(self.history_table)
        self.pages.addWidget(page)

    @staticmethod
    def _new_detail_tree() -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Field", "Value"])
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(False)
        tree.header().setStretchLastSection(True)
        return tree

    # -- workspace and review actions -------------------------------------
    @Slot()
    def choose_workspace(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Open existing workspace")
        if directory:
            self.open_workspace_path(directory)

    def open_workspace_path(self, path: str, well_reference: str = "") -> bool:
        try:
            info = self.controller.open_workspace(path, config_path=self._config_path or None)
            choices = tuple(self.controller.list_wells())
        except Exception as exc:  # noqa: BLE001  # preserve a visible UI boundary for all open failures
            self._show_exception("workspace", exc)
            return False

        self._well_choices = choices
        self.workspace_label.setText(f"{info['name']}  ·  {info['root']}")
        self.workspace_label.setToolTip(
            f"Database: {info['database_path']}\nSchema: {_value_text(info.get('schema'))}"
        )
        self.well_combo.blockSignals(True)
        self.well_combo.clear()
        for choice in choices:
            self.well_combo.addItem(choice.label, choice.well_id)
            self.well_combo.setItemData(
                self.well_combo.count() - 1,
                f"{choice.name} | {choice.lifecycle_status or 'status unavailable'} | {choice.well_id}",
                Qt.ItemDataRole.ToolTipRole,
            )
        selected = self._select_well(well_reference)
        self.well_combo.blockSignals(False)
        if not choices:
            self._review = None
            self._set_status("Workspace opened, but no registered wells are available.")
            self._set_busy(False)
            return True
        if selected >= 0:
            self.well_combo.setCurrentIndex(selected)
            self.request_review(verify_citations=False)
        return True

    def _select_well(self, reference: str = "") -> int:
        if not self._well_choices:
            return -1
        wanted = reference.strip()
        if wanted:
            for index, choice in enumerate(self._well_choices):
                if wanted in {choice.well_id, choice.name}:
                    return index
            self._set_status(f"No well matches {wanted!r}; showing the first registered well.")
        return 0

    @Slot(int)
    def _well_changed(self, _index: int) -> None:
        if self.well_combo.currentData() and self.controller.workspace is not None:
            self.request_review(verify_citations=False)

    @Slot(int)
    def _lifecycle_changed(self, _index: int) -> None:
        if self.well_combo.currentData() and self.controller.workspace is not None:
            self.request_review(verify_citations=False)

    def request_review(self, *, verify_citations: bool) -> None:
        if self._review_thread is not None:
            return
        well_id = str(self.well_combo.currentData() or "")
        if not well_id:
            self._set_status("Select a well before loading a review.")
            return
        lifecycle = str(self.lifecycle_combo.currentData() or "current")
        action = "Verifying citations" if verify_citations else "Loading review"
        self._set_status(f"{action} for {self.well_combo.currentText()} ({lifecycle})…")
        self.banner.setVisible(False)
        self._set_busy(True)

        thread = QThread(self)
        worker = ReviewWorker(
            self.controller,
            well_id,
            lifecycle,
            verify_citations,
            parent=None,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._review_succeeded)
        worker.failed.connect(self._review_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._review_thread_finished)
        self._review_thread = thread
        self._review_worker = worker
        thread.start()

    @Slot(object)
    def _review_succeeded(self, review: DomainReview) -> None:
        self.set_review(review)
        audit = "; citations audited" if review.citation_audit is not None else ""
        self._set_status(
            f"Loaded {review.record_count} records for {review.request.get('lifecycle', 'current')} review{audit}."
        )

    @Slot(object)
    def _review_failed(self, error: WorkerError) -> None:
        self._set_status(f"{error.category.title()} error: {error.message}", error=True)
        detail = error.message + (f"\n\nHint: {error.hint}" if error.hint else "")
        self.banner.setText(detail)
        self.banner.setObjectName("warningBanner")
        self.banner.setVisible(True)

    @Slot()
    def _review_thread_finished(self) -> None:
        thread = self._review_thread
        self._review_thread = None
        self._review_worker = None
        self._set_busy(False)
        if thread is not None:
            thread.deleteLater()

    # -- rendering --------------------------------------------------------
    def set_review(self, review: DomainReview) -> None:
        """Render a complete service result without reinterpreting it."""
        self._review = review
        request = review.request
        with QSignalBlocker(self.lifecycle_combo):
            index = self.lifecycle_combo.findData(request.get("lifecycle", "current"))
            if index >= 0:
                self.lifecycle_combo.setCurrentIndex(index)

        subject = review.subject
        for key, payload_key in (
            ("well", "well"),
            ("field", "field"),
            ("project", "project"),
            ("company", "company"),
        ):
            value = subject.get(payload_key) or {}
            self.overview_identity[key].setText(str(value.get("name") or "—"))
        self.overview_identity["well_id"].setText(str((subject.get("well") or {}).get("id") or "—"))
        schema = (
            self.controller.workspace.migration.to_dict() if self.controller.workspace else None
        )
        self.overview_identity["schema"].setText(_value_text(schema))

        count_text = (
            f"Mode: {request.get('lifecycle', 'current').upper()}   ·   "
            f"Records: {review.record_count}   ·   Sections: {len(review.sections)}   ·   "
            f"Conflicts: {len(review.conflicts)}   ·   Relations: {len(review.relations)}   ·   "
            f"Plan/actual rows: {len(review.plan_actual)}"
        )
        self.overview_counts.setText(count_text)
        self.observations_model.set_rows(
            [{"key": key, "value": value} for key, value in sorted(review.observations.items())]
        )
        self.sections_model.set_rows(review.sections)
        self.plan_model.set_rows(review.plan_actual)
        self.records_model.set_records(review.records)
        self.history_model.set_records([record for record in review.records if not record.current])
        self._refresh_record_filters(review.records)
        self._set_evidence(review)
        self._set_conflicts(review.conflicts)
        self.relations_model.set_rows(review.relations)
        self.calculations_model.set_rows(self._calculation_rows(review.records))
        self._clear_detail_views()

        if review.truncated:
            self.banner.setText(
                "Review result is truncated. Some domain groups may contain additional records; "
                "the service returned the stable bounded prefix."
            )
            self.banner.setObjectName("warningBanner")
            self.banner.setVisible(True)
        else:
            self.banner.clear()
            self.banner.setVisible(False)

    def _refresh_record_filters(self, records: Sequence[ReviewRecord]) -> None:
        self.record_type_filter.blockSignals(True)
        self.record_status_filter.blockSignals(True)
        self.record_type_filter.clear()
        self.record_status_filter.clear()
        self.record_type_filter.addItem("All types")
        self.record_status_filter.addItem("All statuses")
        self.record_type_filter.addItems(sorted({record.record_type for record in records}))
        self.record_status_filter.addItems(
            sorted({record.status for record in records if record.status})
        )
        self.record_type_filter.blockSignals(False)
        self.record_status_filter.blockSignals(False)
        self._record_filters_changed()

    def _set_evidence(self, review: DomainReview) -> None:
        rows = [
            {
                "record_type": record.record_type,
                "record_id": record.record_id,
                "provenance_count": len(record.provenance),
                "evidence_count": len(record.evidence),
                "citation_audit": record.verification.citation_audit,
            }
            for record in review.records
            if record.provenance or record.evidence
        ]
        self.evidence_model.set_rows(rows)
        audit = review.citation_audit
        if audit is None:
            self.evidence_summary.setText(
                "Citation audit not run. Select Verify citations to re-read recorded source files."
            )
            self.audit_model.set_rows([])
            return
        counts = audit.get("counts") or {}
        self.evidence_summary.setText(
            "Citation audit completed: "
            + ", ".join(
                f"{key}={counts.get(key, 0)}"
                for key in ("MATCH", "MISMATCH", "UNREADABLE", "NOT_CHECKABLE")
            )
            + f"   · all_verified={audit.get('all_verified')}"
        )
        self.audit_model.set_rows(audit.get("checks") or [])

    def _set_conflicts(self, conflicts: Sequence[ReviewConflict]) -> None:
        self.conflicts_model.set_rows(
            [
                {
                    "conflict_id": conflict.conflict_id,
                    "lookup_key": conflict.lookup_key,
                    "property_name": conflict.property_name,
                    "status": conflict.status,
                    "current": conflict.current,
                    "candidate_count": len(conflict.candidates),
                }
                for conflict in conflicts
            ]
        )

    @staticmethod
    def _calculation_rows(records: Sequence[ReviewRecord]) -> list[dict[str, Any]]:
        rows = []
        for record in records:
            if record.record_type != "calculation":
                continue
            data = record.data
            rows.append(
                {
                    "record_id": record.record_id,
                    "method_id": data.get("method_id"),
                    "method_version": data.get("method_version"),
                    "calculation_type": data.get("calculation_type"),
                    "status": record.status,
                    "current": record.current,
                    "indexed_inputs": len(data.get("indexed_inputs") or []),
                    "outputs": data.get("outputs"),
                    "reproducibility": record.verification.reproducibility,
                }
            )
        return rows

    # -- selection/filter helpers -----------------------------------------
    @Slot(int)
    def _navigation_changed(self, index: int) -> None:
        if 0 <= index < self.pages.count():
            self.pages.setCurrentIndex(index)

    @Slot()
    def _record_filters_changed(self) -> None:
        type_value = self.record_type_filter.currentText()
        status_value = self.record_status_filter.currentText()
        state_value = self.record_state_filter.currentText()
        self.records_proxy.set_record_type("" if type_value.startswith("All") else type_value)
        self.records_proxy.set_status("" if status_value.startswith("All") else status_value)
        self.records_proxy.set_state("" if state_value == "All" else state_value)
        self.records_proxy.set_text(self.record_text_filter.text())

    @Slot(QModelIndex, QModelIndex)
    def _record_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            _fill_tree(self.record_detail, None)
            return
        source_index = self.records_proxy.mapToSource(current)
        record = self.records_model.record_at(source_index.row())
        _fill_tree(self.record_detail, record.to_dict() if record else None)

    @Slot(QModelIndex, QModelIndex)
    def _conflict_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid() or self._review is None:
            _fill_tree(self.conflict_detail, None)
            return
        row = self.conflicts_model.row_payload(current.row())
        conflict = next(
            (
                item
                for item in self._review.conflicts
                if item.conflict_id == str((row or {}).get("conflict_id"))
            ),
            None,
        )
        _fill_tree(self.conflict_detail, conflict.to_dict() if conflict else None)

    def _clear_detail_views(self) -> None:
        _fill_tree(self.record_detail, None)
        _fill_tree(self.conflict_detail, None)

    # -- state and shutdown -----------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.open_button.setEnabled(not busy)
        self.well_combo.setEnabled(not busy and self.well_combo.count() > 0)
        self.lifecycle_combo.setEnabled(not busy and self.well_combo.count() > 0)
        self.load_button.setEnabled(not busy and self.well_combo.count() > 0)
        self.verify_button.setEnabled(not busy and self.well_combo.count() > 0)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #f08a8a;" if error else "")

    def _show_exception(self, category: str, exc: Exception) -> None:
        message = str(exc) or type(exc).__name__
        hint = ""
        if hasattr(exc, "context"):
            hint = str(getattr(exc, "context", {}).get("hint") or "")
        exception_type = type(exc).__name__
        if isinstance(exc, (ValidationError, ConfigurationError, WorkspaceError)):
            visible_category = "input"
        elif isinstance(exc, KnowledgeIntegrityError) or exception_type == "IntegrityError":
            visible_category = "integrity"
        elif isinstance(exc, OSError) or exception_type in {"OperationalError", "DatabaseError"}:
            visible_category = "unavailable"
        elif isinstance(exc, DrillingIntelligenceError):
            visible_category = category
        else:
            visible_category = "unexpected"
        self._set_status(f"{visible_category.title()} error: {message}", error=True)
        self.banner.setText(message + (f"\n\nHint: {hint}" if hint else ""))
        self.banner.setObjectName("warningBanner")
        self.banner.setVisible(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        thread = self._review_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.quit()
            if not thread.wait(10000):
                self._set_status("Waiting for the read-only review worker to finish.", error=True)
                event.ignore()
                return
        self.controller.close()
        event.accept()


__all__ = ["MainWindow"]
