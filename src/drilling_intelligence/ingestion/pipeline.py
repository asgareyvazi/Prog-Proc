"""End-to-end ingestion: scan → plan → register → extract → classify → index.

The pipeline owns no business rules itself; it sequences the components so that
the same code path serves the UI button, the CLI and the tests.  Sessions are
created here (not passed in) because this is the one service designed to run on
a worker thread while the UI stays responsive: SQLAlchemy sessions are not
thread-safe, so each run gets its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.enums import FileChangeKind, ProcessingStatus
from ..core.errors import DrillingIntelligenceError
from ..core.logging import get_logger
from ..database.session import Database
from ..documents.registry import DocumentRegistry, RegistrationResult
from ..documents.repository import DocumentRepository
from ..extraction.registry import build_default_router
from .planner import IngestionPlanner, ScanPlan
from .scanner import FileScanner, ScanResult

log = get_logger("ingestion.pipeline")


@dataclass
class PipelineResult:
    """Report of one ingestion run (persisted in ``ingestion_run``)."""

    root: str = ""
    run_id: str = ""
    workspace_id: str | None = None
    files_found: int = 0
    files_registered: int = 0
    files_extracted: int = 0
    from_cache: int = 0
    failures: int = 0
    duration_ms: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    results: list[RegistrationResult] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    indexed: int = 0
    #: Chunks written for those versions.  Zero alongside a successful registration means the
    #: artefact had nothing searchable in it, which is a different fact from "indexed".
    indexed_chunks: int = 0
    #: Indexed versions dropped because the registry no longer considers them current.
    index_removed: int = 0
    #: Snapshot of the index after this run (chunk/document counts), when one was wired in.
    index_stats: dict[str, Any] = field(default_factory=dict)
    #: Registry inconsistencies found after the run (see ``database.integrity``).  Reported,
    #: never raised: the files are already committed, and a warning the user can act on
    #: beats a failed run over a row a repair tool can fix.
    invariant_problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    def failures_report(self) -> list[RegistrationResult]:
        return [item for item in self.results if item.error]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "run_id": self.run_id,
            "files_found": self.files_found,
            "files_registered": self.files_registered,
            "files_extracted": self.files_extracted,
            "from_cache": self.from_cache,
            "failures": self.failures,
            "indexed": self.indexed,
            "indexed_chunks": self.indexed_chunks,
            "duration_ms": round(self.duration_ms, 1),
            "counts": dict(self.counts),
            "removed": list(self.removed[:100]),
            "skipped": list(self.skipped[:100]),
            "warnings": list(self.warnings),
            "error": self.error,
            "index_removed": self.index_removed,
            "index_stats": dict(self.index_stats),
            "results": [item.to_dict() for item in self.results[:2000]],
        }


class IngestionPipeline:
    """Wires the pieces together and records an auditable run."""

    def __init__(
        self,
        *,
        settings: Any,
        workspace_root: Path | str,
        database: Database | None = None,
        router: Any = None,
        index: Any = None,
        scanner: FileScanner | None = None,
        verify_invariants: bool = True,
    ) -> None:
        self.settings = settings
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.database = database or Database.for_workspace(self.workspace_root, settings)
        self.router = router
        self.index = index
        self.scanner = scanner
        self.verify_invariants = verify_invariants

    # -- scanner ------------------------------------------------------------
    def build_scanner(
        self,
        *,
        cancel: Callable[[], bool] | None = None,
        on_progress: Callable[[int, str], None] | None = None,
        extra_extensions: tuple[str, ...] = (),
    ) -> FileScanner:
        """Scanner configured from ``[ingestion]`` (never hardcoded)."""
        ingestion = getattr(self.settings, "ingestion", None)
        scanner = FileScanner()
        if ingestion is not None:
            scanner = replace(
                scanner,
                max_file_size_bytes=int(ingestion.max_file_size_mb) * 1024 * 1024,
                follow_symlinks=bool(ingestion.follow_symlinks),
                ignore_dir_names=tuple(ingestion.ignore_dir_names),
                ignore_file_patterns=tuple(ingestion.ignore_file_patterns),
                supported_extensions=tuple(ingestion.supported_extensions) + tuple(ext.lower() for ext in extra_extensions),
            )
        if cancel is not None:
            scanner = replace(scanner, cancel=cancel)
        if on_progress is not None:
            scanner = replace(scanner, on_progress=on_progress)
        return scanner

    # -- run ----------------------------------------------------------------
    def reconcile_index(self, repository: DocumentRepository) -> tuple[int, dict[str, Any]]:
        """Bring the searchable state in line with the registry after a run.

        Two things happen, both of them safe to repeat: chunks for versions that are no longer
        current (superseded, or rows whose document row has gone) leave the index, and the
        resulting counts come back for the run report.  This is *why* a re-run after an edit is
        not a duplicate answer - the superseded version is searchable no more, while its rows
        stay in the registry with their provenance.

        Returns ``(removed, stats)``.  A failure is reported as a warning by the caller rather
        than failing the run: the index is disposable, and losing a run because a sidecar could
        not be written would be backwards.
        """
        removed = int(self.index.prune_obsolete(repository=repository) or 0)
        try:
            stats = self.index.stats(repository=repository).to_dict()
        except Exception:  # noqa: BLE001 - statistics are a nicety
            stats = {}
        return removed, stats

    def check_invariants(self, repository: DocumentRepository) -> list[dict[str, Any]]:
        """Run the cross-row registry checks once, at the end of a pass.

        :meth:`DocumentRepository.create_version` already *writes* under the invariants
        (one current version per document, pointer matching, sequential numbers), so
        anything this finds is damage from elsewhere: an interrupted run, a hand-edited
        file, a database written by an older build.  Surfacing it here means the user sees
        it in the run report instead of in a wrong search result months later.
        """
        if not self.verify_invariants:
            return []
        try:
            return [problem.to_dict() for problem in repository.check_current_version_invariants()]
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail a good run
            log.warning("ingestion integrity check unavailable: %s", exc)
            return []

    def run(
        self,
        *,
        root: Path | str | None = None,
        workspace_id: str | None = None,
        well_id: str | None = None,
        project_id: str | None = None,
        force: bool = False,
        limit: int = 0,
        progress: Callable[[int, int, str], None] | None = None,
        cancel: Callable[[], bool] | None = None,
        extra_extensions: tuple[str, ...] = (),
    ) -> PipelineResult:
        from ..core.ids import new_id
        from ..database.models import IngestionRun

        scan_root = Path(root).expanduser().resolve() if root else self.workspace_root
        result = PipelineResult(root=str(scan_root), workspace_id=workspace_id)
        if not scan_root.exists():
            result.error = f"scan root does not exist: {scan_root}"
            result.counts = {"error": 1}
            return result

        started = time.perf_counter()
        session = self.database.session()
        repository = DocumentRepository(session)
        router = self.router or build_default_router(self.settings)
        registry = DocumentRegistry(repository, router=router, settings=self.settings)
        planner = IngestionPlanner(repository, settings=self.settings)
        run = IngestionRun(
            id=new_id("run"),
            workspace_id=workspace_id,
            root_path=str(scan_root),
            mode="forced" if force else "incremental",
            counts={},
            started_at=datetime.now(UTC),
        )
        result.run_id = run.id
        try:
            scanner = self.scanner or self.build_scanner(cancel=cancel, extra_extensions=extra_extensions)
            scan: ScanResult = scanner.scan(scan_root, extra_extensions=extra_extensions)
            result.files_found = len(scan.files)
            result.skipped = [{"path": path, "reason": reason} for path, reason in scan.skipped]
            result.warnings.extend(scan.warnings)
            candidates = scan.candidates
            files = candidates[:limit] if limit and limit > 0 else candidates
            deferred = candidates[len(files) :] if len(files) < len(candidates) else []
            plan: ScanPlan = planner.plan(
                workspace_id=workspace_id,
                files=files,
                root=str(scan_root),
                identity_root=self.workspace_root,
                force_reprocess=force,
                cancel=cancel,
                # A limit bounds the *work*, not the knowledge: the scanner saw the rest of the
                # folder, so the rest of the folder is present, not gone.
                seen_but_not_processed=deferred,
            )
            result.counts = plan.counts()
            result.removed = [item.to_dict() for item in plan.removed]
            session.add(run)

            work_items = [
                item
                for item in plan.items
                if item.needs_extraction or item.change in (FileChangeKind.NEW, FileChangeKind.MODIFIED, FileChangeKind.DUPLICATE)
            ]
            total = len(work_items)
            for index, planned in enumerate(work_items, start=1):
                if cancel is not None and cancel():
                    result.warnings.append("run cancelled by user")
                    break
                registration = registry.register(
                    path=planned.file.path,
                    workspace_root=self.workspace_root,
                    workspace_id=workspace_id,
                    change=planned.change,
                    well_id=well_id,
                    project_id=project_id,
                    document_id=planned.document_id,
                    carry_forward=planned.carry_forward,
                    duplicate_of=planned.duplicate_of,
                    precomputed_sha=planned.sha256,
                    force=force,
                )
                result.results.append(registration)
                if registration.error:
                    result.failures += 1
                else:
                    result.files_registered += 1
                    if registration.from_cache:
                        result.from_cache += 1
                    else:
                        result.files_extracted += 1
                    if self.index is not None and registration.version_id:
                        try:
                            # The repository of this run, not one the index opens for itself: the
                            # rows describing this file are still uncommitted in this session, so
                            # a second session would read "no artefact yet" and index nothing
                            # while reporting success.
                            chunks = int(self.index.upsert(registration.document_id, registration.version_id, repository=repository) or 0)
                            result.indexed += 1
                            result.indexed_chunks += chunks
                            if not chunks:
                                # Reported instead of swallowed: "no chunks" after a successful
                                # extraction means the artefact had nothing to index *or* the
                                # index read a snapshot that did not contain it yet.
                                result.warnings.append(f"index wrote no chunks for {registration.filename}")
                            repository.set_document_status(registration.document_id, ProcessingStatus.INDEXED)
                        except Exception as exc:  # noqa: BLE001 - indexing must never fail ingestion
                            result.warnings.append(f"index update failed for {registration.filename}: {type(exc).__name__}: {exc}")
                # Per-file commit: a crash loses at most the file in flight.
                session.commit()
                if progress is not None:
                    progress(index, total, planned.file.relative_path)

            result.counts["PROCESSED"] = len(result.results)
            result.invariant_problems = self.check_invariants(repository)
            if self.index is not None:
                result.index_removed, index_stats = self.reconcile_index(repository)
                if index_stats:
                    result.index_stats = index_stats
            for problem in result.invariant_problems[:20]:
                result.warnings.append(f"registry invariant broken: {problem['problem']} on {problem['table']}({problem['row_id']})")
            run.counts = dict(result.counts)
            run.report = {
                "removed": result.removed[:200],
                "skipped": result.skipped[:200],
                "warnings": result.warnings[:200],
                "failures": [item.to_dict() for item in result.failures_report()][:200],
                "invariant_problems": result.invariant_problems[:200],
            }
            run.finished_at = datetime.now(UTC)
            repository.audit(
                action="ingestion.run",
                subject_type="workspace",
                subject_id=workspace_id or "default",
                detail={
                    "run_id": run.id,
                    "root": str(scan_root),
                    "counts": dict(result.counts),
                    "files_found": result.files_found,
                    "registered": result.files_registered,
                    "extracted": result.files_extracted,
                    "cache_hits": result.from_cache,
                    "failures": result.failures,
                },
            )
            session.commit()
        except DrillingIntelligenceError as exc:
            session.rollback()
            run.error = str(exc)
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - report, never swallow
            session.rollback()
            result.error = f"{type(exc).__name__}: {exc}"
            log.error_event("ingestion.failed", root=str(scan_root), error=str(exc), exc_info=True)
        finally:
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                session.commit()
            except Exception:  # noqa: BLE001 - the run record is best-effort
                session.rollback()
            session.close()
        log.event(
            "ingestion.complete",
            level=30 if (result.error or result.failures) else 15,
            root=str(scan_root),
            found=result.files_found,
            registered=result.files_registered,
            extracted=result.files_extracted,
            cache_hits=result.from_cache,
            failures=result.failures,
            duration_ms=round(result.duration_ms, 1),
            error=result.error,
        )
        return result


__all__ = ["IngestionPipeline", "PipelineResult"]
