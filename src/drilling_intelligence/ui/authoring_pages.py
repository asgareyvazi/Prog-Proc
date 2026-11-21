"""Write-capable Qt authoring pages for Programs and Procedures.

These pages are real editors backed by the workspace database through
``EngineeringRepository`` / ``EngineeringAuthoringService`` - not read-only mocks. All
lifecycle rules (DRAFT-only mutation, freshness tokens, distinct reviewer, no inherited
approval, exact-revision linkage) are enforced by the service; the widgets only call it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import DrillingIntelligenceError
from ..engineering.authoring import EngineeringAuthoringService
from ..engineering.export import export_record
from ..engineering.repository import EngineeringRepository
from ..engineering.templates import PROCEDURE_TEMPLATES, PROGRAM_TEMPLATES, create_from_template

_COMPLETION_STATES = (
    "MISSING",
    "DRAFT",
    "SOURCE_PROVIDED",
    "EXTRACTED_UNVERIFIED",
    "NOT_APPLICABLE",
    "OWNER_DECISION_REQUIRED",
)


def _workspace_session(workspace: Any):
    """Open a writable unit of work on the workspace database."""
    return workspace.database.unit_of_work()


class _AuthoringPageBase(QWidget):
    kind = "program"

    def __init__(self, workspace_getter: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_getter = workspace_getter
        self._program_id: str | None = None
        self._record: Any = None
        self._build_ui()

    # -- helpers -----------------------------------------------------------
    def _workspace(self) -> Any:
        workspace = self._workspace_getter()
        if workspace is None:
            raise DrillingIntelligenceError("no workspace is open")
        return workspace

    def _services(self, session: Any) -> tuple[EngineeringRepository, EngineeringAuthoringService]:
        return EngineeringRepository(session), EngineeringAuthoringService(session)

    def _token(self, session: Any, row: Any) -> str:
        return EngineeringAuthoringService._token(row)

    def _notify(self, message: str) -> None:
        self.status_label.setText(message)

    def _error(self, exc: Exception) -> None:
        # Non-modal by design: a blocking dialog would hang headless/offscreen runs.
        self.status_label.setText(f"Error: {exc}")

    def _refresh_records(self) -> None:
        try:
            workspace = self._workspace()
        except DrillingIntelligenceError:
            self.record_list.clear()
            return
        with _workspace_session(workspace) as session:
            repo, _service = self._services(session)
            rows = repo.list_programs() if self.kind == "program" else repo.list_procedures()
        self.record_list.clear()
        for row in rows:
            item = QListWidgetItem(
                f"{row.code or '-'} | {row.title} | rev {row.revision} | {row.status}"
                + (" | current" if row.is_current else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, row.id)
            self.record_list.addItem(item)

    def _open_selected(self) -> None:
        item = self.record_list.currentItem()
        if item is None:
            self._notify("Select a record first.")
            return
        self._program_id = item.data(Qt.ItemDataRole.UserRole)
        with _workspace_session(self._workspace()) as session:
            repo, _service = self._services(session)
            self._record = (
                repo.get_program(self._program_id)
                if self.kind == "program"
                else repo.get_procedure(self._program_id)
            )
        self._load_record_into_editor()

    # -- lifecycle actions (shared by Program and Procedure pages) ----------
    def _actor(self) -> str:
        return self.author_input.text().strip() or "ui-actor"

    def _lifecycle_call(self, fn, *args, **kwargs) -> bool:
        """Run one lifecycle transition; return True only if it actually succeeded."""
        if self._program_id is None:
            self._notify("Open a record first.")
            return False
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                row = (
                    repo.get_program(self._program_id)
                    if self.kind == "program"
                    else repo.get_procedure(self._program_id)
                )
                fn(service, repo, row, session, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return False
        self._refresh_records()
        self._load_record_into_editor()
        return True

    def _do_review(self) -> None:
        note = self.note_input.text().strip() or "reviewed via UI"

        def call(service, repo, row, session):
            service.record_review(
                self.kind,
                row.id,
                expected_updated_at=self._token(session, row),
                by=self._actor(),
                note=note,
            )

        if self._lifecycle_call(call):
            self._notify(
                "Review recorded (declared attribution; structural review is not approval)."
            )

    def _do_decide(self, approved: bool) -> None:
        reason = self.note_input.text().strip() or ("approve" if approved else "reject")

        def call(service, repo, row, session):
            service.decide(
                self.kind,
                row.id,
                expected_updated_at=self._token(session, row),
                by=self._actor(),
                approved=approved,
                reason=reason,
            )

        if self._lifecycle_call(call):
            self._notify(
                f"Decision recorded: {'APPROVED' if approved else 'REJECTED'} (exact revision)."
            )

    def _do_revise(self) -> None:
        def call(service, repo, row, session):
            new_row = (
                repo.revise_program(row.id, by=self._actor())
                if self.kind == "program"
                else repo.revise_procedure(row.id, by=self._actor())
            )
            self._program_id = new_row.id  # open the new DRAFT revision in the editor

        if self._lifecycle_call(call):
            self._notify("New DRAFT revision created; approval is not inherited.")

    def _do_compare_previous(self) -> None:
        if self._program_id is None:
            self._notify("Open a record first.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                row = (
                    repo.get_program(self._program_id)
                    if self.kind == "program"
                    else repo.get_procedure(self._program_id)
                )
                old_id = row.supersedes_id
                if not old_id:
                    self._notify("This revision has no predecessor to compare with.")
                    return
                result = (
                    service.compare_programs(old_id, row.id)
                    if self.kind == "program"
                    else service.compare_procedures(old_id, row.id)
                )
            changed = result.get("changed", result)
            self._notify(f"Comparison vs rev predecessor: {str(changed)[:200]}")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    # -- subclass hooks ----------------------------------------------------
    def _build_ui(self) -> None:
        raise NotImplementedError

    def _load_record_into_editor(self) -> None:
        raise NotImplementedError


class ProgramBuilderPage(_AuthoringPageBase):
    """Program library + structured section editor backed by the authoring service."""

    kind = "program"

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(
            QLabel(
                "<b>Program Builder</b> — create, edit, validate, submit and "
                "export exact Program revisions"
            )
        )
        root.addLayout(header)

        create_box = QGroupBox("Create DRAFT Program")
        form = QFormLayout(create_box)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Program title (required)")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Controlled code, e.g. W-17-DRP")
        self.author_input = QLineEdit()
        self.author_input.setPlaceholderText("Declared author attribution (not authentication)")
        self.well_input = QLineEdit()
        self.well_input.setPlaceholderText("Registered well ID (optional scope)")
        self.template_combo = QComboBox()
        self.template_combo.addItem("(blank)", None)
        for name in sorted(PROGRAM_TEMPLATES):
            self.template_combo.addItem(name, name)
        form.addRow("Title", self.title_input)
        form.addRow("Code", self.code_input)
        form.addRow("Author", self.author_input)
        form.addRow("Well ID", self.well_input)
        form.addRow("Template", self.template_combo)
        create_row = QHBoxLayout()
        self.create_button = QPushButton("Create DRAFT")
        self.create_button.clicked.connect(self._create)
        create_row.addWidget(self.create_button)
        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.clicked.connect(self._refresh_records)
        create_row.addWidget(self.refresh_button)
        form.addRow(create_row)
        root.addWidget(create_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.record_list = QListWidget()
        self.record_list.currentItemChanged.connect(lambda *_: self._open_selected())
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Programs (exact revisions)"))
        left_layout.addWidget(self.record_list)
        self.open_button = QPushButton("Open selected revision")
        self.open_button.clicked.connect(self._open_selected)
        left_layout.addWidget(self.open_button)
        splitter.addWidget(left)

        middle = QTabWidget()
        sections_tab = QWidget()
        mid_layout = QVBoxLayout(sections_tab)
        mid_layout.addWidget(QLabel("Sections (table of contents)"))
        self.section_list = QListWidget()
        self.section_list.currentItemChanged.connect(lambda *_: self._load_section())
        mid_layout.addWidget(self.section_list)
        section_buttons = QHBoxLayout()
        self.section_add_button = QPushButton("Add section")
        self.section_add_button.clicked.connect(self._add_section)
        self.section_up_button = QPushButton("Move up")
        self.section_up_button.clicked.connect(lambda: self._move_section(-1))
        self.section_down_button = QPushButton("Move down")
        self.section_down_button.clicked.connect(lambda: self._move_section(1))
        for button in (self.section_add_button, self.section_up_button, self.section_down_button):
            section_buttons.addWidget(button)
        mid_layout.addLayout(section_buttons)
        middle.addTab(sections_tab, "Sections")

        targets_tab = QWidget()
        targets_layout = QVBoxLayout(targets_tab)
        targets_layout.addWidget(QLabel("Planned targets (explicit values and units)"))
        self.target_list = QListWidget()
        self.target_list.currentItemChanged.connect(lambda *_: self._load_target())
        targets_layout.addWidget(self.target_list)
        target_buttons = QHBoxLayout()
        self.target_add_button = QPushButton("Add target")
        self.target_add_button.clicked.connect(self._add_target)
        self.target_up_button = QPushButton("Move up")
        self.target_up_button.clicked.connect(lambda: self._move_target(-1))
        self.target_down_button = QPushButton("Move down")
        self.target_down_button.clicked.connect(lambda: self._move_target(1))
        for button in (self.target_add_button, self.target_up_button, self.target_down_button):
            target_buttons.addWidget(button)
        targets_layout.addLayout(target_buttons)
        middle.addTab(targets_tab, "Targets")
        splitter.addWidget(middle)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        editor_box = QGroupBox("Section editor")
        editor_form = QFormLayout(editor_box)
        self.section_title_input = QLineEdit()
        self.section_key_input = QLineEdit()
        self.section_state_combo = QComboBox()
        self.section_state_combo.addItems(_COMPLETION_STATES)
        self.section_source_input = QLineEdit()
        self.section_content_input = QTextEdit()
        editor_form.addRow("Title", self.section_title_input)
        editor_form.addRow("Section key", self.section_key_input)
        editor_form.addRow("Completion state", self.section_state_combo)
        editor_form.addRow("Source reference", self.section_source_input)
        editor_form.addRow("Content", self.section_content_input)
        self.section_save_button = QPushButton("Save section")
        self.section_save_button.clicked.connect(self._save_section)
        editor_form.addRow(self.section_save_button)
        right_layout.addWidget(editor_box)

        target_box = QGroupBox("Target editor")
        target_form = QFormLayout(target_box)
        self.target_name_input = QLineEdit()
        self.target_hole_input = QLineEdit()
        self.target_hole_input.setPlaceholderText("in")
        self.target_casing_input = QLineEdit()
        self.target_formation_input = QLineEdit()
        self.target_depth_value_input = QLineEdit()
        self.target_depth_unit_input = QLineEdit()
        self.target_depth_unit_input.setPlaceholderText("explicit unit, e.g. m")
        self.target_duration_input = QLineEdit()
        self.target_duration_input.setPlaceholderText("days")
        self.target_mud_value_input = QLineEdit()
        self.target_mud_unit_input = QLineEdit()
        self.target_mud_unit_input.setPlaceholderText("explicit unit, e.g. ppg")
        self.target_npt_input = QLineEdit()
        self.target_npt_input.setPlaceholderText("hours")
        self.target_cost_value_input = QLineEdit()
        self.target_cost_unit_input = QLineEdit()
        self.target_cost_unit_input.setPlaceholderText("currency/unit, e.g. USD")
        self.target_source_input = QLineEdit()
        target_form.addRow("Target name", self.target_name_input)
        target_form.addRow("Hole size (in)", self.target_hole_input)
        target_form.addRow("Casing program", self.target_casing_input)
        target_form.addRow("Formation top", self.target_formation_input)
        target_form.addRow("Planned depth value", self.target_depth_value_input)
        target_form.addRow("Depth unit", self.target_depth_unit_input)
        target_form.addRow("Duration (days)", self.target_duration_input)
        target_form.addRow("Mud weight value", self.target_mud_value_input)
        target_form.addRow("MW unit", self.target_mud_unit_input)
        target_form.addRow("NPT allowance (h)", self.target_npt_input)
        target_form.addRow("Cost value", self.target_cost_value_input)
        target_form.addRow("Cost currency/unit", self.target_cost_unit_input)
        target_form.addRow("Source reference", self.target_source_input)
        self.target_save_button = QPushButton("Save target")
        self.target_save_button.clicked.connect(self._save_target)
        target_form.addRow(self.target_save_button)
        right_layout.addWidget(target_box)
        splitter.addWidget(right)
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.validate_button = QPushButton("Validate (structural only)")
        self.validate_button.clicked.connect(self._validate)
        self.submit_button = QPushButton("Submit for review")
        self.submit_button.clicked.connect(self._submit)
        self.review_button = QPushButton("Review")
        self.review_button.clicked.connect(self._do_review)
        self.approve_button = QPushButton("Approve")
        self.approve_button.clicked.connect(lambda: self._do_decide(True))
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(lambda: self._do_decide(False))
        self.revise_button = QPushButton("Revise (new DRAFT)")
        self.revise_button.clicked.connect(self._do_revise)
        self.compare_button = QPushButton("Compare vs predecessor")
        self.compare_button.clicked.connect(self._do_compare_previous)
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(self._export)
        for button in (
            self.validate_button,
            self.submit_button,
            self.review_button,
            self.approve_button,
            self.reject_button,
            self.revise_button,
            self.compare_button,
            self.export_button,
        ):
            actions.addWidget(button)
        actions.addWidget(QLabel("Note/reason"))
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("reasoned note (required by review/decision)")
        actions.addWidget(self.note_input)
        root.addLayout(actions)
        self.status_label = QLabel("No Program selected.")
        root.addWidget(self.status_label)
        self._refresh_records()

    # -- actions -----------------------------------------------------------
    def _create(self) -> None:
        title = self.title_input.text().strip()
        author = self.author_input.text().strip()
        if not title or not author:
            self._notify("Title and author attribution are required.")
            return
        template = self.template_combo.currentData()
        try:
            with _workspace_session(self._workspace()) as session:
                repo, _service = self._services(session)
                if template:
                    record = create_from_template(
                        _service,
                        "program",
                        template,
                        content={
                            "title": title,
                            "code": self.code_input.text().strip() or None,
                            "author": author,
                            "well_id": self.well_input.text().strip() or None,
                        },
                    )
                else:
                    record = repo.create_program(
                        title=title,
                        code=self.code_input.text().strip(),
                        author=author,
                        created_by=author,
                        well_id=self.well_input.text().strip() or None,
                    )
                self._program_id = record.id
                self._record = repo.get_program(record.id)
            self._refresh_records()
            self._load_record_into_editor()
            self._notify(f"Created DRAFT Program rev {self._record.revision}.")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self._error(exc)

    def _load_record_into_editor(self) -> None:
        if self._program_id is None:
            return
        with _workspace_session(self._workspace()) as session:
            repo, _service = self._services(session)
            self._record = repo.get_program(self._program_id)
            sections = repo.list_program_sections(self._program_id)
        self.section_list.clear()
        for s in sections:
            item = QListWidgetItem(f"{s.sequence}. {s.title} [{s.completion_state}]")
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            self.section_list.addItem(item)
        targets = repo.list_targets(self._program_id)
        self.target_list.clear()
        for t in targets:
            item = QListWidgetItem(
                f"{t.sequence}. {t.name or '-'} — depth "
                f"{t.planned_depth_md_value if t.planned_depth_md_value is not None else '-'} "
                f"{t.planned_depth_md_unit or ''}"
            )
            item.setData(Qt.ItemDataRole.UserRole, t.id)
            self.target_list.addItem(item)
        self._notify(
            f"{self._record.code or '-'} rev {self._record.revision} — {self._record.status}"
            + (" (current)" if self._record.is_current else " (historical)")
        )

    # -- target editor -----------------------------------------------------
    @staticmethod
    def _num(text: str) -> float | None:
        text = text.strip()
        if not text:
            return None
        value = float(text)  # ValueError surfaces as a validation error below
        return value

    def _selected_target_id(self) -> str | None:
        item = self.target_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_target(self) -> None:
        target_id = self._selected_target_id()
        if target_id is None or self._program_id is None:
            return
        with _workspace_session(self._workspace()) as session:
            repo, _service = self._services(session)
            targets = {t.id: t for t in repo.list_targets(self._program_id)}
        t = targets.get(target_id)
        if t is None:
            return
        self.target_name_input.setText(t.name or "")
        self.target_hole_input.setText("" if t.hole_size_in is None else str(t.hole_size_in))
        self.target_casing_input.setText(t.casing_program or "")
        self.target_formation_input.setText(t.formation_top or "")
        self.target_depth_value_input.setText(
            "" if t.planned_depth_md_value is None else str(t.planned_depth_md_value)
        )
        self.target_depth_unit_input.setText(t.planned_depth_md_unit or "")
        self.target_duration_input.setText(
            "" if t.planned_duration_days is None else str(t.planned_duration_days)
        )
        self.target_mud_value_input.setText(
            "" if t.planned_mud_weight_value is None else str(t.planned_mud_weight_value)
        )
        self.target_mud_unit_input.setText(t.planned_mud_weight_unit or "")
        self.target_npt_input.setText(
            "" if t.planned_npt_hours is None else str(t.planned_npt_hours)
        )
        self.target_cost_value_input.setText(
            "" if t.planned_cost_value is None else str(t.planned_cost_value)
        )
        self.target_cost_unit_input.setText(t.planned_cost_unit or "")
        source = ""
        for entry in t.provenance or []:
            if isinstance(entry, dict) and entry.get("source_reference"):
                source = str(entry["source_reference"])
                break
        self.target_source_input.setText(source)

    def _target_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "name": self.target_name_input.text().strip(),
            "hole_size_in": self._num(self.target_hole_input.text()),
            "casing_program": self.target_casing_input.text().strip() or None,
            "formation_top": self.target_formation_input.text().strip() or None,
            "planned_depth_md_value": self._num(self.target_depth_value_input.text()),
            "planned_depth_md_unit": self.target_depth_unit_input.text().strip(),
            "planned_duration_days": self._num(self.target_duration_input.text()),
            "planned_mud_weight_value": self._num(self.target_mud_value_input.text()),
            "planned_mud_weight_unit": self.target_mud_unit_input.text().strip(),
            "planned_npt_hours": self._num(self.target_npt_input.text()),
            "planned_cost_value": self._num(self.target_cost_value_input.text()),
            "planned_cost_unit": self.target_cost_unit_input.text().strip(),
        }
        source = self.target_source_input.text().strip()
        fields["provenance"] = [{"source_reference": source}] if source else []
        return fields

    def _add_target(self) -> None:
        if self._program_id is None:
            self._notify("Open or create a Program first.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                service.add_program_target(
                    program.id,
                    expected_updated_at=self._token(session, program),
                    name=f"Target {self.target_list.count() + 1}",
                )
            self._load_record_into_editor()
            self._notify("Target added (DRAFT).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _save_target(self) -> None:
        target_id = self._selected_target_id()
        if target_id is None or self._program_id is None:
            self._notify("Select a target to save.")
            return
        try:
            fields = self._target_fields()
        except ValueError:
            self._notify("Error: numeric fields must be finite numbers (no NaN/Infinity/bool).")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                service.update_program_target(
                    program.id,
                    target_id,
                    expected_updated_at=self._token(session, program),
                    changes=fields,
                )
            self._load_record_into_editor()
            self._notify("Target saved.")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _move_target(self, delta: int) -> None:
        target_id = self._selected_target_id()
        if target_id is None or self._program_id is None:
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                targets = repo.list_targets(self._program_id)
                ordered = [t.id for t in targets]
                index = ordered.index(target_id)
                new_index = max(0, min(len(ordered) - 1, index + delta))
                if new_index != index:
                    ordered.insert(new_index, ordered.pop(index))
                    service.reorder_program_targets(
                        program.id,
                        expected_updated_at=self._token(session, program),
                        ordered_ids=ordered,
                    )
            self._load_record_into_editor()
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _selected_section_id(self) -> str | None:
        item = self.section_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_section(self) -> None:
        section_id = self._selected_section_id()
        if section_id is None or self._program_id is None:
            return
        with _workspace_session(self._workspace()) as session:
            repo, _service = self._services(session)
            sections = {s.id: s for s in repo.list_program_sections(self._program_id)}
        s = sections.get(section_id)
        if s is None:
            return
        self.section_title_input.setText(s.title)
        self.section_key_input.setText(s.section_key)
        index = self.section_state_combo.findText(s.completion_state)
        self.section_state_combo.setCurrentIndex(index if index >= 0 else 0)
        self.section_source_input.setText(s.source_reference or "")
        self.section_content_input.setPlainText(s.content)

    def _add_section(self) -> None:
        if self._program_id is None:
            self._notify("Open or create a Program first.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                service.add_program_section(
                    program.id,
                    expected_updated_at=self._token(session, program),
                    section_key=f"section_{self.section_list.count() + 1}",
                    title=f"New section {self.section_list.count() + 1}",
                    content="",
                )
            self._load_record_into_editor()
            self._notify("Section added (DRAFT).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _save_section(self) -> None:
        section_id = self._selected_section_id()
        if section_id is None or self._program_id is None:
            self._notify("Select a section to save.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                service.update_program_section(
                    program.id,
                    section_id,
                    expected_updated_at=self._token(session, program),
                    changes={
                        "title": self.section_title_input.text(),
                        "completion_state": self.section_state_combo.currentText(),
                        "source_reference": self.section_source_input.text() or None,
                        "content": self.section_content_input.toPlainText(),
                    },
                )
            self._load_record_into_editor()
            self._notify("Section saved.")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _move_section(self, delta: int) -> None:
        section_id = self._selected_section_id()
        if section_id is None or self._program_id is None:
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                sections = repo.list_program_sections(self._program_id)
                ordered = [s.id for s in sections]
                index = ordered.index(section_id)
                new_index = max(0, min(len(ordered) - 1, index + delta))
                if new_index != index:
                    ordered.insert(new_index, ordered.pop(index))
                    service.reorder_program_sections(
                        program.id,
                        expected_updated_at=self._token(session, program),
                        ordered_ids=ordered,
                    )
            self._load_record_into_editor()
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _validate(self) -> None:
        if self._program_id is None:
            self._notify("Open a Program first.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                _repo, service = self._services(session)
                result = service.validate_program(self._program_id)
            ready = result.get("ready_for_review")
            missing = result.get("missing", [])
            self._notify(
                f"Structural validation: ready_for_review={ready}; missing={missing}. "
                "This is not a technical approval."
            )
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _submit(self) -> None:
        if self._program_id is None:
            self._notify("Open a Program first.")
            return
        author = self.author_input.text().strip() or "ui-author"
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                program = repo.get_program(self._program_id)
                service.submit(
                    "program",
                    program.id,
                    expected_updated_at=self._token(session, program),
                    by=author,
                )
            self._load_record_into_editor()
            self._notify("Submitted for review (submission is not approval).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _export(self) -> None:
        if self._program_id is None:
            self._notify("Open a Program first.")
            return
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Export Program",
            "program.docx",
            "Documents (*.docx);;JSON (*.json);;HTML (*.html);;PDF (*.pdf)",
        )
        if not path:
            return
        fmt = path.rsplit(".", 1)[-1].lower()
        if fmt not in ("json", "html", "docx", "pdf"):
            fmt = "docx"
        try:
            with _workspace_session(self._workspace()) as session:
                _repo, service = self._services(session)
                result = export_record(service, "program", self._program_id, fmt=fmt, out_path=path)
            self._notify(f"Exported to {result['path']} ({result['format']}).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)


class ProcedureBuilderPage(_AuthoringPageBase):
    """Procedure library + ordered step editor backed by the authoring service."""

    kind = "procedure"

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "<b>Procedure Builder</b> — create, edit, validate, link to an "
                "exact Program revision, and export Procedures"
            )
        )

        create_box = QGroupBox("Create DRAFT Procedure")
        form = QFormLayout(create_box)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Procedure title (required)")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Controlled code")
        self.author_input = QLineEdit()
        self.author_input.setPlaceholderText("Declared author attribution (not authentication)")
        self.well_input = QLineEdit()
        self.well_input.setPlaceholderText("Registered well ID (optional scope)")
        self.template_combo = QComboBox()
        self.template_combo.addItem("(blank)", None)
        for name in sorted(PROCEDURE_TEMPLATES):
            self.template_combo.addItem(name, name)
        form.addRow("Title", self.title_input)
        form.addRow("Code", self.code_input)
        form.addRow("Author", self.author_input)
        form.addRow("Well ID", self.well_input)
        form.addRow("Template", self.template_combo)
        create_row = QHBoxLayout()
        self.create_button = QPushButton("Create DRAFT")
        self.create_button.clicked.connect(self._create)
        self.refresh_button = QPushButton("Refresh list")
        self.refresh_button.clicked.connect(self._refresh_records)
        create_row.addWidget(self.create_button)
        create_row.addWidget(self.refresh_button)
        form.addRow(create_row)
        root.addWidget(create_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.record_list = QListWidget()
        self.record_list.currentItemChanged.connect(lambda *_: self._open_selected())
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Procedures (exact revisions)"))
        left_layout.addWidget(self.record_list)
        self.open_button = QPushButton("Open selected revision")
        self.open_button.clicked.connect(self._open_selected)
        left_layout.addWidget(self.open_button)
        splitter.addWidget(left)

        middle = QWidget()
        mid_layout = QVBoxLayout(middle)
        mid_layout.addWidget(QLabel("Steps (ordered)"))
        self.step_list = QListWidget()
        self.step_list.currentItemChanged.connect(lambda *_: self._load_step())
        mid_layout.addWidget(self.step_list)
        step_buttons = QHBoxLayout()
        self.step_add_button = QPushButton("Add step")
        self.step_add_button.clicked.connect(self._add_step)
        self.step_up_button = QPushButton("Move up")
        self.step_up_button.clicked.connect(lambda: self._move_step(-1))
        self.step_down_button = QPushButton("Move down")
        self.step_down_button.clicked.connect(lambda: self._move_step(1))
        for button in (self.step_add_button, self.step_up_button, self.step_down_button):
            step_buttons.addWidget(button)
        mid_layout.addLayout(step_buttons)
        splitter.addWidget(middle)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        editor_box = QGroupBox("Step editor")
        editor_form = QFormLayout(editor_box)
        self.step_title_input = QLineEdit()
        self.step_instructions_input = QTextEdit()
        self.step_role_input = QLineEdit()
        self.step_equipment_input = QTextEdit()
        self.step_preconditions_input = QTextEdit()
        self.step_parameters_input = QTextEdit()
        self.step_parameters_input.setPlaceholderText(
            'strict JSON array, e.g. [{"name":"WOB","value":25,"unit":"kN",'
            '"source_reference":"SRC-1"}]'
        )
        self.step_expected_input = QTextEdit()
        self.step_acceptance_input = QTextEdit()
        self.step_evidence_input = QTextEdit()
        self.step_safety_input = QTextEdit()
        self.step_contingency_input = QTextEdit()
        self.step_hold_check = QComboBox()
        self.step_hold_check.addItems(["no", "yes (HOLD POINT)"])
        self.step_source_input = QLineEdit()
        editor_form.addRow("Title", self.step_title_input)
        editor_form.addRow("Instructions", self.step_instructions_input)
        editor_form.addRow("Responsible role", self.step_role_input)
        editor_form.addRow("Equipment", self.step_equipment_input)
        editor_form.addRow("Preconditions", self.step_preconditions_input)
        editor_form.addRow("Parameters (JSON)", self.step_parameters_input)
        editor_form.addRow("Expected result", self.step_expected_input)
        editor_form.addRow("Acceptance criteria", self.step_acceptance_input)
        editor_form.addRow("Evidence required", self.step_evidence_input)
        editor_form.addRow("Safety controls", self.step_safety_input)
        editor_form.addRow("Contingency", self.step_contingency_input)
        editor_form.addRow("Hold point", self.step_hold_check)
        editor_form.addRow("Source reference", self.step_source_input)
        self.step_save_button = QPushButton("Save step")
        self.step_save_button.clicked.connect(self._save_step)
        editor_form.addRow(self.step_save_button)
        right_layout.addWidget(editor_box)
        splitter.addWidget(right)
        root.addWidget(splitter, 1)

        link_box = QGroupBox("Exact Program revision link")
        link_layout = QHBoxLayout(link_box)
        self.program_combo = QComboBox()
        self.refresh_programs_button = QPushButton("Reload Programs")
        self.refresh_programs_button.clicked.connect(self._reload_programs)
        self.link_button = QPushButton("Link to selected Program revision")
        self.link_button.clicked.connect(self._link_program)
        self.link_status_label = QLabel("NONE")
        link_layout.addWidget(QLabel("Program:"))
        link_layout.addWidget(self.program_combo)
        link_layout.addWidget(self.refresh_programs_button)
        link_layout.addWidget(self.link_button)
        link_layout.addWidget(self.link_status_label)
        root.addWidget(link_box)
        self.link_impact_label = QLabel("Link impact: NONE — no exact Program revision is linked.")
        root.addWidget(self.link_impact_label)

        actions = QHBoxLayout()
        self.validate_button = QPushButton("Validate (structural only)")
        self.validate_button.clicked.connect(self._validate)
        self.submit_button = QPushButton("Submit for review")
        self.submit_button.clicked.connect(self._submit)
        self.review_button = QPushButton("Review")
        self.review_button.clicked.connect(self._do_review)
        self.approve_button = QPushButton("Approve")
        self.approve_button.clicked.connect(lambda: self._do_decide(True))
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(lambda: self._do_decide(False))
        self.revise_button = QPushButton("Revise (new DRAFT)")
        self.revise_button.clicked.connect(self._do_revise)
        self.compare_button = QPushButton("Compare vs predecessor")
        self.compare_button.clicked.connect(self._do_compare_previous)
        self.export_button = QPushButton("Export…")
        self.export_button.clicked.connect(self._export)
        for button in (
            self.validate_button,
            self.submit_button,
            self.review_button,
            self.approve_button,
            self.reject_button,
            self.revise_button,
            self.compare_button,
            self.export_button,
        ):
            actions.addWidget(button)
        actions.addWidget(QLabel("Note/reason"))
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("reasoned note (required by review/decision)")
        actions.addWidget(self.note_input)
        root.addLayout(actions)
        self.status_label = QLabel("No Procedure selected.")
        root.addWidget(self.status_label)
        self._refresh_records()
        self._reload_programs()

    def _create(self) -> None:
        title = self.title_input.text().strip()
        author = self.author_input.text().strip()
        if not title or not author:
            self._notify("Title and author attribution are required.")
            return
        template = self.template_combo.currentData()
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                if template:
                    record = create_from_template(
                        service,
                        "procedure",
                        template,
                        content={
                            "title": title,
                            "code": self.code_input.text().strip() or None,
                            "created_by": author,
                            "well_id": self.well_input.text().strip() or None,
                        },
                    )
                else:
                    record = repo.create_procedure(
                        title=title,
                        code=self.code_input.text().strip(),
                        created_by=author,
                        well_id=self.well_input.text().strip() or None,
                    )
                self._program_id = record.id
                self._record = repo.get_procedure(record.id)
            self._refresh_records()
            self._load_record_into_editor()
            self._notify(f"Created DRAFT Procedure rev {self._record.revision}.")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _reload_programs(self) -> None:
        self.program_combo.clear()
        try:
            with _workspace_session(self._workspace()) as session:
                repo, _service = self._services(session)
                programs = repo.list_programs()
        except Exception:  # noqa: BLE001 - no workspace yet
            return
        for row in programs:
            self.program_combo.addItem(
                f"{row.code or '-'} rev {row.revision} [{row.status}]", row.id
            )

    def _load_record_into_editor(self) -> None:
        if self._program_id is None:
            return
        with _workspace_session(self._workspace()) as session:
            repo, service = self._services(session)
            self._record = repo.get_procedure(self._program_id)
            steps = repo.list_procedure_steps(self._program_id)
            link = service.procedure_program_link(self._program_id)
        self.step_list.clear()
        for s in steps:
            item = QListWidgetItem(f"{s.sequence}. {s.title}" + (" [HOLD]" if s.hold_point else ""))
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            self.step_list.addItem(item)
        self.link_status_label.setText(str(link.get("link_status", "NONE")))
        status = str(link.get("link_status", "NONE"))
        if status == "STALE":
            self.link_impact_label.setText(
                "Link impact: STALE — the linked Program revision is superseded. "
                "Review the linked revision's diff before relying on this procedure; "
                "the link is never auto-updated."
            )
        elif status == "LINKED":
            self.link_impact_label.setText(
                f"Link impact: LINKED to Program {link.get('program_code') or ''} "
                f"rev {link.get('program_revision')} [{link.get('program_status')}] "
                "(exact revision; no automatic relinking)."
            )
        else:
            self.link_impact_label.setText(
                "Link impact: NONE — no exact Program revision is linked."
            )
        self._notify(
            f"{self._record.code or '-'} rev {self._record.revision} — {self._record.status}"
            + (" (current)" if self._record.is_current else " (historical)")
        )

    def _selected_step_id(self) -> str | None:
        item = self.step_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_step(self) -> None:
        step_id = self._selected_step_id()
        if step_id is None or self._program_id is None:
            return
        with _workspace_session(self._workspace()) as session:
            repo, _service = self._services(session)
            steps = {s.id: s for s in repo.list_procedure_steps(self._program_id)}
        s = steps.get(step_id)
        if s is None:
            return
        import json as _json

        self.step_title_input.setText(s.title)
        self.step_instructions_input.setPlainText(s.instructions or "")
        self.step_role_input.setText(s.responsible_role or "")
        self.step_equipment_input.setPlainText(s.equipment or "")
        self.step_preconditions_input.setPlainText(s.preconditions or "")
        self.step_parameters_input.setPlainText(
            _json.dumps(s.parameters, ensure_ascii=False) if s.parameters else ""
        )
        self.step_expected_input.setPlainText(s.expected_result or "")
        self.step_acceptance_input.setPlainText(s.acceptance_criteria or "")
        self.step_evidence_input.setPlainText(s.evidence_required or "")
        self.step_safety_input.setPlainText(s.safety_controls or "")
        self.step_contingency_input.setPlainText(s.contingency or "")
        self.step_hold_check.setCurrentIndex(1 if s.hold_point else 0)
        self.step_source_input.setText(s.source_reference or "")

    def _add_step(self) -> None:
        if self._program_id is None:
            self._notify("Open or create a Procedure first.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                procedure = repo.get_procedure(self._program_id)
                service.add_procedure_step(
                    procedure.id,
                    expected_updated_at=self._token(session, procedure),
                    title=f"Step {self.step_list.count() + 1}",
                    instructions="Enter controlled source-backed instruction text.",
                )
            self._load_record_into_editor()
            self._notify("Step added (DRAFT).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _save_step(self) -> None:
        step_id = self._selected_step_id()
        if step_id is None or self._program_id is None:
            self._notify("Select a step to save.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                procedure = repo.get_procedure(self._program_id)
                import json as _json

                params_text = self.step_parameters_input.toPlainText().strip()
                parameters = _json.loads(params_text) if params_text else []
                service.update_procedure_step(
                    procedure.id,
                    step_id,
                    expected_updated_at=self._token(session, procedure),
                    changes={
                        "title": self.step_title_input.text(),
                        "instructions": self.step_instructions_input.toPlainText(),
                        "responsible_role": self.step_role_input.text() or None,
                        "equipment": self.step_equipment_input.toPlainText() or None,
                        "preconditions": self.step_preconditions_input.toPlainText() or None,
                        "parameters": parameters,
                        "expected_result": self.step_expected_input.toPlainText() or None,
                        "acceptance_criteria": self.step_acceptance_input.toPlainText() or None,
                        "evidence_required": self.step_evidence_input.toPlainText() or None,
                        "safety_controls": self.step_safety_input.toPlainText() or None,
                        "contingency": self.step_contingency_input.toPlainText() or None,
                        "hold_point": self.step_hold_check.currentIndex() == 1,
                        "source_reference": self.step_source_input.text() or None,
                    },
                )
            self._load_record_into_editor()
            self._notify("Step saved.")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _move_step(self, delta: int) -> None:
        step_id = self._selected_step_id()
        if step_id is None or self._program_id is None:
            return
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                procedure = repo.get_procedure(self._program_id)
                steps = repo.list_procedure_steps(self._program_id)
                ordered = [s.id for s in steps]
                index = ordered.index(step_id)
                new_index = max(0, min(len(ordered) - 1, index + delta))
                if new_index != index:
                    ordered.insert(new_index, ordered.pop(index))
                    service.reorder_procedure_steps(
                        procedure.id,
                        expected_updated_at=self._token(session, procedure),
                        ordered_ids=ordered,
                    )
            self._load_record_into_editor()
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _link_program(self) -> None:
        if self._program_id is None:
            self._notify("Open a Procedure first.")
            return
        program_id = self.program_combo.currentData()
        if not program_id:
            self._notify("No Program revision available to link.")
            return
        author = self.author_input.text().strip() or "ui-author"
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                procedure = repo.get_procedure(self._program_id)
                service.link_procedure_to_program(
                    procedure.id,
                    program_id,
                    expected_updated_at=self._token(session, procedure),
                    by=author,
                    note="Linked from Procedure Builder UI",
                )
            self._load_record_into_editor()
            self._notify("Linked to exact Program revision (no automatic relinking).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _validate(self) -> None:
        if self._program_id is None:
            self._notify("Open a Procedure first.")
            return
        try:
            with _workspace_session(self._workspace()) as session:
                _repo, service = self._services(session)
                result = service.validate_procedure(self._program_id)
            self._notify(
                f"Structural validation: ready_for_review={result.get('ready_for_review')}; "
                f"missing={result.get('missing', [])}. Not a technical approval."
            )
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _submit(self) -> None:
        if self._program_id is None:
            self._notify("Open a Procedure first.")
            return
        author = self.author_input.text().strip() or "ui-author"
        try:
            with _workspace_session(self._workspace()) as session:
                repo, service = self._services(session)
                procedure = repo.get_procedure(self._program_id)
                service.submit(
                    "procedure",
                    procedure.id,
                    expected_updated_at=self._token(session, procedure),
                    by=author,
                )
            self._load_record_into_editor()
            self._notify("Submitted for review (submission is not approval).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _export(self) -> None:
        if self._program_id is None:
            self._notify("Open a Procedure first.")
            return
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Export Procedure",
            "procedure.docx",
            "Documents (*.docx);;JSON (*.json);;HTML (*.html);;PDF (*.pdf)",
        )
        if not path:
            return
        fmt = path.rsplit(".", 1)[-1].lower()
        if fmt not in ("json", "html", "docx", "pdf"):
            fmt = "docx"
        try:
            with _workspace_session(self._workspace()) as session:
                _repo, service = self._services(session)
                result = export_record(
                    service, "procedure", self._program_id, fmt=fmt, out_path=path
                )
            self._notify(f"Exported to {result['path']} ({result['format']}).")
        except Exception as exc:  # noqa: BLE001
            self._error(exc)


__all__ = ["ProcedureBuilderPage", "ProgramBuilderPage"]
