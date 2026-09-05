"""Units, numeric conventions and the constants the engineering tools rely on."""

from __future__ import annotations

import pytest

from drilling_intelligence.core.errors import DimensionMismatchError, UnitError, UnknownUnitError
from drilling_intelligence.core.units import (
    BBL_M3,
    FIELD_DEFAULT_UNITS,
    FT_M,
    GAL_US_M3,
    HYDROSTATIC_K_PSI_PER_FT_PER_PPG,
    Dimension,
    Quantity,
    convert,
    convertible,
    default_unit,
    format_number,
    known_units,
    mud_weight_equivalent,
    parse_decimal,
    resolve_unit,
)


def test_field_constants_are_the_ones_the_calculations_use() -> None:
    assert pytest.approx(0.052, abs=0.0) == HYDROSTATIC_K_PSI_PER_FT_PER_PPG  # industry constant
    assert pytest.approx(42 * GAL_US_M3, rel=1e-12) == BBL_M3
    assert pytest.approx(0.3048, abs=0.0) == FT_M
    assert convert(42, "gal", "m3") == pytest.approx(BBL_M3, rel=1e-12)


def test_hydrostatic_convention_sg_ppg() -> None:
    # 1 SG = 8.3454 ppg because ppg is lb per US gallon while SG assumes 1000 kg/m3.
    assert convert(1.0, "sg", "ppg") == pytest.approx(8.3454044, abs=1e-6)
    assert Quantity.of(1.05, "sg").to("ppg").value == pytest.approx(8.7626747, abs=1e-6)
    assert mud_weight_equivalent(Quantity.of(1.05, "sg")).unit.symbol == "ppg"


def test_density_and_mud_weight_are_separate_dimensions_but_convertible() -> None:
    assert convertible(Dimension.DENSITY, Dimension.MUD_WEIGHT)
    assert not convertible(Dimension.MUD_WEIGHT, Dimension.PRESSURE)
    assert not convertible(Dimension.LENGTH, Dimension.VOLUME)
    # A gradient is pressure over length, never a density: 15.8 ppg != 0.865 psi/ft.
    assert not convertible("ppg", "psi/ft")


def test_parse_accepts_both_separator_conventions() -> None:
    assert parse_decimal("1,234.5") == pytest.approx(1234.5)
    assert parse_decimal("1.234,56") == pytest.approx(1234.56)
    assert parse_decimal("10,2") == pytest.approx(10.2)  # decimal comma, not thousands
    assert parse_decimal("1,234,567") == pytest.approx(1234567)
    assert Quantity.parse("1,234.5 ft").value == pytest.approx(1234.5)
    assert Quantity.parse("10,2 sg").value == pytest.approx(10.2)
    assert Quantity.parse("1.234,56 kg/l").value == pytest.approx(1234.56)
    assert Quantity.parse("1.5e3 ft").value == pytest.approx(1500.0)


def test_parse_handles_nominal_fractional_sizes() -> None:
    assert Quantity.parse('12 1/4"').value == pytest.approx(12.25)
    assert Quantity.parse("9-5/8 in").value == pytest.approx(9.625)
    assert Quantity.parse("12 1/4 in").to("mm").value == pytest.approx(311.15, abs=0.01)


def test_quantities_refuse_silent_unit_mixing() -> None:
    with pytest.raises(DimensionMismatchError):
        Quantity.of(10.2, "ppg") + Quantity.of(9000, "ft")
    with pytest.raises(UnknownUnitError):
        resolve_unit("banana")
    with pytest.raises(UnitError):
        Quantity.parse("no number here")
    with pytest.raises(UnitError):
        Quantity.parse("")
    assert Quantity.parse(10.2, default_unit="ppg").unit.symbol == "ppg"


def test_dimensions_can_be_restated_in_the_field_default_unit() -> None:
    # Depth arrives in metres from a European report but is displayed in feet, because
    # that is the unit the well files and the program use.
    depth = Quantity.of(3048.0, "m").as_dimension(Dimension.LENGTH)
    assert depth.unit.symbol == default_unit(Dimension.LENGTH).symbol == "ft"
    assert depth.value == pytest.approx(10000.0, rel=1e-9)
    assert resolve_unit(FIELD_DEFAULT_UNITS[Dimension.MUD_WEIGHT]).symbol == "ppg"


def test_unit_registry_is_interned_and_reportable() -> None:
    assert resolve_unit("PPG") is resolve_unit("ppg")
    assert "ppg" in known_units() and "bbl" in known_units()
    assert resolve_unit("lb/gal") is resolve_unit("ppg")
    assert format_number(1234.56789) == "1234.57"
