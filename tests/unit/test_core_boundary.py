"""The core boundary: units, lifecycles, vocabulary, provenance and the logger.

These are the behaviours that the type checker now holds to a strict profile
(see ``[tool.mypy]`` in ``pyproject.toml``). Each test below pins a decision
that the strict profile made visible, so the decision cannot be undone by a
refactor that only looks like a type change.
"""

from __future__ import annotations

import json
import logging

import pytest

from drilling_intelligence.core.enums import StrEnumLike
from drilling_intelligence.core.errors import UnitError, ValidationError
from drilling_intelligence.core.lifecycle import LIFECYCLES, Lifecycle
from drilling_intelligence.core.logging import PlatformLogger
from drilling_intelligence.core.provenance import PdfLocator, SourceLocator, TextLocator
from drilling_intelligence.core.units import Quantity
from drilling_intelligence.core.vocabulary import operation_type, problem_type


class TestQuantityFromDict:
    """A quantity dict is a number plus a unit, or it is refused."""

    def test_a_number_and_a_unit_round_trip(self) -> None:
        quantity = Quantity.from_dict({"value": 12.5, "unit": "m"})
        assert quantity.value == pytest.approx(12.5)
        assert quantity.unit.symbol == "m"

    def test_a_numeric_string_is_accepted(self) -> None:
        quantity = Quantity.from_dict({"value": "12.5", "unit": "m"})
        assert quantity.value == pytest.approx(12.5)

    def test_a_value_that_is_not_a_number_is_refused(self) -> None:
        # A boolean is not a number here: `True` is 1, and a depth of 1 metre
        # is not what a caller meant.
        with pytest.raises(UnitError):
            Quantity.from_dict({"value": True, "unit": "m"})

    def test_a_value_that_is_not_a_number_or_a_string_is_refused(self) -> None:
        with pytest.raises(UnitError):
            Quantity.from_dict({"value": {"n": 1}, "unit": "m"})

    def test_a_string_that_is_not_a_number_is_refused(self) -> None:
        with pytest.raises(UnitError):
            Quantity.from_dict({"value": "twelve", "unit": "m"})

    def test_a_missing_value_or_unit_is_refused(self) -> None:
        with pytest.raises(UnitError):
            Quantity.from_dict({"unit": "m"})
        with pytest.raises(UnitError):
            Quantity.from_dict({"value": 1.0})


class TestVocabularyPayload:
    """A vocabulary match is JSON data, and its values are not all text."""

    def test_the_recognised_flag_is_a_boolean_and_not_a_string(self) -> None:
        payload = operation_type("Tripping out").to_dict()
        assert payload["recognised"] is True
        assert payload.get("token") == "tripping"
        assert payload["raw"] == "Tripping out"

    def test_an_unrecognised_code_stays_visible_as_its_own_token(self) -> None:
        payload = problem_type("NPT-HOLE-ANGULAR").to_dict()
        assert payload["recognised"] is False
        assert payload.get("token") == "npt_hole_angular"

    def test_the_payload_survives_a_json_round_trip(self) -> None:
        payload = operation_type("Drilling").to_dict()
        assert json.loads(json.dumps(payload)) == payload


class TestLifecycleStates:
    """Every state enum is a ``StrEnumLike``, so ``parse`` always exists."""

    def test_every_lifecycle_uses_a_state_enum_that_can_parse(self) -> None:
        for name, machine in LIFECYCLES.items():
            assert issubclass(machine.states, StrEnumLike), name

    def test_an_unknown_state_is_refused_with_the_allowed_values(self) -> None:
        machine: Lifecycle = LIFECYCLES["risk_record"]
        with pytest.raises(ValidationError) as caught:
            machine.parse("not-a-state")
        assert "OPEN" in str(caught.value)

    def test_a_known_state_parses(self) -> None:
        machine: Lifecycle = LIFECYCLES["risk_record"]
        assert machine.parse("MITIGATED") is machine.states.MITIGATED


class TestLocatorPayload:
    """A locator dict is rebuilt into the locator class it names."""

    def test_a_pdf_locator_is_rebuilt_as_a_pdf_locator(self) -> None:
        locator = SourceLocator.from_dict({"kind": "pdf", "page": 3, "block": 1})
        assert isinstance(locator, PdfLocator)
        assert locator.page == 3
        assert locator.block == 1

    def test_an_unknown_kind_is_rebuilt_as_an_unknown_locator(self) -> None:
        locator = SourceLocator.from_dict({"kind": "hologram", "page": 1})
        assert not isinstance(locator, (PdfLocator, TextLocator))

    def test_a_field_the_class_does_not_have_is_dropped(self) -> None:
        locator = SourceLocator.from_dict({"kind": "text", "page": 3, "nonsense": "x"})
        assert isinstance(locator, TextLocator)
        assert not hasattr(locator, "nonsense")


class TestStructuredLogger:
    """A field named like an argument is data, not a second argument."""

    @staticmethod
    def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return list(caplog.records)

    def test_a_duration_field_in_a_warning_is_data(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = PlatformLogger(logging.getLogger("drilling_intelligence.test.warning"))
        with caplog.at_level(logging.WARNING, logger="drilling_intelligence.test.warning"):
            logger.warning_event("thing.happened", duration_ms=12.34, other=1)
        records = self._records(caplog)
        assert len(records) == 1
        assert records[0].duration_ms == pytest.approx(12.3)

    def test_a_duration_field_in_an_error_is_data(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = PlatformLogger(logging.getLogger("drilling_intelligence.test.error"))
        with caplog.at_level(logging.ERROR, logger="drilling_intelligence.test.error"):
            logger.error_event("thing.failed", duration_ms=12.34, exc_info=True)
        records = self._records(caplog)
        assert len(records) == 1
        assert records[0].duration_ms == pytest.approx(12.3)
        # ``exc_info=True`` asks the logging module to attach the current
        # exception; outside an ``except`` block that is an empty triple, but
        # it is not ``None``, so the flag reached the record.
        assert records[0].exc_info is not None

    def test_the_exc_info_flag_is_not_also_a_field(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = PlatformLogger(logging.getLogger("drilling_intelligence.test.excinfo"))
        with caplog.at_level(logging.ERROR, logger="drilling_intelligence.test.excinfo"):
            logger.error_event("thing.failed", exc_info=False, where="here")
        records = self._records(caplog)
        assert len(records) == 1
        assert "exc_info" not in records[0].msg
        assert records[0].where == "here"

    def test_an_event_without_a_duration_has_none(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = PlatformLogger(logging.getLogger("drilling_intelligence.test.plain"))
        with caplog.at_level(logging.WARNING, logger="drilling_intelligence.test.plain"):
            logger.warning_event("thing.happened", where="here")
        records = self._records(caplog)
        assert len(records) == 1
        assert not hasattr(records[0], "duration_ms")
