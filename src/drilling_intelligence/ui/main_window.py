"""Read-only desktop Review Workbench window.

The window is deliberately a presentation surface.  It receives ``DomainReview`` values from
``ReviewController`` and does not query the database, inspect source files, or calculate engineering
values.  A small QThread worker keeps review loading and optional citation auditing off the GUI event
loop.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, QSignalBlocker, Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
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
from ..reporting.exhibits import exhibits_from_comparison
from ..reporting.html import write_report_html
from ..review import (
    DomainReview,
    ReviewAction,
    ReviewActionRequest,
    ReviewActionResult,
    ReviewConflict,
    ReviewRecord,
)
from .chart import ExhibitChart
from .controller import NptRollupResult, ReviewController, WellChoice
from .models import (
    MappingTableModel,
    ReviewRecordFilterProxy,
    ReviewRecordsModel,
    TableColumn,
)
from .worker import (
    CalculationWorker,
    ComparisonWorker,
    DecisionWorker,
    ReportAuditWorker,
    ReportWorker,
    ReviewActionWorker,
    ReviewWorker,
    WorkerError,
)

_NAVIGATION = (
    "Overview",
    "Decision",
    "Comparison",
    "Report",
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
        self._decision_thread: QThread | None = None
        self._decision_worker: DecisionWorker | None = None
        self._comparison_thread: QThread | None = None
        self._comparison_worker: ComparisonWorker | None = None
        self._report_thread: QThread | None = None
        self._report_audit_thread: QThread | None = None
        self._report_audit: dict | None = None
        self._report_audit_generation = 0
        self._report_worker: ReportWorker | None = None
        self._comparison_exhibits: dict[str, dict] = {}
        self._report_payload: dict | None = None
        self._report_exhibits: dict[str, dict] = {}
        self._action_thread: QThread | None = None
        self._action_worker: ReviewActionWorker | None = None
        self._calculation_thread: QThread | None = None
        self._calculation_worker: CalculationWorker | None = None
        self._selected_record: ReviewRecord | None = None
        self._record_actions: tuple[ReviewAction, ...] = ()
        self._selected_conflict: ReviewConflict | None = None
        self._conflict_actions: tuple[ReviewAction, ...] = ()
        self._selected_source_path = ""
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
        self._build_decision_page()
        self._build_comparison_page()
        self._build_report_page()
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

    def _build_decision_page(self) -> None:
        """The decision pack, rendered exactly as the service returned it.

        A tree rather than a grid because the pack is a nested plain-value document: converting it
        into columns would mean the widget re-deciding what matters, and this layer's job is to
        display the decision substrate, not to re-interpret it.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        self.decision_header = QLabel("Load a review to build the decision pack for the well.")
        self.decision_header.setWordWrap(True)
        layout.addWidget(self.decision_header)
        self.decision_tree = QTreeWidget()
        self.decision_tree.setHeaderLabels(["Key", "Value"])
        layout.addWidget(self.decision_tree, 1)
        self.pages.addWidget(page)

    def _build_comparison_page(self) -> None:
        """The comparison pack as a matrix, with a chart view of the same cells.

        The matrix stays authoritative. The chart paints a ReportExhibit built from those
        cells; it does not reorder wells or replace a missing value with zero.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.comparison_wells = QListWidget()
        self.comparison_wells.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.comparison_wells.setMaximumHeight(110)
        controls.addWidget(self.comparison_wells, 1)
        side = QVBoxLayout()
        self.comparison_build_button = QPushButton("Build comparison")
        self.comparison_build_button.clicked.connect(self._request_comparison_from_ui)
        side.addWidget(self.comparison_build_button)
        side.addStretch(1)
        controls.addLayout(side)
        layout.addLayout(controls)
        self.comparison_header = QLabel("Tick at least two wells, then build the comparison pack.")
        self.comparison_header.setWordWrap(True)
        layout.addWidget(self.comparison_header)
        self.comparison_authority = QLabel(
            "The matrix is authoritative. Charts are a view of the same cells and do not replace it."
        )
        self.comparison_authority.setWordWrap(True)
        layout.addWidget(self.comparison_authority)
        chart_row = QHBoxLayout()
        self.comparison_chart_selector = QComboBox()
        self.comparison_chart_selector.setObjectName("comparisonChartSelector")
        self.comparison_chart_selector.currentIndexChanged.connect(self._comparison_chart_changed)
        chart_row.addWidget(self.comparison_chart_selector, 1)
        layout.addLayout(chart_row)
        self.comparison_chart = ExhibitChart()
        self.comparison_chart.setObjectName("comparisonExhibitChart")
        layout.addWidget(self.comparison_chart, 1)
        self.comparison_chart_state = QLabel("Chart state: —")
        self.comparison_chart_state.setWordWrap(True)
        self.comparison_chart_state.setObjectName("comparisonChartState")
        layout.addWidget(self.comparison_chart_state)
        self.comparison_table = QTableWidget()
        self.comparison_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.comparison_table.setWordWrap(True)
        layout.addWidget(self.comparison_table, 2)
        self.comparison_observations = QLabel("")
        self.comparison_observations.setWordWrap(True)
        layout.addWidget(self.comparison_observations)
        self.comparison_limitations = QLabel("")
        self.comparison_limitations.setWordWrap(True)
        layout.addWidget(self.comparison_limitations)
        self.pages.addWidget(page)

    def _build_report_page(self) -> None:
        """Report preview and HTML export. The page selects wells; the service composes."""
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        self.report_wells = QListWidget()
        self.report_wells.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.report_wells.setMaximumHeight(110)
        controls.addWidget(self.report_wells, 1)
        side = QVBoxLayout()
        self.report_single_button = QPushButton("Report for open well")
        self.report_single_button.clicked.connect(self._request_single_report)
        side.addWidget(self.report_single_button)
        self.report_compare_button = QPushButton("Report for ticked wells")
        self.report_compare_button.clicked.connect(self._request_ticked_report)
        side.addWidget(self.report_compare_button)
        self.report_offsets_button = QPushButton("Discovered offsets of open well")
        self.report_offsets_button.clicked.connect(self._request_offset_report)
        side.addWidget(self.report_offsets_button)
        self.report_export_button = QPushButton("Export HTML…")
        self.report_export_button.clicked.connect(self._export_report_dialog)
        side.addWidget(self.report_export_button)
        self.report_verify_button = QPushButton("Verify citations")
        self.report_verify_button.clicked.connect(self._request_report_audit)
        side.addWidget(self.report_verify_button)
        controls.addLayout(side)
        layout.addLayout(controls)
        self.report_header = QLabel(
            "Build a report from the open well, ticked wells, or discovered offsets."
        )
        self.report_header.setWordWrap(True)
        self.report_header.setObjectName("reportHeader")
        layout.addWidget(self.report_header)
        self.report_chart_selector = QComboBox()
        self.report_chart_selector.setObjectName("reportChartSelector")
        self.report_chart_selector.currentIndexChanged.connect(self._report_chart_changed)
        layout.addWidget(self.report_chart_selector)
        self.report_chart = ExhibitChart()
        self.report_chart.setObjectName("reportExhibitChart")
        layout.addWidget(self.report_chart, 1)
        self.report_chart_state = QLabel("Chart state: —")
        self.report_chart_state.setWordWrap(True)
        layout.addWidget(self.report_chart_state)
        self.report_sections = QLabel("")
        self.report_sections.setWordWrap(True)
        layout.addWidget(self.report_sections)
        self.report_limitations = QLabel("")
        self.report_limitations.setWordWrap(True)
        layout.addWidget(self.report_limitations)
        self.report_lineage = QLabel(
            "Evidence lineage appears after a report is built. "
            "Citation verification was not requested."
        )
        self.report_lineage.setWordWrap(True)
        self.report_lineage.setObjectName("reportLineage")
        layout.addWidget(self.report_lineage)
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

        action_box = QGroupBox("Confirmed domain action")
        action_layout = QVBoxLayout(action_box)
        action_controls = QHBoxLayout()
        action_controls.addWidget(QLabel("Action"))
        self.record_action_combo = QComboBox()
        self.record_action_combo.setMinimumWidth(190)
        self.record_action_combo.currentIndexChanged.connect(self._record_action_changed)
        action_controls.addWidget(self.record_action_combo)
        action_controls.addWidget(QLabel("Actor"))
        self.record_actor_input = QLineEdit()
        self.record_actor_input.setPlaceholderText("required: user or reviewer id")
        self.record_actor_input.setClearButtonEnabled(True)
        action_controls.addWidget(self.record_actor_input, 1)
        action_controls.addWidget(QLabel("Reason / note"))
        self.record_reason_input = QLineEdit()
        self.record_reason_input.setPlaceholderText("required where the domain rule says so")
        self.record_reason_input.setClearButtonEnabled(True)
        action_controls.addWidget(self.record_reason_input, 2)
        self.record_action_button = QPushButton("Confirm action…")
        self.record_action_button.clicked.connect(self.confirm_record_action)
        action_controls.addWidget(self.record_action_button)
        action_layout.addLayout(action_controls)
        self.record_action_hint = QLabel(
            "Select a current record to see actions derived from its domain lifecycle."
        )
        self.record_action_hint.setWordWrap(True)
        action_layout.addWidget(self.record_action_hint)
        layout.addWidget(action_box)

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
        source_controls = QHBoxLayout()
        self.open_source_button = QPushButton("Open selected source")
        self.open_source_button.setToolTip(
            "Open the existing cited source file; review does not calculate or rewrite it."
        )
        self.open_source_button.clicked.connect(self.open_selected_source)
        source_controls.addWidget(self.open_source_button)
        self.source_navigation_label = QLabel(
            "Select evidence to see its recorded source path and locator."
        )
        self.source_navigation_label.setWordWrap(True)
        source_controls.addWidget(self.source_navigation_label, 1)
        layout.addLayout(source_controls)
        self.evidence_table, self.evidence_model = self._new_table(
            tuple(
                TableColumn(key, title, width)
                for key, title, width in (
                    ("record_type", "Type", 140),
                    ("record_id", "Record ID", 220),
                    ("provenance_count", "Provenance", 100),
                    ("evidence_count", "Evidence", 90),
                    ("source_path", "Source path", 260),
                    ("citation_audit", "Citation", 130),
                )
            )
        )
        layout.addWidget(self.evidence_table, 1)
        self.evidence_table.selectionModel().currentChanged.connect(
            self._evidence_selection_changed
        )
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
        conflict_action_box = QGroupBox("Resolve selected conflict")
        conflict_action_layout = QVBoxLayout(conflict_action_box)
        conflict_controls = QHBoxLayout()
        conflict_controls.addWidget(QLabel("Candidate"))
        self.conflict_candidate_combo = QComboBox()
        self.conflict_candidate_combo.setMinimumWidth(260)
        conflict_controls.addWidget(self.conflict_candidate_combo)
        conflict_controls.addWidget(QLabel("Actor"))
        self.conflict_actor_input = QLineEdit()
        self.conflict_actor_input.setPlaceholderText("required: user or reviewer id")
        self.conflict_actor_input.setClearButtonEnabled(True)
        conflict_controls.addWidget(self.conflict_actor_input, 1)
        conflict_controls.addWidget(QLabel("Reason / note"))
        self.conflict_reason_input = QLineEdit()
        self.conflict_reason_input.setPlaceholderText("why this candidate governs")
        self.conflict_reason_input.setClearButtonEnabled(True)
        conflict_controls.addWidget(self.conflict_reason_input, 2)
        self.conflict_action_button = QPushButton("Confirm resolution…")
        self.conflict_action_button.clicked.connect(self.confirm_conflict_action)
        conflict_controls.addWidget(self.conflict_action_button)
        conflict_action_layout.addLayout(conflict_controls)
        self.conflict_action_hint = QLabel(
            "Select an open conflict to choose one of its recorded candidates."
        )
        self.conflict_action_hint.setWordWrap(True)
        conflict_action_layout.addWidget(self.conflict_action_hint)
        layout.addWidget(conflict_action_box)
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

        execute_box = QGroupBox("Explicit NPT roll-up V1 execution")
        execute_layout = QVBoxLayout(execute_box)
        execute_controls = QHBoxLayout()
        execute_controls.addWidget(QLabel("Actor"))
        self.calculation_actor_input = QLineEdit()
        self.calculation_actor_input.setPlaceholderText("required: user or reviewer id")
        self.calculation_actor_input.setClearButtonEnabled(True)
        execute_controls.addWidget(self.calculation_actor_input, 1)
        execute_controls.addWidget(QLabel("Supersedes"))
        self.calculation_supersedes_input = QLineEdit()
        self.calculation_supersedes_input.setPlaceholderText("optional calculation id")
        self.calculation_supersedes_input.setClearButtonEnabled(True)
        execute_controls.addWidget(self.calculation_supersedes_input, 1)
        self.calculation_confirm = QCheckBox("I explicitly confirmed execution")
        self.calculation_confirm.setToolTip(
            "Review, ingestion, indexing and stale detection never execute calculations."
        )
        execute_controls.addWidget(self.calculation_confirm)
        self.calculation_execute_button = QPushButton("Run NPT roll-up")
        self.calculation_execute_button.clicked.connect(self.run_calculation)
        execute_controls.addWidget(self.calculation_execute_button)
        execute_layout.addLayout(execute_controls)
        self.calculation_result_label = QLabel(
            "No calculation has been executed from this workbench."
        )
        self.calculation_result_label.setWordWrap(True)
        execute_layout.addWidget(self.calculation_result_label)
        layout.addWidget(execute_box)

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
                    ("execution", "Execution", 140),
                    ("dependency", "Dependency", 180),
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
        self.comparison_wells.blockSignals(True)
        self.comparison_wells.clear()
        for choice in choices:
            item = QListWidgetItem(choice.name or choice.label)
            item.setData(Qt.ItemDataRole.UserRole, choice.well_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(choice.well_id)
            self.comparison_wells.addItem(item)
        self.comparison_wells.blockSignals(False)
        self.report_wells.blockSignals(True)
        self.report_wells.clear()
        for choice in choices:
            item = QListWidgetItem(choice.name or choice.label)
            item.setData(Qt.ItemDataRole.UserRole, choice.well_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(choice.well_id)
            self.report_wells.addItem(item)
        self.report_wells.blockSignals(False)
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
        self._request_decision(str(review.request.get("well_id") or ""))
        self._preselect_comparison_well(str(review.request.get("well_id") or ""))

    def _request_decision(self, well_id: str) -> None:
        """Fetch the pack for the same well, off the GUI thread, after every review load.

        A failure here is a status line and nothing else: the review already on screen stays, and
        a decision-pack problem can never corrupt the service or the record views - both are
        read-only surfaces.
        """
        if self._decision_thread is not None or not well_id:
            return
        thread = QThread(self)
        worker = DecisionWorker(self.controller, well_id, parent=None)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._decision_succeeded)
        worker.failed.connect(self._decision_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._decision_thread_finished)
        self._decision_thread = thread
        self._decision_worker = worker
        thread.start()

    @Slot(object)
    def _decision_succeeded(self, payload: dict) -> None:
        self.set_decision(payload)

    @Slot(object)
    def _decision_failed(self, error: WorkerError) -> None:
        self._set_status(f"Decision pack {error.category}: {error.message}", error=True)

    @Slot()
    def _decision_thread_finished(self) -> None:
        thread = self._decision_thread
        self._decision_thread = None
        self._decision_worker = None
        if thread is not None:
            thread.deleteLater()

    def set_decision(self, payload: dict) -> None:
        """Render a complete DecisionPack payload: label, tree, nothing reinterpreted."""
        subject = payload.get("subject") or {}
        schema = str(payload.get("schema") or "")
        label = f"{subject.get('name') or subject.get('id') or '—'} · {subject.get('kind') or ''}"
        header = f"Decision pack {schema} · scope: {label}"
        limitations = payload.get("limitations") or []
        if limitations:
            header += f" · {len(limitations)} limitation(s)"
        self.decision_header.setText(header)
        _fill_tree(self.decision_tree, payload)
        self.decision_tree.expandToDepth(1)
        self._set_status(
            f"Decision pack loaded for {subject.get('name') or 'well'} "
            f"({len(payload.get('observations') or [])} observation(s))."
        )

    def _preselect_comparison_well(self, well_id: str) -> None:
        """Tick the review's well in the comparison list so a second tick starts a pair."""
        if not well_id:
            return
        for index in range(self.comparison_wells.count()):
            item = self.comparison_wells.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == well_id:
                item.setCheckState(Qt.CheckState.Checked)
                return

    @Slot()
    def _request_comparison_from_ui(self) -> None:
        well_ids: list[str] = []
        for index in range(self.comparison_wells.count()):
            item = self.comparison_wells.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                value = str(item.data(Qt.ItemDataRole.UserRole) or "")
                if value:
                    well_ids.append(value)
        self._request_comparison(well_ids)

    def _request_comparison(self, well_ids: Sequence[str]) -> None:
        """Fetch the comparison pack off the GUI thread; failures change only the status.

        Every rule (two wells minimum, known ids, unit comparability) lives behind the
        service boundary - the window selects and displays, nothing else.
        """
        if self._comparison_thread is not None:
            return
        if len(well_ids) < 2:
            self._set_status("Comparison needs at least two wells ticked.", error=True)
            return
        thread = QThread(self)
        worker = ComparisonWorker(self.controller, well_ids, parent=None)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._comparison_succeeded)
        worker.failed.connect(self._comparison_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._comparison_thread_finished)
        self._comparison_thread = thread
        self._comparison_worker = worker
        thread.start()

    @Slot(object)
    def _comparison_succeeded(self, payload: dict) -> None:
        self.set_comparison(payload)

    @Slot(object)
    def _comparison_failed(self, error: WorkerError) -> None:
        self._set_status(f"Comparison pack {error.category}: {error.message}", error=True)

    @Slot()
    def _comparison_thread_finished(self) -> None:
        thread = self._comparison_thread
        self._comparison_thread = None
        self._comparison_worker = None
        if thread is not None:
            thread.deleteLater()

    def set_comparison(self, payload: dict) -> None:
        """Render a complete ComparisonPack payload as the matrix it is.

        One row per metric, one column per subject, the verdict last.  An absent value is
        the em dash every other page uses - visibly different from a recorded zero - and
        each cell carries its value state and comparability as a tooltip.  Nothing is
        recomputed: the table shows the pack's own cells.
        """
        basis = payload.get("basis") or {}
        subjects = basis.get("subjects") or []
        names = [str(s.get("name") or s.get("well_id") or "") for s in subjects]
        header = f"Comparison pack {payload.get('schema')} · basis: {basis.get('kind')} · {', '.join(names) or 'no subjects'}"
        window = basis.get("window") or {}
        if window.get("applied"):
            header += f" · window {window.get('since') or '...'} to {window.get('until') or '...'}"
        limitations = payload.get("limitations") or []
        if limitations:
            header += f" · {len(limitations)} limitation(s)"
        self.comparison_header.setText(header)

        rows: list[Mapping[str, Any]] = []
        for section in payload.get("sections") or []:
            for row in section.get("metrics") or []:
                rows.append(row)
        columns = ["Metric", *names, "State"]
        self.comparison_table.clear()
        self.comparison_table.setColumnCount(len(columns))
        self.comparison_table.setRowCount(len(rows))
        self.comparison_table.setHorizontalHeaderLabels(columns)
        for row_index, row in enumerate(rows):
            metric_item = QTableWidgetItem(str(row.get("metric") or ""))
            metric_item.setToolTip(str(row.get("label") or ""))
            self.comparison_table.setItem(row_index, 0, metric_item)
            values = row.get("values") or {}
            for column_index, subject in enumerate(subjects, start=1):
                cell = values.get(str(subject.get("well_id"))) or {}
                value = cell.get("value")
                text = _value_text(value)
                if value is not None and cell.get("unit"):
                    text = f"{text} {cell['unit']}"
                cell_item = QTableWidgetItem(text)
                cell_item.setToolTip(
                    f"value_state: {cell.get('value_state', '—')}\n"
                    f"comparability: {cell.get('comparability', '—')}"
                )
                self.comparison_table.setItem(row_index, column_index, cell_item)
            state_item = QTableWidgetItem(str(row.get("comparability") or ""))
            self.comparison_table.setItem(row_index, len(subjects) + 1, state_item)
        self.comparison_table.resizeColumnsToContents()
        self.comparison_table.horizontalHeader().setStretchLastSection(True)

        observations = payload.get("observations") or []
        self.comparison_observations.setText("\n".join(f"• {line}" for line in observations))
        self.comparison_limitations.setText(
            "Limitations: " + ", ".join(limitations) if limitations else "Limitations: none"
        )
        self._set_status(
            f"Comparison pack loaded for {len(subjects)} well(s) ({len(rows)} metric row(s))."
        )
        self._load_comparison_exhibits(payload)

    def _load_comparison_exhibits(self, payload: dict) -> None:
        """Chart specs from the pack already on screen. No database, no second fold."""
        exhibits = [item.payload() for item in exhibits_from_comparison(payload)]
        self._comparison_exhibits = {item["exhibit_id"]: item for item in exhibits}
        self.comparison_chart_selector.blockSignals(True)
        self.comparison_chart_selector.clear()
        for item in exhibits:
            self.comparison_chart_selector.addItem(
                f"{item.get('title')} [{item.get('state')}]",
                item.get("exhibit_id"),
            )
        preferred = next(
            (index for index, item in enumerate(exhibits) if item.get("state") == "RENDERED"), 0
        )
        if exhibits:
            self.comparison_chart_selector.setCurrentIndex(preferred)
            self._show_comparison_exhibit(str(exhibits[preferred].get("exhibit_id") or ""))
        self.comparison_chart_selector.blockSignals(False)

    @Slot(int)
    def _comparison_chart_changed(self, index: int) -> None:
        if index < 0:
            return
        exhibit_id = str(self.comparison_chart_selector.itemData(index) or "")
        self._show_comparison_exhibit(exhibit_id)

    def _show_comparison_exhibit(self, exhibit_id: str) -> None:
        exhibit = self._comparison_exhibits.get(exhibit_id)
        if exhibit is None:
            self.comparison_chart.set_exhibit(None)
            self.comparison_chart_state.setText("Chart state: —")
            return
        self.comparison_chart.set_exhibit(exhibit)
        reason = exhibit.get("reason") or exhibit.get("caption") or ""
        self.comparison_chart_state.setText(f"Chart state: {exhibit.get('state')}. {reason}")

    def _checked_report_wells(self) -> list[str]:
        well_ids: list[str] = []
        for index in range(self.report_wells.count()):
            item = self.report_wells.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                value = str(item.data(Qt.ItemDataRole.UserRole) or "")
                if value:
                    well_ids.append(value)
        return well_ids

    def _open_well_id(self) -> str:
        return str(self.well_combo.currentData() or "")

    @Slot()
    def _request_single_report(self) -> None:
        well_id = self._open_well_id()
        if not well_id:
            self._set_status("Open a well before building its report.", error=True)
            return
        self._request_report(well_ids=[well_id])

    @Slot()
    def _request_ticked_report(self) -> None:
        well_ids = self._checked_report_wells()
        if not well_ids:
            self._set_status("Tick at least one well, or use the open-well report.", error=True)
            return
        self._request_report(well_ids=well_ids)

    @Slot()
    def _request_offset_report(self) -> None:
        well_id = self._open_well_id()
        if not well_id:
            self._set_status("Open a well before discovering offsets.", error=True)
            return
        self._request_report(anchor=well_id)

    def _request_report(
        self,
        *,
        well_ids: Sequence[str] = (),
        anchor: str = "",
        offsets: Sequence[str] = (),
    ) -> None:
        if self._report_thread is not None:
            return
        thread = QThread(self)
        worker = ReportWorker(
            self.controller,
            well_ids=well_ids,
            anchor=anchor,
            offsets=offsets,
            parent=None,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._report_succeeded)
        worker.failed.connect(self._report_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._report_thread_finished)
        self._report_thread = thread
        self._report_worker = worker
        thread.start()

    @Slot(object)
    def _report_succeeded(self, payload: dict) -> None:
        self.set_report(payload)

    @Slot(object)
    def _report_failed(self, error: WorkerError) -> None:
        self._set_status(f"Report {error.category}: {error.message}", error=True)

    @Slot()
    def _report_thread_finished(self) -> None:
        thread = self._report_thread
        self._report_thread = None
        self._report_worker = None
        if thread is not None:
            thread.deleteLater()

    def _show_report_lineage(self, payload: dict, audit: dict | None) -> None:
        from ..reporting.lineage import traceability_manifest

        manifest = traceability_manifest(payload).to_dict()
        counts: dict[str, int] = {}
        for source in manifest.get("sources") or []:
            state = str(source.get("relationship") or "")
            counts[state] = counts.get(state, 0) + 1
        summary = ", ".join(f"{key} {counts[key]}" for key in sorted(counts)) or "none"
        if audit is None:
            notice = "Citation verification was not requested."
        else:
            notice = (
                f"Citation verification requested. Overall {audit.get('overall')}. "
                f"eligible {audit.get('eligible')}, attempted {audit.get('attempted')}, "
                f"omitted {audit.get('omitted')}."
            )
        self.report_lineage.setText(
            f"Evidence lineage: {summary}. Unlinked {len(manifest.get('unlinked') or [])}. {notice}"
        )

    @Slot()
    def _request_report_audit(self) -> None:
        if not self._report_payload:
            self._set_status("Build a report before verifying citations.", error=True)
            return
        if self._report_audit_thread is not None:
            return
        generation = self._report_audit_generation
        thread = QThread(self)
        worker = ReportAuditWorker(self.controller, dict(self._report_payload), parent=None)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(
            lambda payload, generation=generation: self._report_audit_succeeded(payload, generation)
        )
        worker.failed.connect(
            lambda error, generation=generation: self._report_audit_failed(error, generation)
        )
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._report_audit_thread_finished)
        self._report_audit_thread = thread
        thread.start()
        self._set_status("Verifying citations…")

    @Slot(object)
    def _report_audit_succeeded(self, payload: dict, generation: int) -> None:
        if generation != self._report_audit_generation or not self._report_payload:
            return
        self._report_audit = payload
        self._show_report_lineage(self._report_payload, payload)
        self._set_status(f"Citation audit {payload.get('overall')}.")

    @Slot(object)
    def _report_audit_failed(self, error: WorkerError, generation: int) -> None:
        if generation != self._report_audit_generation:
            return
        self._set_status(f"Citation audit {error.category}: {error.message}", error=True)

    @Slot()
    def _report_audit_thread_finished(self) -> None:
        thread = self._report_audit_thread
        self._report_audit_thread = None
        if thread is not None:
            thread.deleteLater()

    def set_report(self, payload: dict) -> None:
        """Render a ReportPack. Export writes this payload, not a second composition."""
        self._report_payload = payload
        subject = payload.get("subject") or {}
        names = [
            str(item.get("name") or item.get("well_id") or "")
            for item in subject.get("subjects") or []
        ]
        who = ", ".join(names) or str(subject.get("name") or subject.get("id") or "")
        self.report_header.setText(
            f"Report {payload.get('schema')} · {payload.get('mode')} · {who} · "
            f"identity {payload.get('identity')}"
        )
        section_lines = [
            f"{section.get('section_id')}: {section.get('state')}"
            for section in payload.get("sections") or []
        ]
        self.report_sections.setText("Sections: " + ", ".join(section_lines))
        limitations = payload.get("limitations") or []
        self.report_limitations.setText(
            "Limitations: " + ", ".join(str(item) for item in limitations)
            if limitations
            else "Limitations: none"
        )
        exhibits = list(payload.get("exhibits") or [])
        self._report_exhibits = {str(item.get("exhibit_id")): item for item in exhibits}
        self.report_chart_selector.blockSignals(True)
        self.report_chart_selector.clear()
        for item in exhibits:
            self.report_chart_selector.addItem(
                f"{item.get('title')} [{item.get('state')}]",
                item.get("exhibit_id"),
            )
        preferred = next(
            (index for index, item in enumerate(exhibits) if item.get("state") == "RENDERED"), 0
        )
        if exhibits:
            self.report_chart_selector.setCurrentIndex(preferred)
            self._show_report_exhibit(str(exhibits[preferred].get("exhibit_id") or ""))
        self.report_chart_selector.blockSignals(False)
        self._report_audit = None
        self._report_audit_generation = getattr(self, "_report_audit_generation", 0) + 1
        self._show_report_lineage(payload, None)
        self._set_status(f"Report loaded ({payload.get('mode')}).")

    @Slot(int)
    def _report_chart_changed(self, index: int) -> None:
        if index < 0:
            return
        self._show_report_exhibit(str(self.report_chart_selector.itemData(index) or ""))

    def _show_report_exhibit(self, exhibit_id: str) -> None:
        exhibit = self._report_exhibits.get(exhibit_id)
        if exhibit is None:
            self.report_chart.set_exhibit(None)
            self.report_chart_state.setText("Chart state: —")
            return
        self.report_chart.set_exhibit(exhibit)
        self.report_chart_state.setText(
            f"Chart state: {exhibit.get('state')}. {exhibit.get('reason') or ''}"
        )

    @Slot()
    def _export_report_dialog(self) -> None:
        if not self._report_payload:
            self._set_status("Build a report before exporting.", error=True)
            return
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Export HTML report",
            "report.html",
            "HTML (*.html)",
        )
        if not path:
            return
        try:
            self.export_loaded_report(path)
        except Exception as exc:  # noqa: BLE001 - export must not crash the window
            self._set_status(f"Export failed: {exc}", error=True)

    def export_loaded_report(self, path: str) -> str:
        """Write the loaded report with the canonical HTML renderer. No second query."""
        if not self._report_payload:
            raise ValidationError("no report is loaded", hint="build a report before exporting")
        write_report_html(path, self._report_payload, audit=getattr(self, "_report_audit", None))
        return str(self._report_payload.get("identity") or "")

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
                "source_path": str(
                    ((record.data.get("source_navigation") or [{}])[0]).get("source_path") or ""
                ),
                "citation_audit": record.verification.citation_audit,
            }
            for record in review.records
            if record.provenance or record.evidence
        ]
        self.evidence_model.set_rows(rows)
        self._set_source_navigation(None)
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

    def _evidence_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        payload = self.evidence_model.row_payload(current.row()) if current.isValid() else None
        record = None
        if payload is not None and self._review is not None:
            record = next(
                (
                    item
                    for item in self._review.records
                    if item.record_type == str(payload.get("record_type") or "")
                    and item.record_id == str(payload.get("record_id") or "")
                ),
                None,
            )
        self._set_source_navigation(record)

    def _set_source_navigation(self, record: ReviewRecord | None) -> None:
        self._selected_source_path = ""
        navigation = record.data.get("source_navigation") if record is not None else ()
        entries = [entry for entry in (navigation or ()) if isinstance(entry, Mapping)]
        entry = next((item for item in entries if item.get("source_path")), None)
        if entry is None:
            self.source_navigation_label.setText(
                "No recorded source path is available for this evidence row."
            )
            self.open_source_button.setEnabled(False)
            return
        raw_path = str(entry.get("source_path") or "").strip()
        path = Path(raw_path).expanduser()
        if not path.is_absolute() and self.controller.workspace is not None:
            path = Path(self.controller.workspace.root) / path
        self._selected_source_path = str(path)
        locator = _value_text(entry.get("locator")) if entry.get("locator") else "—"
        self.source_navigation_label.setText(
            f"{path}   ·   locator: {locator}   ·   version: {entry.get('document_version_id') or '—'}"
        )
        self.open_source_button.setEnabled(True)

    @Slot()
    def open_selected_source(self) -> None:
        if not self._selected_source_path:
            self._set_status("No recorded source is selected.", error=True)
            return
        path = Path(self._selected_source_path)
        if not path.is_file():
            self._set_status(
                f"Recorded source is unavailable: {path}",
                error=True,
            )
            self.banner.setText(
                "The calculation/review kept the source citation, but the cited file is not present at "
                f"{path}. No replacement or recalculation was attempted."
            )
            self.banner.setObjectName("warningBanner")
            self.banner.setVisible(True)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self._set_status(
                f"The operating system could not open recorded source: {path}", error=True
            )

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
                    "execution": data.get("calculation_classification") or "NOT_ASSESSED",
                    "dependency": (data.get("dependency_impact") or {}).get("counts") or {},
                    "reproducibility": record.verification.reproducibility,
                }
            )
        return rows

    @Slot()
    def run_calculation(self) -> None:
        """Start the sole executable calculation only after an explicit UI confirmation."""
        if (
            self._calculation_thread is not None
            or self._review_thread is not None
            or self._action_thread is not None
        ):
            return
        well_id = str(self.well_combo.currentData() or "")
        actor = self.calculation_actor_input.text().strip()
        if not well_id:
            self._set_status("Select a well before executing a calculation.", error=True)
            return
        if not actor:
            self._set_status("An explicit calculation actor is required.", error=True)
            self.calculation_actor_input.setFocus()
            return
        if not self.calculation_confirm.isChecked():
            self._set_status(
                "Check the explicit execution confirmation; review itself never calculates.",
                error=True,
            )
            return
        thread = QThread(self)
        worker = CalculationWorker(
            self.controller,
            well_id,
            actor,
            self.calculation_supersedes_input.text().strip(),
            parent=None,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._calculation_succeeded)
        worker.failed.connect(self._calculation_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._calculation_thread_finished)
        self._calculation_thread = thread
        self._calculation_worker = worker
        self._set_busy(True)
        self._set_status(f"Executing NPT roll-up for {self.well_combo.currentText()}…")
        thread.start()

    @Slot(object)
    def _calculation_succeeded(self, result: NptRollupResult) -> None:
        self.calculation_result_label.setText(
            f"Stored calculation {result.calculation_id} "
            f"({'new' if result.created else 'unchanged'}): "
            f"{result.value if result.value is not None else '—'} {result.unit or '—'}; "
            f"method {result.method_id} v{result.method_version}, revision {result.revision}."
        )
        self.banner.clear()
        self.banner.setVisible(False)
        self._set_status("Calculation completed; reloading authoritative review…")
        self.request_review(verify_citations=False)

    @Slot(object)
    def _calculation_failed(self, error: WorkerError) -> None:
        self._set_status(f"{error.category.title()} error: {error.message}", error=True)
        self.calculation_result_label.setText(
            error.message + (f" Hint: {error.hint}" if error.hint else "")
        )
        self.banner.setText(
            "Calculation was not executed or stored. No review, indexing, or stale-state path retries it."
        )
        self.banner.setObjectName("warningBanner")
        self.banner.setVisible(True)

    @Slot()
    def _calculation_thread_finished(self) -> None:
        thread = self._calculation_thread
        self._calculation_thread = None
        self._calculation_worker = None
        self._set_busy(False)
        if thread is not None:
            thread.deleteLater()

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
            self._selected_record = None
            _fill_tree(self.record_detail, None)
            self._set_record_actions(())
            return
        source_index = self.records_proxy.mapToSource(current)
        record = self.records_model.record_at(source_index.row())
        self._selected_record = record
        _fill_tree(self.record_detail, record.to_dict() if record else None)
        self._set_source_navigation(record)
        if record is None or self.controller.workspace is None:
            self._set_record_actions(())
            return
        try:
            self._set_record_actions(self.controller.available_actions(record))
        except Exception as exc:  # noqa: BLE001 - keep selection/read rendering usable
            self._set_record_actions(())
            self._set_status(f"Could not determine actions: {exc}", error=True)

    def _set_record_actions(self, actions: Sequence[ReviewAction]) -> None:
        self._record_actions = tuple(actions)
        self.record_action_combo.blockSignals(True)
        self.record_action_combo.clear()
        for action in self._record_actions:
            self.record_action_combo.addItem(action.label, action.action_id)
        self.record_action_combo.blockSignals(False)
        self._record_action_changed(self.record_action_combo.currentIndex())
        enabled = bool(self._record_actions) and self._review is not None
        self.record_action_combo.setEnabled(enabled)
        self.record_actor_input.setEnabled(enabled)
        self.record_reason_input.setEnabled(enabled)
        self.record_action_button.setEnabled(enabled)

    @Slot(int)
    def _record_action_changed(self, _index: int) -> None:
        action = self._current_record_action()
        if action is None:
            self.record_action_hint.setText(
                "No governed action is available for this record, or the selected row is historical."
            )
            return
        requirements = ["actor"]
        if action.requires_reason:
            requirements.append("reason")
        if action.requires_evidence:
            requirements.append("recorded evidence")
        self.record_action_hint.setText(
            f"{action.label} moves the authoritative status to {action.target_status}. "
            f"Explicit {', '.join(requirements)} required; the record will be re-read after success."
        )

    def _current_record_action(self) -> ReviewAction | None:
        action_id = str(self.record_action_combo.currentData() or "")
        return next(
            (action for action in self._record_actions if action.action_id == action_id),
            None,
        )

    @Slot()
    def confirm_record_action(self) -> None:
        """Collect explicit human inputs, confirm semantically, then hand off to a worker."""
        if self._action_thread is not None or self._review_thread is not None:
            return
        record = self._selected_record
        action = self._current_record_action()
        if record is None or action is None or self._review is None:
            self._set_status("Select a current actionable record first.", error=True)
            return
        actor = self.record_actor_input.text().strip()
        reason = self.record_reason_input.text()
        if not actor:
            self._set_status(
                "An explicit actor is required before confirming an action.", error=True
            )
            self.record_actor_input.setFocus()
            return
        if action.requires_reason and not reason.strip():
            self._set_status(f"{action.label} needs a reason.", error=True)
            self.record_reason_input.setFocus()
            return
        data = record.data
        expected_revision = data.get("revision")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError):
                expected_revision = None
        request = ReviewActionRequest(
            record_type=record.record_type,
            record_id=record.record_id,
            action=action.action_id,
            actor=actor,
            reason=reason,
            note=reason,
            well_id=str(self._review.request.get("well_id") or ""),
            expected_status=record.status,
            expected_revision=expected_revision,
            expected_current=record.current,
            expected_scope=dict(record.scope),
            expected_updated_at=str(data.get("updated_at") or ""),
        )
        answer = QMessageBox.question(
            self,
            f"Confirm {action.label.lower()}",
            (
                f"{action.confirmation}?\n\n"
                f"Record: {record.record_type} / {record.record_id}\n"
                f"Current status: {record.status}\n"
                f"New status: {action.target_status}\n"
                f"Actor: {actor}\n"
                f"Reason / note: {reason.strip() or '—'}\n\n"
                "Only this confirmed action can write the authoritative record."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_action_worker(request)

    def _start_action_worker(self, request: ReviewActionRequest) -> None:
        thread = QThread(self)
        worker = ReviewActionWorker(self.controller, request, parent=None)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._action_succeeded)
        worker.failed.connect(self._action_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.succeeded.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._action_thread_finished)
        self._action_thread = thread
        self._action_worker = worker
        self._set_busy(True)
        self._set_status(f"Executing {request.action.replace('_', ' ')} for {request.record_id}…")
        thread.start()

    @Slot(object)
    def _action_succeeded(self, result: ReviewActionResult) -> None:
        # Do not patch the displayed ReviewRecord with a client-side result.  A fresh read is the
        # only success rendering, so approval metadata, history, revision and evidence remain the
        # values the domain committed.
        self.banner.clear()
        self.banner.setVisible(False)
        self._set_status(
            f"{result.action.replace('_', ' ').capitalize()} committed by {result.actor}; reloading authoritative review…"
        )
        self.request_review(verify_citations=False)

    @Slot(object)
    def _action_failed(self, error: WorkerError) -> None:
        # The existing DomainReview is deliberately left in place on failure, including its
        # selection and detail tree.  The error explains stale/ineligible actions without inventing
        # a local status.
        self._set_status(f"{error.category.title()} error: {error.message}", error=True)
        self.banner.setText(error.message + (f"\n\nHint: {error.hint}" if error.hint else ""))
        self.banner.setObjectName("warningBanner")
        self.banner.setVisible(True)

    @Slot()
    def _action_thread_finished(self) -> None:
        thread = self._action_thread
        self._action_thread = None
        self._action_worker = None
        self._set_busy(False)
        if thread is not None:
            thread.deleteLater()

    @Slot(QModelIndex, QModelIndex)
    def _conflict_selection_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid() or self._review is None:
            self._selected_conflict = None
            _fill_tree(self.conflict_detail, None)
            self._set_conflict_actions(())
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
        self._selected_conflict = conflict
        _fill_tree(self.conflict_detail, conflict.to_dict() if conflict else None)
        if conflict is None or self.controller.workspace is None:
            self._set_conflict_actions(())
            return
        try:
            self._set_conflict_actions(self.controller.available_actions(conflict))
        except Exception as exc:  # noqa: BLE001 - preserve read-side selection on capability errors
            self._set_conflict_actions(())
            self._set_status(f"Could not determine conflict actions: {exc}", error=True)

    def _set_conflict_actions(self, actions: Sequence[ReviewAction]) -> None:
        self._conflict_actions = tuple(actions)
        self.conflict_candidate_combo.blockSignals(True)
        self.conflict_candidate_combo.clear()
        conflict = self._selected_conflict
        if conflict is not None:
            for candidate in conflict.candidates:
                candidate_id = str(candidate.get("item_id") or "")
                label = (
                    str(candidate.get("text") or candidate.get("original_value") or candidate_id)
                    + f"  [{candidate_id}]"
                )
                self.conflict_candidate_combo.addItem(label, candidate_id)
        self.conflict_candidate_combo.blockSignals(False)
        enabled = bool(self._conflict_actions) and conflict is not None and self._review is not None
        self.conflict_candidate_combo.setEnabled(enabled)
        self.conflict_actor_input.setEnabled(enabled)
        self.conflict_reason_input.setEnabled(enabled)
        self.conflict_action_button.setEnabled(enabled)
        self.conflict_action_hint.setText(
            "Resolve this conflict by explicitly selecting one recorded candidate; the other facts remain as history."
            if enabled
            else "Select an open conflict to choose one of its recorded candidates."
        )

    @Slot()
    def confirm_conflict_action(self) -> None:
        if self._action_thread is not None or self._review_thread is not None:
            return
        conflict = self._selected_conflict
        action = self._conflict_actions[0] if self._conflict_actions else None
        chosen_item_id = str(self.conflict_candidate_combo.currentData() or "")
        actor = self.conflict_actor_input.text().strip()
        reason = self.conflict_reason_input.text()
        if conflict is None or action is None:
            self._set_status("Select an open conflict first.", error=True)
            return
        if not chosen_item_id:
            self._set_status("Choose one of the recorded conflict candidates.", error=True)
            return
        if not actor:
            self._set_status(
                "An explicit actor is required before resolving a conflict.", error=True
            )
            self.conflict_actor_input.setFocus()
            return
        answer = QMessageBox.question(
            self,
            "Confirm conflict resolution",
            (
                f"{action.confirmation}?\n\n"
                f"Conflict: {conflict.conflict_id}\n"
                f"Candidate: {chosen_item_id}\n"
                f"Actor: {actor}\n"
                f"Reason / note: {reason.strip() or '—'}\n\n"
                "The losing facts will remain stored as retired history."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        request = ReviewActionRequest(
            record_type="knowledge_conflict",
            record_id=conflict.conflict_id,
            action=action.action_id,
            actor=actor,
            reason=reason,
            note=reason,
            well_id=str(self._review.request.get("well_id") if self._review else ""),
            expected_status=conflict.status,
            expected_current=conflict.current,
            expected_updated_at=str(conflict.data.get("updated_at") or ""),
            chosen_item_id=chosen_item_id,
        )
        self._start_action_worker(request)

    def _clear_detail_views(self) -> None:
        self._selected_record = None
        self._selected_conflict = None
        self._set_record_actions(())
        self._set_conflict_actions(())
        _fill_tree(self.record_detail, None)
        _fill_tree(self.conflict_detail, None)
        self._set_source_navigation(None)

    # -- state and shutdown -----------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self.open_button.setEnabled(not busy)
        self.well_combo.setEnabled(not busy and self.well_combo.count() > 0)
        self.lifecycle_combo.setEnabled(not busy and self.well_combo.count() > 0)
        self.load_button.setEnabled(not busy and self.well_combo.count() > 0)
        self.verify_button.setEnabled(not busy and self.well_combo.count() > 0)
        self.calculation_actor_input.setEnabled(not busy and self.well_combo.count() > 0)
        self.calculation_supersedes_input.setEnabled(not busy and self.well_combo.count() > 0)
        self.calculation_confirm.setEnabled(not busy and self.well_combo.count() > 0)
        self.calculation_execute_button.setEnabled(not busy and self.well_combo.count() > 0)
        action_available = bool(self._record_actions) and self._review is not None
        self.record_action_combo.setEnabled(not busy and action_available)
        self.record_actor_input.setEnabled(not busy and action_available)
        self.record_reason_input.setEnabled(not busy and action_available)
        self.record_action_button.setEnabled(not busy and action_available)
        conflict_available = bool(self._conflict_actions) and self._selected_conflict is not None
        self.conflict_candidate_combo.setEnabled(not busy and conflict_available)
        self.conflict_actor_input.setEnabled(not busy and conflict_available)
        self.conflict_reason_input.setEnabled(not busy and conflict_available)
        self.conflict_action_button.setEnabled(not busy and conflict_available)

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
        threads = [
            thread
            for thread in (
                self._review_thread,
                self._action_thread,
                self._calculation_thread,
                self._report_thread,
                self._report_audit_thread,
            )
            if thread is not None
        ]
        for thread in threads:
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
                if not thread.wait(10000):
                    self._set_status("Waiting for a review worker to finish.", error=True)
                    event.ignore()
                    return
        self.controller.close()
        event.accept()


__all__ = ["MainWindow"]
