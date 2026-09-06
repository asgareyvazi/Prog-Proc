"""Unit-aware engineering values (master spec section 26).

Design rules, enforced by this module:

*   Every engineering value carries its unit.  A bare float is only accepted
    where the dimension is unambiguous by contract (e.g. a count).
*   Conversions are explicit (``Quantity.to``).  There is no implicit
    coercion in arithmetic: mixing ``ft`` and ``m`` raises
    :class:`DimensionMismatchError` instead of silently producing garbage.
*   SI is the internal canonical system; field units (ft, psi, ppg, bbl) are
    first-class so that inputs stay in the unit the source used - provenance
    must be able to echo back exactly what the document said.

Conversion factors are exact definitions where a definition exists (ft, in,
gal(US), bbl=42 gal, lbf, psi, ...).  Documented conventions:

*   ``SG`` (specific gravity) uses water density = 1000.0 kg/m3, which is the
    drilling-field convention (not 999.0 kg/m3 at 15 degC).
*   ``ppg`` is pounds (avoirdupois) per US gallon.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import DimensionMismatchError, UnitError, UnknownUnitError

# --------------------------------------------------------------------------- constants
#: Exact definitions (NIST SP 811).
FT_M = 0.3048
IN_M = 0.0254
GAL_US_M3 = 0.003785411784
BBL_M3 = 42.0 * GAL_US_M3  # 0.158987294928 m3
LB_KG = 0.45359237
LBF_N = 4.4482216152605
PSI_PA = 6894.757293168
KGF_N = 9.80665
G0 = 9.80665  # standard gravity, m/s2

#: Field constant: hydrostatic pressure, psi = 0.052 * ppg * ft.
#: The exact physical value is 0.05194; 0.052 is the industry rounding and is
#: what a drilling engineer will reproduce by hand, so it is what we use.
#: See docs/engineering_methods.md for the +/-0.11 % consequence.
HYDROSTATIC_K_PSI_PER_FT_PER_PPG = 0.052


class Dimension(StrEnum):
    """Dimensions the platform reasons about."""

    LENGTH = "LENGTH"
    PRESSURE = "PRESSURE"
    PRESSURE_GRADIENT = "PRESSURE_GRADIENT"  # psi/ft, kPa/m
    DENSITY = "DENSITY"
    MUD_WEIGHT = "MUD_WEIGHT"  # ppg, SG, kg/m3 - semantically distinct from density
    VOLUME = "VOLUME"
    VOLUME_PER_LENGTH = "VOLUME_PER_LENGTH"  # annular/pipe capacity
    FLOW_RATE = "FLOW_RATE"
    RATE = "RATE"  # penetration rate
    ROTARY_SPEED = "ROTARY_SPEED"
    FORCE = "FORCE"
    MASS = "MASS"
    TORQUE = "TORQUE"
    TIME = "TIME"
    ANGLE = "ANGLE"
    CURVATURE = "CURVATURE"  # dogleg severity: angle per measured length
    TEMPERATURE = "TEMPERATURE"
    RATIO = "RATIO"  # dimensionless
    PERCENT = "PERCENT"
    COUNT = "COUNT"
    COST_PER_LENGTH = "COST_PER_LENGTH"
    COST = "COST"
    AREA = "AREA"


@dataclass(frozen=True)
class Unit:
    """A unit: ``value_in_base = (value - offset) * factor``."""

    symbol: str
    name: str
    dimension: Dimension
    factor: float
    offset: float = 0.0
    #: Human note about convention/rounding, shown in documentation and tool tips.
    note: str = ""

    def to_base(self, value: float) -> float:
        return (value + self.offset) * self.factor

    def from_base(self, base_value: float) -> float:
        return base_value / self.factor - self.offset


_UNITS: dict[str, Unit] = {}
_ALIASES: dict[str, str] = {}


def _reg(unit: Unit, *aliases: str) -> Unit:
    _UNITS[unit.symbol] = unit
    for alias in (unit.symbol, *aliases):
        key = alias.lower()
        existing = _ALIASES.get(key)
        if existing is not None and existing != unit.symbol:
            raise RuntimeError(f"unit alias collision: {key} -> {existing}/{unit.symbol}")
        _ALIASES[key] = unit.symbol
    return unit


# Length
_reg(Unit("m", "metre", Dimension.LENGTH, 1.0))
_reg(Unit("ft", "foot", Dimension.LENGTH, FT_M), "feet", "foot")
_reg(Unit("in", "inch", Dimension.LENGTH, IN_M), "inch", '"')
_reg(Unit("mm", "millimetre", Dimension.LENGTH, 1e-3))
_reg(Unit("km", "kilometre", Dimension.LENGTH, 1e3))
_reg(Unit("yd", "yard", Dimension.LENGTH, 0.9144))
_reg(Unit("mile", "statute mile", Dimension.LENGTH, 1609.344))
# Pressure
_reg(Unit("Pa", "pascal", Dimension.PRESSURE, 1.0))
_reg(Unit("kPa", "kilopascal", Dimension.PRESSURE, 1e3))
_reg(Unit("MPa", "megapascal", Dimension.PRESSURE, 1e6))
_reg(Unit("bar", "bar", Dimension.PRESSURE, 1e5), "bars")
_reg(Unit("psi", "pound per square inch", Dimension.PRESSURE, PSI_PA), "psia")
_reg(Unit("kg/cm2", "kilogram-force per square centimetre", Dimension.PRESSURE, KGF_N / (0.01**2)))
# Density / mud weight
_reg(Unit("kg/m3", "kilogram per cubic metre", Dimension.DENSITY, 1.0), "kg/m³", "kglm3")
_reg(Unit("g/cm3", "gram per cubic centimetre", Dimension.DENSITY, 1000.0), "sg_water", "sg")
_reg(
    Unit(
        "ppg",
        "pounds per US gallon",
        Dimension.MUD_WEIGHT,
        LB_KG / GAL_US_M3,
        note="1 ppg = 119.82642 kg/m3; SG uses water = 1000 kg/m3 by field convention",
    ),
    "lbs/gal",
    "lb/gal",
    "ppg(US)",
)
_reg(Unit("lb/ft3", "pound per cubic foot", Dimension.DENSITY, LB_KG / FT_M**3), "pcf")
# Continental density spellings: 1 kg/l == 1 t/m3 == 1000 kg/m3 (and the SG a mud
# engineer quotes is numerically the same number, by the water=1000 kg/m3 convention).
_reg(
    Unit("kg/l", "kilogram per litre", Dimension.DENSITY, 1000.0),
    "kg/dm3",
    "t/m3",
    "t/m\u00b3",
    "tonnes/m3",
    "g/l",
)
# Volume
_reg(Unit("m3", "cubic metre", Dimension.VOLUME, 1.0), "m³", "cum")
_reg(Unit("bbl", "barrel (42 US gal)", Dimension.VOLUME, BBL_M3), "bbls", "stb")
_reg(Unit("gal", "US gallon", Dimension.VOLUME, GAL_US_M3), "gal(US)", "gallon")
_reg(Unit("L", "litre", Dimension.VOLUME, 1e-3), "l", "litre", "liter")
_reg(Unit("ft3", "cubic foot", Dimension.VOLUME, FT_M**3), "cf")
# Volume per length (capacities)
_reg(Unit("bbl/ft", "barrel per foot", Dimension.VOLUME_PER_LENGTH, BBL_M3 / FT_M), "bbl/ft2")
_reg(Unit("bbl/m", "barrel per metre", Dimension.VOLUME_PER_LENGTH, BBL_M3))
_reg(Unit("m3/m", "cubic metre per metre", Dimension.VOLUME_PER_LENGTH, 1.0))
_reg(Unit("gal/ft", "gallon per foot", Dimension.VOLUME_PER_LENGTH, GAL_US_M3 / FT_M))
# Flow
_reg(Unit("m3/s", "cubic metre per second", Dimension.FLOW_RATE, 1.0))
_reg(Unit("l/s", "litre per second", Dimension.FLOW_RATE, 1e-3), "L/s")
_reg(Unit("gpm", "US gallon per minute", Dimension.FLOW_RATE, GAL_US_M3 / 60.0))
_reg(Unit("bbl/min", "barrel per minute", Dimension.FLOW_RATE, BBL_M3 / 60.0))
_reg(Unit("rpm", "revolutions per minute", Dimension.ROTARY_SPEED, 1 / 60.0), "rev/min", "r/min")
_reg(Unit("l/min", "litre per minute", Dimension.FLOW_RATE, 1e-3 / 60.0), "L/min")
# Penetration rate
_reg(Unit("ft/hr", "foot per hour", Dimension.RATE, FT_M / 3600.0), "fph", "ft/h")
_reg(Unit("m/hr", "metre per hour", Dimension.RATE, 1 / 3600.0), "m/h")
_reg(Unit("m/s", "metre per second", Dimension.RATE, 1.0))
# Force
_reg(Unit("N", "newton", Dimension.FORCE, 1.0))
_reg(Unit("kN", "kilonewton", Dimension.FORCE, 1e3))
_reg(Unit("lbf", "pound-force", Dimension.FORCE, LBF_N))
_reg(Unit("kip", "kilopound-force", Dimension.FORCE, 1000 * LBF_N))
_reg(Unit("kgf", "kilogram-force", Dimension.FORCE, KGF_N))
# Mass
_reg(Unit("kg", "kilogram", Dimension.MASS, 1.0))
_reg(Unit("lb", "pound (avoirdupois)", Dimension.MASS, LB_KG), "lbs", "lbm")
_reg(Unit("t", "tonne", Dimension.MASS, 1000.0), "tonne", "tonne metric")
_reg(Unit("short ton", "short ton", Dimension.MASS, 2000 * LB_KG))
# Torque
_reg(Unit("N.m", "newton metre", Dimension.TORQUE, 1.0), "Nm")
_reg(Unit("kN.m", "kilonewton metre", Dimension.TORQUE, 1e3), "kNm")
_reg(Unit("ft.lbf", "foot pound-force", Dimension.TORQUE, LBF_N * FT_M), "ft-lbf", "ftlb", "ft-lb")
_reg(Unit("in.lbf", "inch pound-force", Dimension.TORQUE, LBF_N * IN_M), "in-lbf", "in-lb")
# Time
_reg(Unit("s", "second", Dimension.TIME, 1.0), "sec", "second")
_reg(Unit("min", "minute", Dimension.TIME, 60.0), "minute")
_reg(Unit("h", "hour", Dimension.TIME, 3600.0), "hr", "hour", "hri")
_reg(Unit("d", "day", Dimension.TIME, 86400.0), "day", "days")
# Angle / curvature
_reg(Unit("deg", "degree", Dimension.ANGLE, math.pi / 180.0), "degree", "o")
_reg(Unit("rad", "radian", Dimension.ANGLE, 1.0))
_reg(
    Unit("deg/100ft", "degrees per 100 feet", Dimension.CURVATURE, math.pi / 180.0 / (100 * FT_M)),
    "deg/100 ft",
)
_reg(
    Unit("deg/30m", "degrees per 30 metres", Dimension.CURVATURE, math.pi / 180.0 / 30.0),
    "deg/30 m",
)
_reg(Unit("deg/m", "degrees per metre", Dimension.CURVATURE, math.pi / 180.0))
# Gradient
_reg(Unit("psi/ft", "pound per square inch per foot", Dimension.PRESSURE_GRADIENT, PSI_PA / FT_M))
_reg(Unit("kPa/m", "kilopascal per metre", Dimension.PRESSURE_GRADIENT, 1e3))
_reg(Unit("bar/m", "bar per metre", Dimension.PRESSURE_GRADIENT, 1e5))
_reg(Unit("MPa/m", "megapascal per metre", Dimension.PRESSURE_GRADIENT, 1e6))
_reg(Unit("psi/m", "pound per square inch per metre", Dimension.PRESSURE_GRADIENT, PSI_PA))
_reg(
    Unit(
        "ppg/ft",
        "ppg per foot (equivalent MW gradient)",
        Dimension.PRESSURE_GRADIENT,
        LB_KG / GAL_US_M3 / FT_M,
    )
)
# Temperature
_reg(Unit("K", "kelvin", Dimension.TEMPERATURE, 1.0))
_reg(Unit("degC", "degree Celsius", Dimension.TEMPERATURE, 1.0, offset=273.15), "C", "c", "deg c")
_reg(Unit("degF", "degree Fahrenheit", Dimension.TEMPERATURE, 5.0 / 9.0, offset=459.67), "F", "f")
# Dimensionless / commercial
_reg(Unit("", "dimensionless", Dimension.RATIO, 1.0), "ratio", "1")
_reg(Unit("%", "per cent", Dimension.PERCENT, 0.01), "percent", "pct")
_reg(Unit("count", "count", Dimension.COUNT, 1.0), "ea", "nos")
_reg(Unit("USD", "US dollar", Dimension.COST, 1.0), "$", "usd")
_reg(Unit("USD/ft", "US dollar per foot", Dimension.COST_PER_LENGTH, 1.0 / FT_M), "$/ft")
_reg(Unit("USD/d", "US dollar per day", Dimension.COST, 1.0), "$/day", "USD/day")
_reg(Unit("m2", "square metre", Dimension.AREA, 1.0))
_reg(Unit("ft2", "square foot", Dimension.AREA, FT_M**2))


#: Dimensions that describe the same physical thing in this domain and are
#: therefore interconvertible.  ``SG`` and ``ppg`` are both mud weight; a
#: general ``kg/m3`` density is accepted where a mud weight is expected because
#: drilling reports use them interchangeably (the group, not a silent guess,
#: makes that legal).  Everything else only converts inside its own dimension.
_DIMENSION_GROUPS: dict[Dimension, str] = {
    Dimension.DENSITY: "density",
    Dimension.MUD_WEIGHT: "density",
}


def dimension_group(dimension: Dimension) -> str:
    return _DIMENSION_GROUPS.get(dimension, dimension.value)


def convertible(source: Dimension | Unit | str, target: Dimension | Unit | str) -> bool:
    def dim(value: Dimension | Unit | str) -> Dimension:
        if isinstance(value, Dimension):
            return value
        return resolve_unit(value).dimension

    return dimension_group(dim(source)) == dimension_group(dim(target))


class Quantity:
    """An immutable number plus its unit.

    Arithmetic keeps units honest: ``+``/``-`` require identical units,
    ``*``/``/`` return a plain float (a ratio or a derived value that the
    caller must re-attach a unit to) unless the other side is a plain number.
    """

    __slots__ = ("_unit", "_value")

    def __init__(self, value: float, unit: Unit) -> None:
        if unit is None:
            raise UnitError("Quantity requires an explicit unit", value=value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UnitError(
                f"Quantity value must be a number, got {type(value).__name__}", value=value
            )
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise UnitError("Quantity value must be finite", value=value, unit=unit.symbol)
        self._value = float(value)
        self._unit = unit

    # -- construction -------------------------------------------------------
    @classmethod
    def of(cls, value: float, unit: str | Unit) -> Quantity:
        return cls(float(value), resolve_unit(unit))

    # ``1,234.5`` (thousands) and ``10,2`` (decimal comma) both have to parse: the
    # thousands alternative is tried first so a comma followed by exactly three digits
    # stays a group separator, while ``10,2`` can only be a decimal.
    _PARSE_RE = re.compile(
        r"^\s*(?P<sign>[-+]?)\s*"
        r"(?P<number>(?:\d+(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d{3})+,\d+|\d+(?:[.,]\d+)?)(?:[eE][-+]?\d+)?)"
        r"\s*(?P<unit>[^\d\s].*?)?\s*$"
    )

    @classmethod
    def parse(cls, text: str, default_unit: str | Unit | None = None) -> Quantity:
        """Parse ``"10.2 ppg"``/``"1,024 ft"``/``"12 1/4 in"`` (nominal sizes as inches)."""
        if isinstance(text, (int, float)) and not isinstance(text, bool):
            if default_unit is None:
                raise UnitError("Cannot infer a unit from a bare number", value=text)
            return cls.of(text, default_unit)
        raw = str(text).strip()
        if not raw:
            raise UnitError("Empty value", text=text)
        expected = resolve_unit(default_unit) if default_unit is not None else None
        # Fractional nominal sizes, e.g. a 12 1/4" hole section written as 12 1/4 in
        frac = re.match(r"^(\d+)[- ](\d+)/(\d+)\s*(.*)$", raw)
        if frac:
            whole, num, den, rest = frac.groups()
            value = float(whole) + float(num) / float(den)
            unit_text = rest.strip().strip('"').strip()
            unit = resolve_unit(unit_text) if unit_text else (expected or resolve_unit("in"))
            if expected is not None and not convertible(unit.dimension, expected.dimension):
                raise DimensionMismatchError(
                    f"Parsed unit {unit.symbol!r} disagrees with expected dimension {expected.dimension.value}",
                    found=unit.symbol,
                    expected=expected.symbol,
                )
            return cls(value, unit)
        match = cls._PARSE_RE.match(raw)
        if not match:
            raise UnitError(f"Cannot parse value from {raw!r}", text=text)
        number = match.group("number")
        unit_text = (match.group("unit") or "").strip()
        value = parse_decimal(number)
        if match.group("sign") == "-":
            value = -value
        if unit_text:
            unit = resolve_unit(unit_text)
            if expected is not None and not convertible(unit.dimension, expected.dimension):
                raise DimensionMismatchError(
                    f"Parsed unit {unit.symbol!r} disagrees with expected dimension {expected.dimension.value}",
                    found=unit.symbol,
                    expected=expected.symbol,
                )
        elif expected is not None:
            unit = expected
        else:
            raise UnknownUnitError(f"No unit in {raw!r} and no default unit supplied", text=text)
        return cls(value, unit)

    # -- properties ---------------------------------------------------------
    @property
    def value(self) -> float:
        return self._value

    @property
    def unit(self) -> Unit:
        return self._unit

    @property
    def dimension(self) -> Dimension:
        return self._unit.dimension

    @property
    def base_value(self) -> float:
        """Value in the canonical SI base unit of the dimension."""
        return self._unit.to_base(self._value)

    # -- conversion ---------------------------------------------------------
    def to(self, unit: str | Unit) -> Quantity:
        target = resolve_unit(unit)
        if not convertible(self._unit.dimension, target.dimension):
            raise DimensionMismatchError(
                f"Cannot convert {self._unit.symbol!r} ({self._unit.dimension.value}) to "
                f"{target.symbol!r} ({target.dimension.value})",
                source=self._unit.symbol,
                target=target.symbol,
            )
        return Quantity(target.from_base(self.base_value), target)

    def as_dimension(self, dimension: Dimension | str) -> Quantity:
        """Express this value in the field-default unit of ``dimension``.

        Used when an engineering method declares "mud weight" but the source
        quoted SG: the conversion is explicit, recorded and unit-checked.
        """
        dim = dimension if isinstance(dimension, Dimension) else Dimension(dimension)
        return self.to(default_unit(dim))

    def value_in(self, unit: str | Unit) -> float:
        return self.to(unit).value

    def convert(self, unit: str | Unit) -> Quantity:  # alias, clearer intent at call sites
        return self.to(unit)

    # -- arithmetic ---------------------------------------------------------
    def __add__(self, other: object) -> Quantity:
        if isinstance(other, Quantity):
            if other.unit.symbol != self._unit.symbol:
                raise DimensionMismatchError(
                    "Refusing to add quantities with different units; convert explicitly with .to()",
                    left=self._unit.symbol,
                    right=other.unit.symbol,
                )
            return Quantity(self._value + other.value, self._unit)
        return NotImplemented

    def __sub__(self, other: object) -> Quantity:
        if isinstance(other, Quantity):
            if other.unit.symbol != self._unit.symbol:
                raise DimensionMismatchError(
                    "Refusing to subtract quantities with different units; convert explicitly with .to()",
                    left=self._unit.symbol,
                    right=other.unit.symbol,
                )
            return Quantity(self._value - other.value, self._unit)
        return NotImplemented

    def __mul__(self, other: object) -> Quantity:
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            return Quantity(self._value * float(other), self._unit)
        return NotImplemented

    __rmul__ = __mul__

    def __truediv__(self, other: object) -> Quantity | float:
        if isinstance(other, Quantity):
            if convertible(self.dimension, other.dimension):
                return self.base_value / other.base_value  # type: ignore[return-value]
            raise DimensionMismatchError(
                "Cannot divide incompatible dimensions",
                left=self._unit.symbol,
                right=other.unit.symbol,
            )
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            return Quantity(self._value / float(other), self._unit)
        return NotImplemented

    def __neg__(self) -> Quantity:
        return Quantity(-self._value, self._unit)

    def __abs__(self) -> Quantity:
        return Quantity(abs(self._value), self._unit)

    # -- comparison ---------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Quantity):
            if not convertible(self.dimension, other.dimension):
                return False
            return math.isclose(self.base_value, other.base_value, rel_tol=1e-9, abs_tol=1e-12)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Quantity) and convertible(self.dimension, other.dimension):
            return self.base_value < other.base_value
        raise DimensionMismatchError(
            "Cannot order values of different dimension",
            left=self._unit.symbol,
            right=getattr(other, "unit", type(other).__name__),
        )

    def __le__(self, other: object) -> bool:
        return self.__lt__(other) or self.__eq__(other) is True

    def __gt__(self, other: object) -> bool:
        return not self.__le__(other)

    def __ge__(self, other: object) -> bool:
        return not self.__lt__(other)

    def __hash__(self) -> int:
        return hash((self._unit.symbol, round(self.base_value, 12)))

    # -- presentation -------------------------------------------------------
    def __repr__(self) -> str:
        return f"Quantity({self._value!r}, {self._unit.symbol!r})"

    def __str__(self) -> str:
        return f"{format_number(self._value)} {self._unit.symbol}".strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self._value,
            "unit": self._unit.symbol,
            "dimension": self._unit.dimension.value,
            "base_value": self.base_value,
            "base_unit": base_unit_symbol(self._unit.dimension),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Quantity:
        unit = payload.get("unit") or ""
        value = payload.get("value")
        if value is None or unit == "":
            raise UnitError("Quantity dict needs 'value' and 'unit'", payload=payload)
        return cls.of(float(value), str(unit))

    def with_value(self, value: float) -> Quantity:
        return Quantity(value, self._unit)


@dataclass(frozen=True)
class DimensionSpec:
    """Declares what an engineering input/output must look like."""

    name: str
    dimension: Dimension
    accepted_units: tuple[str, ...] = field(default_factory=tuple)
    #: Field-preferred unit used for display; the stored value keeps its own unit.
    preferred_unit: str = ""


def resolve_unit(token: str | Unit) -> Unit:
    """Look up a unit by symbol or alias (case-insensitive, tolerant of spacing)."""
    if isinstance(token, Unit):
        return token
    raw = str(token).strip()
    if raw in _UNITS:
        return _UNITS[raw]
    key = re.sub(r"\s+", "", raw).lower()
    # unicode approximations that appear in drilling reports
    key = (
        key.replace("³", "3")
        .replace("²", "2")
        .replace("µ", "u")
        .replace("′", "'")
        .replace("″", '"')
    )
    if key in _ALIASES:
        return _UNITS[_ALIASES[key]]
    # "pounds/gal", "PPG" and trailing punctuation from PDF text
    trimmed = key.rstrip(". ")
    if trimmed in _ALIASES:
        return _UNITS[_ALIASES[trimmed]]
    raise UnknownUnitError(
        f"Unknown unit {token!r}", token=token, known_examples=sorted(_UNITS)[:24]
    )


def known_units() -> list[str]:
    return sorted(_UNITS)


def base_unit_symbol(dimension: Dimension) -> str:
    for unit in _UNITS.values():
        if unit.dimension is dimension and unit.factor == 1.0 and unit.offset == 0.0:
            return unit.symbol
    return ""


def convert(value: float, source: str | Unit, target: str | Unit) -> float:
    return Quantity.of(value, source).value_in(target)


def same_dimension(a: str | Unit, b: str | Unit) -> bool:
    return resolve_unit(a).dimension is resolve_unit(b).dimension


#: Dimension -> default unit for display of engineering results.
FIELD_DEFAULT_UNITS: dict[Dimension, str] = {
    Dimension.LENGTH: "ft",
    Dimension.PRESSURE: "psi",
    Dimension.PRESSURE_GRADIENT: "psi/ft",
    Dimension.MUD_WEIGHT: "ppg",
    Dimension.DENSITY: "kg/m3",
    Dimension.VOLUME: "bbl",
    Dimension.VOLUME_PER_LENGTH: "bbl/ft",
    Dimension.FLOW_RATE: "l/s",
    Dimension.RATE: "ft/hr",
    Dimension.ROTARY_SPEED: "rpm",
    Dimension.FORCE: "lbf",
    Dimension.MASS: "lb",
    Dimension.TORQUE: "ft.lbf",
    Dimension.TIME: "h",
    Dimension.ANGLE: "deg",
    Dimension.CURVATURE: "deg/100ft",
    Dimension.TEMPERATURE: "degC",
    Dimension.PERCENT: "%",
    Dimension.COST: "USD",
    Dimension.COST_PER_LENGTH: "USD/ft",
}


def default_unit(dimension: Dimension) -> Unit:
    symbol = FIELD_DEFAULT_UNITS.get(dimension, "")
    return resolve_unit(symbol) if symbol else Unit("", "", dimension, 1.0)


def parse_decimal(text: str) -> float:
    """Read a numeric string written with either separator convention.

    ``1,234.5`` is thousands-separated English, ``10,2`` is a European decimal comma (a
    comma followed by anything other than exactly three digits cannot be a group
    separator), and ``1.234,56`` is continental thousands-plus-decimal.  Getting this
    wrong is the classic way to record a mud weight of 10.2 ppg as 102 ppg, so the rule
    lives in one place and is unit-tested.
    """
    text = (text or "").strip().replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):  # comma is the decimal separator
            head, _, tail = text.replace(".", "").rpartition(",")
            text = head.replace(",", "") + "." + tail
        else:
            text = text.replace(",", "")
    elif "," in text:
        groups = text.split(",")
        if len(groups) > 1 and all(len(part) == 3 for part in groups[1:]) and len(groups[0]) <= 3:
            text = text.replace(",", "")  # 1,234 / 1,234,567
        elif len(groups) == 2 and len(groups[1]) != 3:
            text = groups[0] + "." + groups[1]  # 10,2
        else:
            text = text.replace(",", "")
    return float(text)


def format_number(value: float, sig: int = 6) -> str:
    """Trim float noise the way an engineer writes it down."""
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.{sig}g}"
    return text


def mud_weight_equivalent(value: Quantity) -> Quantity:
    """Express any mud-weight/density value in ppg (for comparison, not for storage)."""
    if convertible(value.dimension, Dimension.MUD_WEIGHT):
        return value.to("ppg")
    raise DimensionMismatchError("Not a density/mud-weight value", unit=value.unit.symbol)


__all__ = [
    "BBL_M3",
    "FIELD_DEFAULT_UNITS",
    "FT_M",
    "G0",
    "GAL_US_M3",
    "HYDROSTATIC_K_PSI_PER_FT_PER_PPG",
    "IN_M",
    "PSI_PA",
    "Dimension",
    "DimensionSpec",
    "Quantity",
    "Unit",
    "base_unit_symbol",
    "convert",
    "convertible",
    "default_unit",
    "dimension_group",
    "format_number",
    "known_units",
    "mud_weight_equivalent",
    "parse_decimal",
    "resolve_unit",
    "same_dimension",
]
