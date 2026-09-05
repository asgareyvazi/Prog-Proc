"""Typed application configuration (master spec section 62).

Rules the codebase follows:

*   No hardcoded paths, ports, model names or thresholds - everything is a
    setting with a documented default.
*   Secrets are never stored in config files; only the *name* of the
    environment variable that carries them is stored (``[ai.secrets]``).
*   Precedence: defaults < config file < environment overrides.  An override is
    recorded in ``settings.applied_overrides`` so the UI can show *why* a value
    looks wrong.
*   Unknown keys are reported, never silently dropped: a typo in a config file
    must not silently disable a safety setting.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError

ENV_PREFIX = "DRILLINTEL_"
#: Separator for nested keys in environment overrides: DRILLINTEL_AI__MODEL=x
ENV_NEST = "__"

CONFIG_FILENAME = "config.toml"
DEFAULT_CONFIG_DIRS = ("configs", ".")


# --------------------------------------------------------------------------- sections
@dataclass
class AppSettings:
    name: str = "Drilling Intelligence"
    environment: str = "development"
    #: Relative paths resolve against the workspace root (see Workspace).
    data_dir: str = ".drillintel"


@dataclass
class DatabaseSettings:
    url: str = ""
    echo_sql: bool = False
    sqlite_busy_timeout_ms: int = 5000
    #: Only used when ``url`` is empty: file name inside the workspace data dir.
    sqlite_filename: str = "drilling_intelligence.db"


@dataclass
class LoggingSettings:
    level: str = "INFO"
    file: str = ""
    format: str = "text"
    redact_keys: list[str] = field(default_factory=lambda: ["api_key", "token", "password", "secret", "authorization"])


@dataclass
class AiSettings:
    provider: str = "ollama"  # ollama | openai-compatible | none
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    temperature: float = 0.0
    max_context_tokens: int = 16384
    max_output_tokens: int = 2048
    timeout_seconds: float = 120.0
    stream: bool = True
    structured_output: bool = True
    retries: int = 2
    retry_backoff_seconds: float = 0.5
    require_ai: bool = False
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 0
    capability_probe: bool = True
    openai_endpoint: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    #: slot -> environment variable name holding the key (never the key itself)
    secrets: dict[str, str] = field(default_factory=lambda: {"openai_api_key": "OPENAI_API_KEY"})

    def api_key(self) -> str | None:
        """Resolve the optional cloud key from the environment, if configured."""
        slot = "openai_api_key"
        env_name = self.secrets.get(slot)
        if not env_name:
            return None
        value = os.environ.get(env_name, "").strip()
        return value or None


@dataclass
class MineruSettings:
    mode: str = "auto"  # auto | cli | http | disabled
    binary: str = "mineru"
    backend: str = "pipeline"
    endpoint: str = "http://127.0.0.1:8000"
    timeout_seconds: float = 900.0
    prefer_when_pages_above: int = 12
    prefer_when_table_rows_above: int = 25
    prefer_when_text_chars_per_page_below: int = 250
    #: Extra environment for the subprocess (e.g. a dedicated venv on PATH).
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractionSettings:
    """Limits that bound a single document's extraction.

    Every one of them is a *safety* limit, so the validator below rejects zero, negative
    and nonsensical values: ``pdf_max_pages = 0`` would silently extract an empty
    document that looks identical to a genuinely empty one, which is the worst possible
    failure mode for this product (master spec section 5).
    """

    pdf_max_pages: int = 4000
    pdf_extract_tables: bool = True
    pdf_min_table_rows: int = 2
    excel_max_sheets: int = 60
    #: Populated cells read per sheet.  When it is reached the extractor says so
    #: (``EXTRACTION_TRUNCATED: max_cells=...``) instead of quietly returning a partial
    #: sheet, so a missing value can never be read as an absent one.
    excel_max_cells: int = 60000
    #: Above this workbook size the *second* (formula) load is skipped: values and
    #: formulas cannot both come from one openpyxl load, and doubling peak memory for a
    #: bonus is a bad trade on a laptop with a year of DDRs in one file.
    excel_max_bytes: int = 64 * 1024 * 1024
    excel_read_formulas: bool = True
    excel_read_hidden: bool = True
    text_max_bytes: int = 8 * 1024 * 1024
    #: How many pages a PDF complexity probe inspects before deciding (routing input
    #: only - it must stay cheap even for a 600-page compilation).
    pdf_probe_pages: int = 12
    cache_enabled: bool = True


@dataclass
class IngestionSettings:
    max_file_size_mb: int = 512
    follow_symlinks: bool = False
    ignore_dir_names: list[str] = field(
        default_factory=lambda: [".git", ".drillintel", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"]
    )
    ignore_file_patterns: list[str] = field(default_factory=lambda: ["~$*", "*.tmp", "*.part", "*.lock", ".DS_Store", "Thumbs.db"])
    supported_extensions: list[str] = field(
        default_factory=lambda: [".pdf", ".xlsx", ".xlsm", ".docx", ".txt", ".md", ".csv", ".tsv"]
    )
    hash_chunk_bytes: int = 1048576


@dataclass
class SearchSettings:
    vector_store: str = "auto"  # auto | sqlite-vec | memory | none
    keyword_results: int = 40
    semantic_results: int = 40
    hybrid_results: int = 20
    embedding_cache: bool = True


@dataclass
class AuthoritySettings:
    """Configurable source-authority ladder (sections 19 and 83)."""

    hierarchy: list[str] = field(
        default_factory=lambda: [
            "approved_drilling_program",
            "approved_engineering_document",
            "current_operational_report",
            "current_program_revision",
            "previous_revision",
            "historical_report",
            "technical_reference",
            "general_knowledge",
        ]
    )
    #: Never invent a value: when authority is equal the conflict stays open.
    resolve_on_equal_authority: str = "report_conflict"

    def rank(self, tier: str) -> int:
        """Lower is stronger; unknown tiers rank last."""
        try:
            return self.hierarchy.index(str(tier))
        except ValueError:
            return len(self.hierarchy)


@dataclass
class UiSettings:
    window_width: int = 1500
    window_height: int = 900
    theme: str = "dark"
    show_source_panel: bool = True
    progress_interval_ms: int = 100
    #: When false the UI will not attempt any AI call (offline demo mode).
    enable_ai_panel: bool = True


# --------------------------------------------------------------------------- container
@dataclass
class Settings:
    app: AppSettings = field(default_factory=AppSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    ai: AiSettings = field(default_factory=AiSettings)
    mineru: MineruSettings = field(default_factory=MineruSettings)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    authority: AuthoritySettings = field(default_factory=AuthoritySettings)
    ui: UiSettings = field(default_factory=UiSettings)
    #: File the values were loaded from (``None`` for pure defaults).
    source_path: str = ""
    unknown_keys: list[str] = field(default_factory=list)
    applied_overrides: list[str] = field(default_factory=list)

    # -- loading ------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None, *, apply_env: bool = True) -> Settings:
        raw: dict[str, Any] = {}
        resolved = cls.discover(path)
        settings = cls()
        if resolved is not None:
            try:
                with resolved.open("rb") as handle:
                    raw = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigurationError(f"Malformed TOML in {resolved}: {exc}") from exc
            settings.source_path = str(resolved)
        settings._apply(raw)
        if apply_env:
            settings.apply_env(os.environ)
        settings.validate()
        return settings

    @staticmethod
    def discover(path: str | Path | None = None) -> Path | None:
        """Explicit path > ``$DRILLINTEL_CONFIG`` > ./configs/development.toml > ./config.toml."""
        if path:
            candidate = Path(path).expanduser()
            if not candidate.exists():
                raise ConfigurationError(f"Config file not found: {candidate}", path=str(candidate))
            return candidate
        from_env = os.environ.get("DRILLINTEL_CONFIG", "").strip()
        if from_env:
            candidate = Path(from_env).expanduser()
            if not candidate.exists():
                raise ConfigurationError(f"$DRILLINTEL_CONFIG points at a missing file: {candidate}", path=str(candidate))
            return candidate
        for root in (Path.cwd(), *Path.cwd().parents):
            for folder in DEFAULT_CONFIG_DIRS:
                candidate = root / folder / CONFIG_FILENAME
                if candidate.exists():
                    return candidate
            if (root / "configs" / "development.toml").exists():
                return root / "configs" / "development.toml"
            if (root / "pyproject.toml").exists():
                break
        return None

    def _apply(self, raw: dict[str, Any]) -> None:
        for name, section in raw.items():
            target = getattr(self, name, None)
            if target is None or not is_dataclass(target):
                self.unknown_keys.append(str(name))
                continue
            if not isinstance(section, dict):
                raise ConfigurationError(f"Section [{name}] must be a table, got {type(section).__name__}")
            _apply_section(target, section, f"{name}", self.unknown_keys)

    def apply_env(self, environ: dict[str, str] | None = None) -> None:
        environ = environ if environ is not None else dict(os.environ)
        flat = _flatten(self)
        for env_key, value in environ.items():
            if not env_key.startswith(ENV_PREFIX) or env_key in {"DRILLINTEL_CONFIG"}:
                continue
            dotted = env_key[len(ENV_PREFIX) :].lower().replace(ENV_NEST, ".").rstrip(".")
            if not dotted:
                continue
            if dotted not in flat:
                # tolerate single-underscore style: AI__MODEL vs AI_MODEL
                alt = env_key[len(ENV_PREFIX) :].lower()
                match = next((k for k in flat if k.replace(".", "_") == alt), None)
                if match is None:
                    self.unknown_keys.append(f"env:{env_key}")
                    continue
                dotted = match
            self._set_path(dotted, _coerce_scalar(value, type(flat[dotted])))
            self.applied_overrides.append(f"env:{env_key}")

    def _set_path(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        obj: Any = self
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)

    # -- validation ---------------------------------------------------------
    def validate(self) -> None:
        """Reject settings that would silently corrupt extraction or burn retries.

        This is a table of range checks, not a validation framework: a config value that
        is out of range is a *typo with consequences*, and the consequence here is
        usually an empty or truncated document being stored as if it were complete.  So
        the check is loud and happens at load time, naming the dotted key the user has to
        edit (``[extraction] excel_max_cells``), and it reports every problem at once
        rather than one per edit-and-restart cycle.
        """
        flat = _flatten(self)
        problems: list[str] = []
        for dotted, minimum in _MINIMUMS:
            value = flat.get(dotted)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue  # a bool or a string here is a type error, reported by _coerce
            if value < minimum:
                problems.append(f"{dotted} must be >= {minimum}, got {value!r}")
        for dotted, low, high in _RANGES:
            value = flat.get(dotted)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and not low <= value <= high:
                problems.append(f"{dotted} must be between {low} and {high}, got {value!r}")
        for dotted, allowed in _CHOICES.items():
            value = flat.get(dotted)
            if value is None:
                continue
            text_value = str(value).strip().lower()
            if text_value not in allowed:
                problems.append(f"{dotted} must be one of {', '.join(sorted(allowed))}, got {value!r}")
        # Lists that must not be empty or duplicated: the authority ladder decides
        # conflicts, so a gap or a repeat there is a correctness bug, not a style issue.
        hierarchy = list(self.authority.hierarchy or [])
        if not hierarchy:
            problems.append("authority.hierarchy must list at least one tier")
        duplicates = sorted({tier for tier in hierarchy if hierarchy.count(tier) > 1})
        if duplicates:
            problems.append(f"authority.hierarchy repeats tiers: {', '.join(duplicates)}")
        if problems:
            raise ConfigurationError(
                "invalid configuration: " + "; ".join(problems),
                problems=problems,
                config_path=self.source_path or "(defaults and environment)",
            )

    # -- derived ------------------------------------------------------------
    def data_dir_for(self, workspace_root: Path) -> Path:
        raw = self.app.data_dir or ".drillintel"
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path(workspace_root) / candidate
        return candidate

    def database_url_for(self, workspace_root: Path) -> str:
        """Absolute SQLAlchemy URL.  SQLite is *initial* only - the domain layer
        must not depend on it (see docs/DECISIONS.md ADR-0004)."""
        if self.database.url:
            return _expand_env_in_url(self.database.url, workspace_root)
        # Same location the Workspace uses: the registry always lives in the
        # workspace's data directory under ``database/``, so an opened folder and a
        # config-driven connection can never disagree about which file is the record.
        db_path = self.data_dir_for(workspace_root) / "database" / self.database.sqlite_filename
        return f"sqlite:///{db_path.as_posix()}"

    def to_toml(self) -> str:
        lines: list[str] = [
            "# Generated by Drilling Intelligence. Sections mirror Settings dataclasses.",
            "",
        ]
        for section_field in fields(self):
            if section_field.name in {"source_path", "unknown_keys", "applied_overrides"}:
                continue
            section = getattr(self, section_field.name)
            lines.append(f"[{section_field.name}]")
            for f in fields(section):
                value = getattr(section, f.name)
                lines.append(f"{f.name} = {_toml_value(value)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def save(self, path: str | Path) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_toml(), encoding="utf-8")
        return target

    def summary(self) -> dict[str, Any]:
        """Non-secret overview for the Settings panel and ``drillintel doctor``."""
        return {
            "source_path": self.source_path or "(defaults only)",
            "environment": self.app.environment,
            "database": "explicit url" if self.database.url else f"sqlite in workspace ({self.database.sqlite_filename})",
            "ai": {
                "provider": self.ai.provider,
                "endpoint": self.ai.endpoint,
                "model": self.ai.model,
                "embedding_model": self.ai.embedding_model,
                "temperature": self.ai.temperature,
                "timeout_seconds": self.ai.timeout_seconds,
                "require_ai": self.ai.require_ai,
                "api_key_source": ("env:" + self.ai.secrets.get("openai_api_key", "-")) if self.ai.provider == "openai-compatible" else "n/a",
            },
            "mineru": {"mode": self.mineru.mode, "binary": self.mineru.binary, "backend": self.mineru.backend},
            "authority_hierarchy": list(self.authority.hierarchy),
            "overrides": list(self.applied_overrides),
            "unknown_keys": list(self.unknown_keys),
        }


# --------------------------------------------------------------------------- helpers
def _apply_section(target: Any, values: dict[str, Any], prefix: str, unknown: list[str]) -> None:
    spec = {f.name: f for f in fields(target)}
    for key, value in values.items():
        f = spec.get(key)
        if f is None:
            unknown.append(f"{prefix}.{key}")
            continue
        current = getattr(target, f.name)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_section(current, value, f"{prefix}.{key}", unknown)
            continue
        setattr(target, f.name, _coerce(value, f, current))


def _coerce(value: Any, f: Any, current: Any) -> Any:
    if value is None:
        return current
    expected = f.type if isinstance(f.type, type) else None
    default = f.default if f.default is not MISSING else current
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError as exc:
                raise ConfigurationError(f"{f.name}: expected an integer, got {value!r}") from exc
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return list(value)
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ConfigurationError(f"{f.name}: expected a table, got {type(value).__name__}")
        return {str(k): v for k, v in value.items()}
    if isinstance(value, (dict, list)):
        raise ConfigurationError(f"{f.name}: expected a scalar, got {type(value).__name__}")
    if expected in (int, float, bool, str) and isinstance(value, expected):
        return value
    return str(value)


def _coerce_scalar(value: str, like: type | object) -> Any:
    if isinstance(like, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    if isinstance(like, list):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        key = f"{prefix}{f.name}"
        if is_dataclass(value):
            out.update(_flatten(value, f"{key}."))
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- validation table
#: ``>= 0`` for things that are allowed to be zero (a zero backoff, a zero probe
#: threshold), and ``>= 1`` for limits where zero or less means "produce nothing" -
#: which must never be confused with "there was nothing there".
_MINIMUMS: tuple[tuple[str, float], ...] = (
    ("database.sqlite_busy_timeout_ms", 0),
    ("ai.timeout_seconds", 0.001),
    ("ai.retries", 0),
    ("ai.retry_backoff_seconds", 0),
    ("ai.max_context_tokens", 1),
    ("ai.max_output_tokens", 1),
    ("ai.embedding_dimensions", 0),
    ("mineru.timeout_seconds", 0.001),
    ("mineru.prefer_when_pages_above", 0),
    ("mineru.prefer_when_table_rows_above", 0),
    ("mineru.prefer_when_text_chars_per_page_below", 0),
    ("extraction.pdf_max_pages", 1),
    ("extraction.pdf_min_table_rows", 1),
    ("extraction.excel_max_sheets", 1),
    ("extraction.excel_max_cells", 1),
    ("extraction.excel_max_bytes", 1),
    ("extraction.text_max_bytes", 1),
    ("extraction.pdf_probe_pages", 1),
    ("ingestion.max_file_size_mb", 1),
    ("ingestion.hash_chunk_bytes", 1),
    ("search.keyword_results", 1),
    ("search.semantic_results", 1),
    ("search.hybrid_results", 1),
    ("ui.window_width", 100),
    ("ui.window_height", 100),
    ("ui.progress_interval_ms", 1),
)

#: Values that are only meaningful inside a band.  Temperature above 2 makes the
#: structured extraction wander; a percentage outside 0-100 is a unit mistake.
_RANGES: tuple[tuple[str, float, float], ...] = (
    ("ai.temperature", 0.0, 2.0),
    ("mineru.prefer_when_text_chars_per_page_below", 0, 100000),
)

#: Enumerated switches.  Compared case-insensitively, because ``Mode = "CLI"`` in a TOML
#: file is a person typing what the README said, not a request to disable the parser.
_CHOICES: dict[str, frozenset[str]] = {
    "ai.provider": frozenset({"ollama", "openai-compatible", "none"}),
    "mineru.mode": frozenset({"auto", "cli", "http", "disabled", "off", "none"}),
    "mineru.backend": frozenset({"pipeline", "hybrid", "hybrid-engine", "vlm-engine", "vlm-transformers", "vlm-vllm-engine", "omniparse", "olmocr"}),
    "search.vector_store": frozenset({"auto", "sqlite-vec", "memory", "none"}),
    "logging.level": frozenset({"debug", "info", "warning", "error", "critical"}),
    "logging.format": frozenset({"text", "json"}),
    "ui.theme": frozenset({"dark", "light", "system"}),
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _expand_env_in_url(url: str, workspace_root: Path) -> str:
    if url.startswith("sqlite:///") and "~" in url:
        return "sqlite:///" + Path(url[len("sqlite:///") :]).expanduser().as_posix()
    if url == "sqlite://" or url.startswith("sqlite:?"):
        db_path = Path(workspace_root) / ".drillintel" / "drilling_intelligence.db"
        return f"sqlite:///{db_path.as_posix()}"
    return url


__all__ = [
    "AiSettings",
    "AppSettings",
    "AuthoritySettings",
    "DatabaseSettings",
    "ExtractionSettings",
    "IngestionSettings",
    "LoggingSettings",
    "MineruSettings",
    "SearchSettings",
    "Settings",
    "UiSettings",
]
