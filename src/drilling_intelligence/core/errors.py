"""Exception hierarchy.

Every failure mode the product must communicate to a user or to the AI layer is
represented by a dedicated exception carrying a machine-readable ``code``.  The
UI and the CLI present ``code`` + ``hint`` instead of swallowing errors.
"""

from __future__ import annotations

from typing import Any


class DrillingIntelligenceError(Exception):
    """Base class for all platform errors."""

    code = "ERROR"
    #: What the operator/user can do about it.  Surfaced verbatim in the UI.
    hint = ""

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.__doc__)
        self.message = message or (self.__doc__ or "")
        self.context: dict[str, Any] = context

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": type(self).__name__, "code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.context:
            payload["context"] = self.context
        return payload

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = self.message or self.code
        if self.context:
            detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
            return f"{base} [{detail}]"
        return base


# --------------------------------------------------------------------------- config
class ConfigurationError(DrillingIntelligenceError):
    code = "CONFIGURATION"
    hint = "Fix configs/<environment>.toml or the corresponding DRILLINTEL_* environment variable."


class WorkspaceError(DrillingIntelligenceError):
    code = "WORKSPACE"
    hint = "Create or select a workspace directory (it must be writable)."


# --------------------------------------------------------------------------- data
class UnitError(DrillingIntelligenceError):
    """Raised instead of performing an ambiguous or silent unit conversion."""

    code = "UNIT"
    hint = "State the unit explicitly; the platform never guesses a unit."


class UnknownUnitError(UnitError):
    code = "UNIT_UNKNOWN"


class DimensionMismatchError(UnitError):
    code = "UNIT_DIMENSION"


class ValidationError(DrillingIntelligenceError):
    code = "VALIDATION"


class ProvenanceError(DrillingIntelligenceError):
    """A provenance reference could not be resolved or verified."""

    code = "PROVENANCE"
    hint = "Re-extract the document; the source file may have changed since indexing."


class ConflictError(DrillingIntelligenceError):
    """Multiple sources disagree and no authority rule resolves it."""

    code = "CONFLICT"
    hint = "Review both sources and record which one governs; the platform will not choose for you."


# --------------------------------------------------------------------------- documents
class ScannerError(DrillingIntelligenceError):
    code = "SCANNER"


class ExtractionError(DrillingIntelligenceError):
    code = "EXTRACTION"


class ParserUnavailableError(ExtractionError):
    """A parser that is optional in this environment cannot be used."""

    code = "PARSER_UNAVAILABLE"
    hint = "Install/enable the parser in the config, or let the router use a fallback extractor."


class ClassificationError(DrillingIntelligenceError):
    code = "CLASSIFICATION"


class UnsupportedFormatError(ExtractionError):
    code = "UNSUPPORTED_FORMAT"


# --------------------------------------------------------------------------- engineering
class CalculationInputError(DrillingIntelligenceError):
    """Required inputs missing or unusable - the platform must say so, not guess."""

    code = "CALC_INPUT_MISSING"
    hint = "Provide the missing inputs from a cited source, or record them as USER_PROVIDED."


class CalculationValidationError(DrillingIntelligenceError):
    code = "CALC_VALIDATION"


class MethodNotFoundError(DrillingIntelligenceError):
    code = "METHOD_NOT_FOUND"
    hint = "List available methods with `drillintel methods`."


# --------------------------------------------------------------------------- ai
class ProviderUnavailableError(DrillingIntelligenceError):
    code = "AI_UNAVAILABLE"
    hint = "Start Ollama (`ollama serve`) and pull the configured model, or set ai.provider = 'none'."


class ProviderProtocolError(DrillingIntelligenceError):
    """The provider answered, but not with something we can trust."""

    code = "AI_PROTOCOL"


class StructuredOutputError(ProviderProtocolError):
    code = "AI_STRUCTURED_OUTPUT"


class UnverifiedClaimError(DrillingIntelligenceError):
    """A claim cannot be tied to evidence; the platform refuses to present it as fact."""

    code = "UNVERIFIED_CLAIM"


# --------------------------------------------------------------------------- integrations
class IntegrationUnavailableError(DrillingIntelligenceError):
    code = "INTEGRATION_UNAVAILABLE"

    def __init__(self, component: str, reason: str, **context: Any) -> None:
        super().__init__(f"{component}: {reason}", component=component, **context)


__all__ = [
    "CalculationInputError",
    "CalculationValidationError",
    "ClassificationError",
    "ConfigurationError",
    "ConflictError",
    "DimensionMismatchError",
    "DrillingIntelligenceError",
    "ExtractionError",
    "IntegrationUnavailableError",
    "MethodNotFoundError",
    "ParserUnavailableError",
    "ProvenanceError",
    "ProviderProtocolError",
    "ProviderUnavailableError",
    "ScannerError",
    "StructuredOutputError",
    "UnitError",
    "UnknownUnitError",
    "UnsupportedFormatError",
    "UnverifiedClaimError",
    "ValidationError",
    "WorkspaceError",
]
