"""Workspace: the unit of work the user actually interacts with.

A workspace is a folder on disk that contains:

    <root>/workspace.toml          <- marker + project metadata (name, corpus dirs)
    <root>/.drillintel/            <- data dir (sqlite db, caches, exports) [configurable]

Why a folder and not just a database: drilling work is folder-shaped
("WellX-2026/programs", ".../ddr"), the platform must work on the data where it
already lives, and a workspace is what you copy to a USB drive or hand to a
successor.  The database stays behind the repository layer, so moving to a
PostgreSQL server changes the URL, not the product.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import Settings
from ..core.errors import WorkspaceError
from ..core.hashing import identity_slug
from ..core.logging import configure_logging, get_logger
from ..database.engine import build_engine
from ..database.migrations import MigrationStatus, ensure_schema
from ..database.session import Database

WORKSPACE_MARKER = "workspace.toml"
log = get_logger("wells.workspace")


@dataclass
class WorkspaceConfig:
    """Contents of ``workspace.toml``."""

    name: str = ""
    description: str = ""
    #: Folders inside the workspace that are scanned for documents.
    corpus_dirs: list[str] = field(default_factory=lambda: ["corpus"])
    #: Extra ignore patterns specific to this project.
    ignore_patterns: list[str] = field(default_factory=list)
    project_code: str = ""
    created_by: str = ""
    #: System-of-record database, relative to the workspace data directory (an
    #: absolute path is used as written).  Stored here rather than in the global
    #: config because each workspace is a self-contained project.
    database_path: str = "database/drilling_intelligence.db"
    #: Rebuildable keyword/vector index sidecar (never a source of truth).
    index_database_path: str = "index/search_index.db"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_toml(self) -> str:
        lines = [
            "# Drilling Intelligence workspace descriptor.",
            "# Safe to edit by hand; the platform will not rewrite it implicitly.",
            f'name = "{self.name}"',
            f'description = "{self.description}"',
            f'project_code = "{self.project_code}"',
            f'created_by = "{self.created_by}"',
            f'database_path = "{self.database_path}"',
            f'index_database_path = "{self.index_database_path}"',
            "corpus_dirs = [" + ", ".join(f'"{d}"' for d in self.corpus_dirs) + "]",
            "ignore_patterns = [" + ", ".join(f'"{p}"' for p in self.ignore_patterns) + "]",
        ]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_path(cls, path: Path) -> WorkspaceConfig:
        try:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
        except FileNotFoundError:
            return cls()
        except tomllib.TOMLDecodeError as exc:
            raise WorkspaceError(f"workspace.toml is not valid TOML: {exc}", path=str(path)) from exc
        known = set(cls.__dataclass_fields__)
        values = {key: value for key, value in payload.items() if key in known}
        extra = {key: value for key, value in payload.items() if key not in known}
        return cls(**values, extra=extra)


class Workspace:
    """An opened workspace: settings, data dirs, engine and services handle."""

    def __init__(self, root: Path, settings: Settings, config: WorkspaceConfig, *, migration: MigrationStatus | None = None) -> None:
        self.root = Path(root)
        self.settings = settings
        self.config = config
        self.data_dir = settings.data_dir_for(self.root)
        self.cache_dir = self.data_dir / "cache"
        self.exports_dir = self.data_dir / "exports"
        self.index_dir = self.data_dir / "index"
        self.logs_dir = self.data_dir / "logs"
        self.migration = migration
        self._database: Database | None = None
        self._index_database: Database | None = None

    # -- construction -------------------------------------------------------
    @classmethod
    def open(cls, root: Path | str, settings: Settings | None = None, *, create: bool = False) -> Workspace:
        path = Path(root).expanduser().resolve()
        if not path.exists():
            if not create:
                raise WorkspaceError(f"workspace does not exist: {path}", hint="create it with `drillintel workspace create` or pass create=True")
            return cls.create(path, settings)
        if not path.is_dir():
            raise WorkspaceError(f"workspace path is not a directory: {path}")
        marker = path / WORKSPACE_MARKER
        if not marker.exists() and not create:
            raise WorkspaceError(
                f"{path} is not a Drilling Intelligence workspace (no {WORKSPACE_MARKER})",
                hint="run `drillintel workspace create <path>` to initialise it",
            )
        settings = settings or Settings.load()
        config = WorkspaceConfig.from_path(marker) if marker.exists() else WorkspaceConfig(name=path.name)
        if not config.name:
            config.name = path.name
        workspace = cls(path, settings, config)
        if create:
            workspace.ensure_layout()
            if not marker.exists():
                workspace.write_marker()
        return workspace

    @classmethod
    def create(cls, root: Path | str, settings: Settings | None = None, *, name: str = "", description: str = "", corpus_dirs: list[str] | None = None) -> Workspace:
        path = Path(root).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise WorkspaceError(f"workspace is not writable: {path}")
        settings = settings or Settings.load()
        config = WorkspaceConfig(
            name=name or identity_slug(path.name) or path.name,
            description=description,
            corpus_dirs=corpus_dirs or ["corpus"],
            created_by=os.environ.get("USER", os.environ.get("USERNAME", "")) or "",
        )
        workspace = cls(path, settings, config)
        workspace.ensure_layout()
        workspace.write_marker()
        return workspace

    # -- layout -------------------------------------------------------------
    def ensure_layout(self) -> None:
        for directory in (self.data_dir, self.cache_dir, self.exports_dir, self.index_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        for corpus in self.config.corpus_dirs:
            target = self.root / corpus
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)

    def write_marker(self) -> None:
        (self.root / WORKSPACE_MARKER).write_text(self.config.to_toml(), encoding="utf-8")

    @property
    def is_new(self) -> bool:
        return not (self.root / WORKSPACE_MARKER).exists()

    # -- data locations -----------------------------------------------------
    def _resolve(self, relative: str, default: str) -> Path:
        """Resolve a workspace.toml data path (``data/...`` means inside the data dir)."""
        value = (relative or "").strip() or default
        path = Path(value)
        if path.is_absolute():
            return path
        if path.parts[:1] == ("data",) and len(path.parts) > 1:
            path = Path(*path.parts[1:])
        return self.data_dir / path

    @property
    def database_path(self) -> Path:
        """Absolute path of the system-of-record database (workspace-bound)."""
        return self._resolve(self.config.database_path, "database/drilling_intelligence.db")

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the registry, so a workspace is self-contained."""
        self.ensure_layout()
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def index_database_path(self) -> Path:
        return self._resolve(self.config.index_database_path, "index/search_index.db")

    @property
    def index_database_url(self) -> str:
        self.ensure_layout()
        return f"sqlite:///{self.index_database_path.as_posix()}"

    # -- infrastructure -----------------------------------------------------
    def configure_logging(self) -> None:
        settings = self.settings.logging
        target = settings.file or str(self.logs_dir / "platform.log")
        configure_logging(
            settings.level,
            file=target if settings.file else None,
            format=settings.format,
            sensitive_keys=tuple(settings.redact_keys),
        )

    @property
    def database(self) -> Database:
        if self._database is None:
            # The workspace owns its registry: opening a project folder must never
            # depend on where the global config points.
            engine = build_engine(self.settings, self.database_url)
            self._database = Database(engine, settings=self.settings)
            self.migration = ensure_schema(engine)
            log.event(
                "workspace.opened",
                root=str(self.root),
                db=str(engine.url),
                schema_mode=self.migration.mode,
                revision=self.migration.current,
            )
        return self._database

    @property
    def index_database(self) -> Database:
        """Sidecar database for derived indexes (FTS5 keyword + vectors).

        Keeping derived structures out of the system of record means:
        the schema stays dialect-portable, and an index can be rebuilt or moved
        without touching engineering records.
        """
        if self._index_database is None:
            engine = build_engine(self.settings, self.index_database_url)
            self._index_database = Database(engine, settings=self.settings)
        return self._index_database

    def close(self) -> None:
        if self._database is not None:
            self._database.dispose()
            self._database = None
        if self._index_database is not None:
            self._index_database.dispose()
            self._index_database = None

    # -- presentation -------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "database_path": str(self.database_path),
            "root": str(self.root),
            "data_dir": str(self.data_dir),
            "corpus_dirs": list(self.config.corpus_dirs),
            "project_code": self.config.project_code,
            "database": str(self._database.url) if self._database else "(not opened)",
            "schema": (self.migration.to_dict() if self.migration else None),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Workspace(root={str(self.root)!r}, name={self.config.name!r})"


__all__ = ["WORKSPACE_MARKER", "Workspace", "WorkspaceConfig"]
