"""Batch DDR Import page (Phase D / D-14).

A first-class Qt workflow for manifest-driven batch import of DDR PDFs:

1. Select multiple PDF files through a native file dialog (explicit UI selection).
2. Selected files are COPIED into a controlled staging directory inside the
   workspace — the batch service's path-containment rule (D-15) then always holds
   and an untrusted manifest can never escape its declared root.
3. Choose a registered well when known.
4. Inspect/remove selected files, start the batch on a worker thread with a live
   progress bar and safe cancellation, and inspect per-file outcomes.
5. Retry failed items; reopen a previous batch (staging dir + manifest + outcome
   JSON persist under the workspace).
6. See the exact-version review queue (D-17).

The page never blocks the Qt event loop and never duplicates import logic: it calls
the same `import_ddr_batch` domain service as any CLI surface.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import DrillingIntelligenceError
from ..operations.ddr_batch import (
    MANIFEST_FORMAT,
    import_ddr_batch,
    load_manifest,
    review_queue,
)


class _BatchWorker(QObject):
    finished = Signal(object)  # BatchImportSummary
    failed = Signal(str)
    entry_done = Signal(int, int)

    def __init__(self, workspace, manifest_path: Path, base_dir: Path, well_id: str | None):
        super().__init__()
        self._workspace = workspace
        self._manifest = manifest_path
        self._base = base_dir
        self._well_id = well_id

    def run(self) -> None:
        from ..cli.app import _workspace_row_id
        from ..documents.registry import DocumentRegistry
        from ..documents.repository import DocumentRepository
        from ..extraction.registry import build_default_router

        try:
            ws = self._workspace
            with ws.database.unit_of_work() as session:
                registry = DocumentRegistry(
                    DocumentRepository(session),
                    router=build_default_router(ws.settings),
                    settings=ws.settings,
                )
                total = len(load_manifest(self._manifest))
                done = 0

                def on_entry(_result: Any) -> None:
                    nonlocal done
                    done += 1
                    self.entry_done.emit(done, total)

                def cancelled() -> bool:
                    return QThread.currentThread().isInterruptionRequested()

                summary = import_ddr_batch(
                    registry,
                    manifest_path=self._manifest,
                    base_dir=self._base,
                    workspace_root=ws.root,
                    workspace_id=_workspace_row_id(ws),
                    default_well_id=self._well_id,
                    should_cancel=cancelled,
                    on_entry=on_entry,
                )
            self.finished.emit(summary)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI status line
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class BatchImportPage(QWidget):
    """Desktop batch DDR import: select, stage, import, inspect, retry, review."""

    def __init__(self, workspace_getter: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_getter = workspace_getter
        self._selected: list[Path] = []
        self._thread: QThread | None = None
        self._worker: _BatchWorker | None = None
        self._last_summary: Any = None
        self._build_ui()

    # -- helpers -----------------------------------------------------------
    def _workspace(self) -> Any:
        workspace = self._workspace_getter()
        if workspace is None:
            raise DrillingIntelligenceError("no workspace is open")
        return workspace

    def _staging_dir(self) -> Path:
        root = self._workspace().root / ".drillintel" / "batch_imports" / uuid.uuid4().hex[:12]
        root.mkdir(parents=True, exist_ok=True)
        return root

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(
            QLabel(
                "<b>Batch DDR Import</b> — select synthetic/source PDFs, stage them into the "
                "workspace, run the manifest-driven import, and inspect per-file outcomes."
            )
        )

        pick_row = QHBoxLayout()
        self.add_button = QPushButton("Add PDF files…")
        self.add_button.clicked.connect(self._add_files)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._remove_selected)
        self.well_combo = QComboBox()
        self.well_combo.addItem("(no well)", None)
        self.refresh_wells_button = QPushButton("Refresh wells")
        self.refresh_wells_button.clicked.connect(self._reload_wells)
        pick_row.addWidget(self.add_button)
        pick_row.addWidget(self.remove_button)
        pick_row.addWidget(QLabel("Well:"))
        pick_row.addWidget(self.well_combo)
        pick_row.addWidget(self.refresh_wells_button)
        root.addLayout(pick_row)

        self.file_list = QListWidget()
        root.addWidget(QLabel("Selected files (staged into the workspace on import):"))
        root.addWidget(self.file_list)

        run_row = QHBoxLayout()
        self.start_button = QPushButton("Start batch import")
        self.start_button.clicked.connect(self._start_batch)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_batch)
        self.cancel_button.setEnabled(False)
        self.retry_button = QPushButton("Retry failed")
        self.retry_button.clicked.connect(self._retry_failed)
        self.retry_button.setEnabled(False)
        self.queue_button = QPushButton("Show review queue")
        self.queue_button.clicked.connect(self._show_queue)
        run_row.addWidget(self.start_button)
        run_row.addWidget(self.cancel_button)
        run_row.addWidget(self.retry_button)
        run_row.addWidget(self.queue_button)
        root.addLayout(run_row)

        # F: real batch history — every previous batch under the workspace is
        # listed from its persisted manifest + outcome.json and can be reopened.
        history_row = QHBoxLayout()
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(260)
        self.reopen_button = QPushButton("Reopen batch")
        self.reopen_button.clicked.connect(self._reopen_selected_batch)
        self.refresh_history_button = QPushButton("Refresh history")
        self.refresh_history_button.clicked.connect(self._reload_history)
        history_row.addWidget(QLabel("Previous batches:"))
        history_row.addWidget(self.history_combo)
        history_row.addWidget(self.reopen_button)
        history_row.addWidget(self.refresh_history_button)
        root.addLayout(history_row)
        self._reload_history()

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        root.addWidget(self.progress)
        self.summary_label = QLabel("No batch run yet.")
        root.addWidget(self.summary_label)

        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(
            ["File", "Status", "Classification", "Pending review", "Error"]
        )
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.results_table)
        self.status_label = QLabel("")
        root.addWidget(self.status_label)

    # -- wells -------------------------------------------------------------
    def _reload_wells(self) -> None:
        self.well_combo.clear()
        self.well_combo.addItem("(no well)", None)
        try:
            from ..wells.repository import WellRepository

            with self._workspace().database.read_only() as session:
                wells = WellRepository(session)
                for row in wells.list_wells():
                    self.well_combo.addItem(f"{row.name}", row.id)
        except DrillingIntelligenceError:
            pass

    # -- file selection ------------------------------------------------------
    def _add_files(self) -> None:
        try:
            self._workspace()
        except DrillingIntelligenceError as exc:
            self.status_label.setText(f"Error: {exc}")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select DDR PDF files (synthetic fixtures allowed)", "", "PDF files (*.pdf)"
        )
        for raw in paths:
            path = Path(raw)
            if path in self._selected:
                continue
            self._selected.append(path)
            self.file_list.addItem(QListWidgetItem(str(path)))
        self.status_label.setText(f"{len(self._selected)} file(s) selected.")

    def _remove_selected(self) -> None:
        for item in self.file_list.selectedItems():
            path = Path(item.text())
            if path in self._selected:
                self._selected.remove(path)
            self.file_list.takeItem(self.file_list.row(item))

    # -- batch run -----------------------------------------------------------
    def _write_manifest(self, staging: Path, files: list[Path]) -> Path:
        """E1: collision-resistant deterministic staging names.

        Two sources both named ``DDR.pdf`` from different folders must never
        overwrite each other; the staged name keeps the original filename after a
        stable index prefix, and the manifest note preserves the original name and
        source directory.
        """
        manifest_files = []
        seen: set[str] = set()
        self._staged_to_original = {}
        for index, src in enumerate(files):
            staged_name = f"{index:03d}__{src.name}"
            if staged_name in seen:
                raise DrillingIntelligenceError(f"duplicate staged name for {src}")
            seen.add(staged_name)
            shutil.copy2(src, staging / staged_name)
            self._staged_to_original[staged_name] = src
            manifest_files.append(
                {
                    "path": staged_name,
                    "note": f"original={src.name};from={src.parent.name}",
                }
            )
        manifest = staging / "manifest.json"
        manifest.write_text(
            json.dumps({"format": MANIFEST_FORMAT, "files": manifest_files}, indent=2)
        )
        return manifest

    def _start_batch(self) -> None:
        self._run_batch(self._selected)

    def _run_batch(self, files: list[Path]) -> None:
        if not files:
            self.status_label.setText("Select at least one PDF file first.")
            return
        try:
            workspace = self._workspace()
        except DrillingIntelligenceError as exc:
            self.status_label.setText(f"Error: {exc}")
            return
        staging = self._staging_dir()
        try:
            manifest = self._write_manifest(staging, files)
        except OSError as exc:
            self.status_label.setText(f"Error staging files: {exc}")
            return
        self._staging = staging
        self._manifest = manifest
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)
        self.progress.setRange(0, len(files))
        self.progress.setValue(0)
        self.status_label.setText("Batch import running…")

        self._thread = QThread(self)
        self._worker = _BatchWorker(workspace, manifest, staging, self.well_combo.currentData())
        self._worker.moveToThread(self._thread)
        self._worker.entry_done.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_batch_finished)
        self._worker.failed.connect(self._on_batch_failed)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _cancel_batch(self) -> None:
        if self._thread is not None:
            self._thread.requestInterruption()
            self.status_label.setText("Cancellation requested…")

    def _on_batch_finished(self, summary: Any) -> None:
        try:
            self._last_summary = summary
            self._fill_results(summary)
            self._render_summary(summary)
        finally:
            self._cleanup_thread()
            self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.retry_button.setEnabled(
            any(e.status in ("FAILED", "UNSUPPORTED", "CANCELLED") for e in summary.entries)
        )
        self.progress.setValue(max(1, self.progress.maximum()))
        self.status_label.setText("Batch import finished.")

    def _render_summary(self, summary: Any) -> None:
        d = summary.to_dict()
        self.summary_label.setText(
            f"Batch done: total={d['total']} imported={d['imported']} "
            f"duplicates={d['skipped_duplicate']} failed={d['failed']} pending_review={d['pending_review']}"
        )
        # F: persist the outcome durably (temp file + fsync + atomic rename) next to
        # the manifest so the batch can be reopened later even after a crash.
        self._write_durable_outcome(self._staging, d)

    @staticmethod
    def _write_durable_outcome(staging: Path, payload: dict[str, Any]) -> None:
        import os

        tmp = staging / f".outcome.{uuid.uuid4().hex[:12]}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(staging / "outcome.json")
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _on_batch_failed(self, message: str) -> None:
        self._cleanup_thread()
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.status_label.setText(f"Error: {message}")

    def _cleanup_thread(self) -> None:
        import shiboken6

        thread, worker = self._thread, self._worker
        self._thread = None
        self._worker = None
        if thread is not None and shiboken6.isValid(thread):
            try:
                thread.requestInterruption()
                thread.quit()
                thread.wait(5000)
            except RuntimeError:
                pass  # C++ object already gone; nothing to join
        if worker is not None and shiboken6.isValid(worker):
            worker.deleteLater()

    def closeEvent(self, event: Any) -> None:
        self._cleanup_thread()
        super().closeEvent(event)

    def _retry_failed(self) -> None:
        if self._last_summary is None:
            return
        retry = []
        for e in self._last_summary.entries:
            status = e.get("status") if isinstance(e, dict) else e.status
            filename = e.get("filename") if isinstance(e, dict) else e.filename
            if status in ("FAILED", "UNSUPPORTED", "CANCELLED"):
                retry.append(Path(filename))
        # E4: retry by entry identity (staged name), resolving to the CURRENT
        # original source so a corrected file is what gets re-imported.
        originals = []
        for r in retry:
            original = getattr(self, "_staged_to_original", {}).get(r.name)
            if original is None:
                original = next((p for p in self._selected if p.name == r.name), None)
            if original is not None:
                originals.append(original)
        if originals:
            self._run_batch(originals)
        else:
            # Fall back to the staged copies of the failed entries.
            staged = [self._staging / r.name for r in retry if (self._staging / r.name).exists()]
            if staged:
                self._run_batch(staged)
            else:
                self.status_label.setText("No retryable files found.")

    def _fill_results(self, summary: Any) -> None:
        self.results_table.setRowCount(0)
        for entry in summary.entries:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            values = [
                entry.filename,
                entry.status,
                entry.classification or "-",
                "yes" if entry.pending_review else "no",
                entry.error or "",
            ]
            for col, value in enumerate(values):
                self.results_table.setItem(row, col, QTableWidgetItem(str(value)))

    # -- batch history / reopen (Phase F) ------------------------------------
    def _batches_root(self) -> Path:
        return self._workspace().root / ".drillintel" / "batch_imports"

    def _reload_history(self) -> None:
        self.history_combo.clear()
        try:
            root = self._batches_root()
        except DrillingIntelligenceError:
            return
        if not root.is_dir():
            return
        for child in sorted(root.iterdir(), reverse=True):
            if child.is_dir() and (child / "manifest.json").is_file():
                has_outcome = (child / "outcome.json").is_file()
                label = f"{child.name} ({'done' if has_outcome else 'no outcome'})"
                self.history_combo.addItem(label, str(child))

    def _reopen_selected_batch(self) -> None:
        """F: reopen a previous batch from its durable staging + outcome.json."""
        staging_raw = self.history_combo.currentData()
        if not staging_raw:
            self.status_label.setText("No previous batch selected.")
            return
        staging = Path(staging_raw)
        outcome = staging / "outcome.json"
        if not outcome.is_file():
            self.status_label.setText("Selected batch has no persisted outcome.json yet.")
            return
        try:
            payload = json.loads(outcome.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.status_label.setText(f"Cannot reopen batch: {exc}")
            return
        self._staging = staging
        self._manifest = staging / "manifest.json"
        entries = payload.get("entries") or []
        self._last_summary = type("ReopenedSummary", (), {"entries": []})()
        # Render the persisted entries directly.
        self.results_table.setRowCount(0)
        self.results_table.setColumnCount(5)
        self.results_table.setHorizontalHeaderLabels(
            ["File", "Status", "Classification", "Pending review", "Error"]
        )
        for e in entries:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            for col, value in enumerate(
                [
                    e.get("filename", ""),
                    e.get("status", ""),
                    e.get("classification") or "-",
                    "yes" if e.get("pending_review") else "no",
                    e.get("error") or "",
                ]
            ):
                self.results_table.setItem(row, col, QTableWidgetItem(str(value)))
        self.summary_label.setText(
            f"Reopened batch {staging.name}: total={payload.get('total')} "
            f"imported={payload.get('imported')} failed={payload.get('failed')}"
        )
        failed_names = {
            e.get("filename")
            for e in entries
            if e.get("status") in ("FAILED", "UNSUPPORTED", "CANCELLED")
        }
        self.retry_button.setEnabled(bool(failed_names))
        self.status_label.setText(f"Reopened batch {staging.name} ({len(entries)} entries).")

    def _show_queue(self) -> None:
        try:
            workspace = self._workspace()
        except DrillingIntelligenceError as exc:
            self.status_label.setText(f"Error: {exc}")
            return
        from ..cli.app import _workspace_row_id
        from ..documents.registry import DocumentRegistry
        from ..documents.repository import DocumentRepository
        from ..extraction.registry import build_default_router

        with workspace.database.read_only() as session:
            registry = DocumentRegistry(
                DocumentRepository(session),
                router=build_default_router(workspace.settings),
                settings=workspace.settings,
            )
            queue = review_queue(registry, workspace_id=_workspace_row_id(workspace))
        self.results_table.setRowCount(0)
        self.results_table.setColumnCount(4)
        self.results_table.setHorizontalHeaderLabels(
            ["Document version", "SHA-256", "Disposition", "Pending"]
        )
        for item in queue:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            for col, value in enumerate(
                [
                    item["document_version_id"],
                    item["sha256"][:16] + "…",
                    item["version_disposition"],
                    "yes" if item["pending_review"] else "no",
                ]
            ):
                self.results_table.setItem(row, col, QTableWidgetItem(str(value)))
        self.summary_label.setText(f"Review queue: {len(queue)} DDR document version(s).")
