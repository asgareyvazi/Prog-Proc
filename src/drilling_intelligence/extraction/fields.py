"""Deterministic drilling-field extraction (sections 29 and 58).

This is *pattern* extraction, not AI extraction: every hit is a literal match of
a documented regular expression against a paragraph or a table row, and each
produced :class:`~drilling_intelligence.core.results.DataField` carries the
provenance of the text it was read from.

Three rules the rest of the platform depends on:

*   A value whose unit is not written in the source is stored with the
    convention unit **and quality** ``UNVERIFIED`` - the value is available for
    retrieval/reading, but :meth:`DataField.require_quantity` refuses to
    calculate with it until a unit is confirmed.  We never guess silently.
*   Out-of-range values are recorded with quality ``INVALID`` plus a note, not
    dropped: a wrong number in a report is an engineering finding.
*   Conflicting statements are all kept; resolving them is
    :mod:`drilling_intelligence.knowledge.conflicts`' job against the
    configurable authority ladder (section 19).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..core.enums import DataQuality
from ..core.provenance import Provenance
from ..core.results import DataField
from ..core.units import Dimension, Quantity, convertible, default_unit, known_units, resolve_unit

#: Context window (characters) searched for qualifying keywords around a match.
CONTEXT_WINDOW = 90

# --- pattern fragments (single source of truth for numeric parsing) ------------
#: 3200 | 3,200 | 3 200 | 12.5 | 12,5 (European decimal comma)
NUM = r"\d+(?:[,.]\d{3})*(?:[.,]\d+)?"
#: Mud weights are always written with a decimal part in drilling reports.
MUD_NUM = r"\d{1,2}[.,]\d{1,2}"
#: Nominal sizes: 12 1/4, 9-5/8, 8.33, 13 3/8"
SIZE = r'\d{1,2}[- ]\d/\d|\d{1,2}[.,]\d{2}'


def _unit_re(*units: str) -> str:
    """Named capture group matching one of ``units`` (longest first)."""
    ordered = sorted(units, key=len, reverse=True)
    return r"(?P<unit>" + "|".join(re.escape(u) for u in ordered) + r")"


def _value_re(pattern: str = NUM) -> str:
    return rf"(?P<value>{pattern})"


@dataclass(frozen=True)
class FieldRule:
    """One deterministic extraction rule."""

    name: str
    pattern: re.Pattern[str]
    #: Keyword that must appear within ``CONTEXT_WINDOW`` around the match.
    context: tuple[str, ...] = ()
    #: Keywords that disqualify a match.
    reject: tuple[str, ...] = ()
    dimension: Dimension | None = None
    #: Field convention used when the source stated no unit (flagged UNVERIFIED).
    default_unit: str = ""
    #: Unit implied by the label itself, e.g. "RPM 120" -> rpm (that is VALID).
    label_unit: tuple[tuple[str, str], ...] = ()
    #: True when a unit must be present for the value to be recorded at all.
    require_unit: bool = False
    confidence: float = 0.6
    #: Bounds for :attr:`plausible_range`, expressed in ``range_unit`` (so that a
    #: value written in SG or ft is compared on the same scale, not naively).
    range_unit: str = ""
    plausible_range: tuple[float, float] | None = None
    doc: str = ""

    def matches_context(self, window: str, reject_window: str | None = None) -> bool:
        """Keyword gate around a match.

        ``window`` is the region that must contain a ``context`` keyword;
        ``reject_window`` is the region whose wording *disqualifies* the match -
        deliberately the text before and between the label and the value, not 90
        characters of whatever follows.  A program line such as "Design mud weight
        is 10.2 ppg with an ECD target of 10.6" must yield MW 10.2 even though
        "ECD" appears nearby, whereas "Do not exceed 11.4 ppg" must not yield a
        mud weight at all.
        """
        lowered = window.lower()
        if self.context and not any(keyword.lower() in lowered for keyword in self.context):
            return False
        rejected = (reject_window if reject_window is not None else window).lower()
        return not any(keyword.lower() in rejected for keyword in self.reject)


_MUD_UNITS = ("ppg", "kg/m3", "kg/m³", "lb/gal", "g/cm3", "sg")

_RULES: tuple[FieldRule, ...] = (
    FieldRule(
        name="mud_weight",
        pattern=re.compile(
            rf"(?i)\b(?:mw|m\.w\.|mud weight|weight of mud|maintain(ed)? mw|active system mw)\s*(?:=|:|of|is|at)?\s*{_value_re(MUD_NUM)}\s*{_unit_re(*_MUD_UNITS)}?"
        ),
        reject=("equivalent", "forecast", "ECD"),
        dimension=Dimension.MUD_WEIGHT,
        confidence=0.85,
        range_unit="ppg",
        plausible_range=(5.0, 25.0),
        doc='Labelled mud weight: "MW 10.2 ppg", "Mud Weight: 1.24 SG".',
    ),
    FieldRule(
        name="mud_weight",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re(MUD_NUM)}\s*{_unit_re('ppg', 'kg/m3', 'kg/m³', 'lb/gal')}"),
        context=("mud", "weight", "mw", "slurry", "spacer", "pill", "riser", "kill"),
        # A bare "11.4 ppg" is only a mud weight when nothing in front of it turns it
        # into a limit, a forecast or an equivalent density.
        reject=("equivalent circulating", "ECD", "exceed", "limit", "maximum", "minimum", "do not", "max ", "min "),
        dimension=Dimension.MUD_WEIGHT,
        confidence=0.7,
        range_unit="ppg",
        plausible_range=(5.0, 25.0),
        doc="Unit-anchored density inside a mud context (DDR table rows, mud logs).",
    ),
    FieldRule(
        name="equivalent_mud_weight",
        pattern=re.compile(rf"(?i)\b(?:emw|ecd|equivalent(?: circulating)? (?:mud )?weight)\s*(?:=|:|of|is|at|during)?\s*{_value_re(MUD_NUM)}\s*{_unit_re(*_MUD_UNITS)}?"),
        dimension=Dimension.MUD_WEIGHT,
        confidence=0.75,
        range_unit="ppg",
        plausible_range=(5.0, 25.0),
        doc="ECD/EMW kept as its own field so a dynamic density never masquerades as the static MW.",
    ),
    FieldRule(
        name="fracture_gradient",
        pattern=re.compile(rf"(?i)\b(?:frac(?:ture)?\s*(?:grad(?:ient)?)?|fg)\s*(?:=|:|of|is|at)?\s*{_value_re()}\s*(?P<unit>ppg/ft|psi/ft|kPa/m|bar/m|MPa/m|ppg)?"),
        # No dimension: a gradient (psi/ft, kPa/m) and an equivalent mud weight
        # (ppg) are both legal ways to state fracture gradient, and the unit must
        # carry that meaning rather than a forced normalisation.
        dimension=None,
        confidence=0.7,
        range_unit="psi/ft",
        plausible_range=(0.3, 1.3),
        require_unit=False,
        doc=(
            "Fracture gradient.  The unit is never assumed: 0.72 psi/ft, 15.7 kPa/m and "
            "16.5 ppg (equivalent MW at the shoe) are all legal, so an unqualified number is "
            "recorded UNVERIFIED and the plausibility gate is skipped when units are incomparable."
        ),
    ),
    FieldRule(
        name="pore_pressure_gradient",
        pattern=re.compile(rf"(?i)\bpore pressure\s*grad(?:ient)?\s*(?:=|:|of|is|at)?\s*{_value_re()}\s*(?P<unit>ppg/ft|psi/ft|kPa/m|bar/m)?"),
        dimension=None,
        confidence=0.7,
        range_unit="psi/ft",
        plausible_range=(0.3, 1.3),
        doc="Pore pressure gradient (unit must be stated).",
    ),
    FieldRule(
        name="depth_md",
        pattern=re.compile(rf"(?i)\b(?:md|m\.d\.|measured depth|depth md)\s*(?:=|:|of|is|at|to)?\s*{_value_re()}\s*(?P<unit>m|ft)?\b"),
        reject=("tvd", "vertical"),
        dimension=Dimension.LENGTH,
        confidence=0.8,
        range_unit="ft",
        plausible_range=(0.0, 40000.0),
        doc='Labelled measured depth, as written in a DDR header or a spreadsheet row: "MD (ft) 10,125 ft".',
    ),
    FieldRule(
        name="depth_tvd",
        pattern=re.compile(rf"(?i)\b(?:tvd|tvdss|true vertical depth)(?: ss)?\s*(?:=|:|of|is|at|to)?\s*{_value_re()}\s*(?P<unit>m|ft)?\b"),
        dimension=Dimension.LENGTH,
        confidence=0.8,
        range_unit="ft",
        plausible_range=(0.0, 40000.0),
        doc="Labelled true vertical depth: the value every hydrostatic and MAASP calculation actually needs.",
    ),
    FieldRule(
        name="depth_md",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>m|ft|meters?|metres?)\s*(?:md)\b"),
        # No context keywords: requiring the literal "MD" after the unit already
        # distinguishes a depth from any other number, and demanding a nearby word
        # like "bit" silently dropped "from 9,780 ft MD to 10,125 ft MD".
        context=(),
        dimension=Dimension.LENGTH,
        confidence=0.65,
        range_unit="m",
        plausible_range=(0.0, 40000.0),
        doc="Measured depth, e.g. '3210.5 m MD'.",
    ),
    FieldRule(
        name="depth_tvd",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>m|ft)\s*tvd(?:ss)?\b"),
        dimension=Dimension.LENGTH,
        confidence=0.7,
        range_unit="m",
        plausible_range=(0.0, 40000.0),
        doc="True vertical depth (TVD/TVDSS).",
    ),
    FieldRule(
        name="total_depth",
        pattern=re.compile(rf"(?i)\b(?:td|total depth|final depth|depth at TD|spud to TD)\s*(?:=|:|of|is|at|@)?\s*{_value_re()}\s*(?P<unit>m|ft)?"),
        dimension=Dimension.LENGTH,
        default_unit="m",
        confidence=0.6,
        range_unit="m",
        plausible_range=(0.0, 40000.0),
        doc="Total depth statement; a missing unit makes it UNVERIFIED and unusable for calculation.",
    ),
    FieldRule(
        name="casing_shoe_depth",
        pattern=re.compile(
            rf"(?i)(?P<size>{SIZE})\s*(?:\"|inch)?\s*casing\b[^.\n]{{0,60}}?\b(?:shoe|set)\b\s*(?:at|@|=|:)?\s*{_value_re()}\s*(?P<unit>m|ft)?"
        ),
        dimension=Dimension.LENGTH,
        default_unit="m",
        confidence=0.65,
        range_unit="m",
        plausible_range=(0.0, 40000.0),
        doc='Casing shoe depth, e.g. 9 5/8" casing shoe @ 2450 m.',
    ),
    FieldRule(
        name="casing_shoe_depth",
        pattern=re.compile(rf"(?i)\b(?:cs|casing)\s*shoe\s*(?:at|@|=|:|depth)?\s*{_value_re()}\s*(?P<unit>m|ft)?"),
        context=("casing", "shoe"),
        dimension=Dimension.LENGTH,
        default_unit="m",
        confidence=0.6,
        range_unit="m",
        plausible_range=(0.0, 40000.0),
        doc='Casing shoe depth written without a casing size, e.g. "CS shoe 1500 m".',
    ),
    FieldRule(
        name="hole_size_in",
        pattern=re.compile(rf'(?i)(?P<value>{SIZE})\s*(?:inch|in\b|")\s*(?:hole|bit|section|open hole|drill)'),
        dimension=Dimension.LENGTH,
        default_unit="in",
        confidence=0.75,
        range_unit="in",
        plausible_range=(3.0, 48.0),
        doc='Hole/bit nominal size in inches (industry nominal-size convention), e.g. 12 1/4" hole.',
    ),
    FieldRule(
        name="casing_size_in",
        pattern=re.compile(rf'(?i)(?P<value>{SIZE})\s*(?:inch|in\b|")\s*(?:casing|liner)\b'),
        dimension=Dimension.LENGTH,
        default_unit="in",
        confidence=0.75,
        range_unit="in",
        plausible_range=(4.5, 36.0),
        doc='Casing/liner nominal size in inches, e.g. 9 5/8" casing.',
    ),
    FieldRule(
        name="rop",
        pattern=re.compile(rf"(?i)\b(?:rop|rate of penetration|average rop|net rop|average rate)\s*(?:=|:|of|is|was|at)?\s*{_value_re()}\s*(?P<unit>m/hr|m/h|ft/hr|fph|f/h|m/s)?"),
        dimension=Dimension.RATE,
        default_unit="m/hr",
        confidence=0.6,
        range_unit="m/hr",
        plausible_range=(0.0, 1500.0),
        doc="Rate of penetration (metric default when the report omits the unit).",
    ),
    FieldRule(
        name="npt_hours",
        pattern=re.compile(rf"(?i)\bnpt\b[^0-9\n]{{0,20}}{_value_re()}\s*(?P<unit>h|hr|hrs|hours?|d|days?|min|mins|minutes?)?\b"),
        dimension=Dimension.TIME,
        label_unit=(("npt", "h"),),
        confidence=0.65,
        range_unit="h",
        plausible_range=(0.0, 10000.0),
        doc="Non-productive time duration; 'NPT 14' is read as 14 h (label implies hours) but UNVERIFIED.",
    ),
    FieldRule(
        name="pressure",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>psi|bar|kPa|MPa)\b"),
        context=("test", "pressure", "maasp", "kick", "bop", "choke", "standpipe", "annular", "pump", "lot", "x-test", "integrity"),
        reject=("mud weight", "gradient", "ppg"),
        dimension=Dimension.PRESSURE,
        confidence=0.6,
        range_unit="psi",
        plausible_range=(0.0, 60000.0),
        doc="Pressure reading in context (leak-off, BOP test, annular, standpipe).",
    ),
    FieldRule(
        name="surface_pressure",
        pattern=re.compile(rf"(?i)\b(?:sidpp|sicp|casing pressure|standpipe pressure|pump pressure|surface pressure|choke pressure)\s*(?:=|:|of|is|at)?\s*{_value_re()}\s*(?P<unit>psi|bar|kPa|MPa)?"),
        dimension=Dimension.PRESSURE,
        default_unit="psi",
        confidence=0.7,
        range_unit="psi",
        plausible_range=(0.0, 20000.0),
        doc="Well-control surface pressures (SIDPP/SICP/choke/standpipe).",
    ),
    FieldRule(
        name="torque",
        pattern=re.compile(rf"(?i)\b(?:torque|rotary torque|off-bottom torque)\s*(?:=|:|of|is|at|avg)?\s*{_value_re()}\s*(?P<unit>ft-?lbf|ft\.?lbf|kNm|kN\.?m|Nm|in-?lbf)?"),
        dimension=Dimension.TORQUE,
        default_unit="ft.lbf",
        confidence=0.6,
        range_unit="ft.lbf",
        plausible_range=(0.0, 200000.0),
        doc="Rotational torque.",
    ),
    FieldRule(
        name="wob",
        pattern=re.compile(rf"(?i)\b(?:wob|weight on bit)\s*(?:=|:|of|is|at|max)?\s*{_value_re()}\s*(?P<unit>kip|klbs|kips|lbs|lbf|kg|kN)?"),
        dimension=Dimension.FORCE,
        default_unit="kip",
        confidence=0.6,
        range_unit="kip",
        plausible_range=(0.0, 200.0),
        doc="Weight on bit (kip default when the report omits the unit).",
    ),
    FieldRule(
        name="rpm",
        pattern=re.compile(rf"(?i)\b(?:rpm|rotary speed|rotary)\s*(?:=|:|of|is|at|max|avg)?\s*{_value_re()}\s*(?P<unit>rpm)?"),
        dimension=Dimension.ROTARY_SPEED,
        label_unit=(("rpm", "rpm"), ("rotary", "rpm")),
        confidence=0.6,
        range_unit="rpm",
        plausible_range=(0.0, 400.0),
        doc="Rotary speed; the label 'RPM' itself states the unit.",
    ),
    FieldRule(
        name="flow_rate",
        pattern=re.compile(rf"(?i)\b(?:flow(?: rate)?|pump(?:ing)?(?: rate| output)|flowline|circulation rate)\s*(?:=|:|of|is|at|max|total)?\s*{_value_re()}\s*(?P<unit>l/s|l/min|gpm|bbl/min|cps)?"),
        dimension=Dimension.FLOW_RATE,
        default_unit="l/s",
        confidence=0.6,
        range_unit="l/s",
        plausible_range=(0.0, 100000.0),
        doc="Circulation rate.",
    ),
    FieldRule(
        name="mud_volume_bbl",
        pattern=re.compile(rf"(?i)\b(?:active (?:system|volume)|total volume|mud volume|trip volume|volume)\s*(?:=|:|of|is|at)?\s*{_value_re()}\s*(?P<unit>bbl|m3|bbls|stb)?\b"),
        context=("volume", "bbl", "m3", "pits", "active", "trip"),
        dimension=Dimension.VOLUME,
        default_unit="bbl",
        confidence=0.55,
        range_unit="bbl",
        plausible_range=(0.0, 200000.0),
        doc="Mud system / trip volume.",
    ),
    FieldRule(
        name="rpm",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>rpm|rev/min|revolutions? per minute)\b"),
        dimension=Dimension.ROTARY_SPEED,
        confidence=0.6,
        range_unit="rpm",
        plausible_range=(0.0, 400.0),
        doc="Rotary speed stated without a label - the unit alone identifies the quantity.",
    ),
    FieldRule(
        name="torque",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>ft-?lbf|ft\.?lbf|kNm|kN\.?m|in-?lbf)\b"),
        dimension=Dimension.TORQUE,
        confidence=0.6,
        range_unit="ft.lbf",
        plausible_range=(0.0, 200000.0),
        doc="Torque stated as a bare torque unit (ft-lbf/kNm are not used for anything else in a drilling report).",
    ),
    FieldRule(
        name="flow_rate",
        pattern=re.compile(rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>gpm|bbl/min)\b"),
        dimension=Dimension.FLOW_RATE,
        confidence=0.6,
        range_unit="gpm",
        plausible_range=(0.0, 5000.0),
        doc="Pump rate stated without a label: gpm identifies flow in a drilling report.",
    ),
    FieldRule(
        name="mud_volume_bbl",
        pattern=re.compile(
            rf"(?i)(?<![\d.,]){_value_re()}\s*(?P<unit>bbl|m3|bbls)\s*(?:of\s+)?(?:active\s+|total\s+|system\s+)*volume\b"
        ),
        context=("mud", "pit", "tank", "active", "system", "total", "trip"),
        dimension=Dimension.VOLUME,
        confidence=0.6,
        range_unit="bbl",
        plausible_range=(0.0, 200000.0),
        doc='"1,450 bbl total system volume": the number precedes the label in narrative text.',
    ),
    FieldRule(
        name="capacity",
        pattern=re.compile(rf"(?i)\b(?:capacity|annular capacity|pipe capacity|volume\s*/\s*(?:ft|m))\s*(?:=|:|of|is|at)?\s*{_value_re()}\s*(?P<unit>bbl/ft|bbl/m|m3/m|gal/ft)?"),
        dimension=Dimension.VOLUME_PER_LENGTH,
        default_unit="bbl/ft",
        confidence=0.55,
        range_unit="bbl/ft",
        plausible_range=(0.0, 200.0),
        doc="Capacity statements; used to cross-check computed volumes against the program (QA).",
    ),
    FieldRule(
        name="day_rate",
        pattern=re.compile(rf"(?i)\b(?:rig (?:day )?rate|day rate|daily cost|cost per day|tariff)\s*(?:=|:|of|is|at)?\s*{_value_re()}\s*(?P<unit>usd|\$|eur|gbp|NOK|GBP)?"),
        dimension=Dimension.COST,
        default_unit="USD",
        confidence=0.55,
        range_unit="USD",
        plausible_range=(0.0, 5000000.0),
        doc="Rig day rate / daily cost.",
    ),
    FieldRule(
        name="duration_hours",
        pattern=re.compile(rf"(?i)\b(?:duration|elapsed|took|time)\s*(?:=|:|of|is|was|for)?\s*{_value_re()}\s*(?P<unit>h|hr|hrs|hours?|d|days?|min|mins)\b"),
        dimension=Dimension.TIME,
        confidence=0.6,
        range_unit="h",
        plausible_range=(0.0, 100000.0),
        doc="Explicit activity duration with a stated unit.",
    ),
)

_DATE_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("date_iso", re.compile(r"(?P<value>\d{4}-\d{2}-\d{2})"), 0.85),
    ("date_text", re.compile(r"(?i)(?P<value>\d{1,2}[- ](?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[- ]\d{2,4})"), 0.7),
    ("date_dmy", re.compile(r"(?P<value>\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})"), 0.6),
)

#: Fields describing a well quantity: used for conflict detection, program QA and
#: the knowledge bridge.  ``None`` means "dimension varies with the stated unit".
WELL_QUANTITIES: dict[str, Dimension | None] = {
    "mud_weight": Dimension.MUD_WEIGHT,
    "equivalent_mud_weight": Dimension.MUD_WEIGHT,
    "fracture_gradient": None,
    "pore_pressure_gradient": None,
    "depth_md": Dimension.LENGTH,
    "depth_tvd": Dimension.LENGTH,
    "total_depth": Dimension.LENGTH,
    "casing_shoe_depth": Dimension.LENGTH,
    "hole_size_in": Dimension.LENGTH,
    "casing_size_in": Dimension.LENGTH,
    "rop": Dimension.RATE,
    "npt_hours": Dimension.TIME,
    "duration_hours": Dimension.TIME,
    "pressure": Dimension.PRESSURE,
    "surface_pressure": Dimension.PRESSURE,
    "torque": Dimension.TORQUE,
    "wob": Dimension.FORCE,
    "rpm": Dimension.ROTARY_SPEED,
    "flow_rate": Dimension.FLOW_RATE,
    "mud_volume_bbl": Dimension.VOLUME,
    "capacity": Dimension.VOLUME_PER_LENGTH,
    "day_rate": Dimension.COST,
}

# --- rule-set post-processing ---------------------------------------------------
#: Words (or a short parenthesised unit) that legitimately sit between a label and
#: its value: ``MD (ft)  10,125``, ``ECD target of 10.6 ppg``, ``torque averaged
#: 18,400 ft-lbf``.
_LABEL_FILLER = (
    r"(?:\s*(?:\([^)\n]{1,24}\)|[=,:]|of|is|at|to|was|were|being|averag(?:e|ed)|"
    r"typical(?:ly)?|target|design|maximum|minimum|limit(?:ed)?|maintain(?:ed)?|hold(?:ing)?|during|with)"
    r")*\s*"
)


def _loosen_label_gap(rule: FieldRule) -> FieldRule:
    """Allow the real-world noise between a label and its value.

    Drilling text rarely writes ``MW 10.2``: a program writes ``Mud weight is
    designed at 10.2 ppg`` and a workbook header writes ``EMW (ppg)  15.8``.  A
    strict ``label connector value`` pattern misses those silently, which is the
    worst kind of extraction bug because nothing is reported at all.  The filler
    accepts only a closed vocabulary of connector words or a short parenthesised
    unit, so it can never span a sentence or invent a number, and the match stays a
    verbatim substring of the source - which is what keeps provenance re-readable
    (master spec section 17).
    """
    pattern = rule.pattern.pattern
    head, separator, tail = pattern.partition("(?P<value>")
    if not separator:
        return rule
    return replace(rule, pattern=re.compile(head + _LABEL_FILLER + separator + tail, re.UNICODE))


def _is_value_first(rule: FieldRule) -> bool:
    """True for the fallback rules, which identify a field from its unit alone.

    These run *after* the label-anchored rules and may only report a number that no
    label has already explained (see :meth:`FieldExtractor.scan_text`).
    """
    return rule.pattern.pattern.startswith("(?i)(?<!")


_RULES = tuple(_loosen_label_gap(rule) for rule in _RULES)


def _value_span(match: re.Match[str]) -> tuple[int, int]:
    try:
        start, end = match.span("value")
    except (IndexError, ValueError):  # pragma: no cover - rules always capture a value
        return match.span()
    return (start, end) if start != -1 else match.span()


#: Field -> dimension used to normalise before comparison (conflict detection).
COMPARE_UNITS: dict[str, str] = {
    "mud_weight": "ppg",
    "equivalent_mud_weight": "ppg",
    "fracture_gradient": "ppg/ft",
    "pore_pressure_gradient": "ppg/ft",
    "depth_md": "m",
    "depth_tvd": "m",
    "total_depth": "m",
    "casing_shoe_depth": "m",
    "hole_size_in": "in",
    "casing_size_in": "in",
    "npt_hours": "h",
    "duration_hours": "h",
    "pressure": "psi",
    "surface_pressure": "psi",
    "mud_volume_bbl": "bbl",
}


def normalise_number(raw: str) -> str:
    """``1,850`` -> ``1850`` (grouping); ``12,5`` -> ``12.5`` (decimal comma)."""
    text = raw.replace("\u00a0", "").replace(" ", "")
    if not text:
        raise ValueError("empty number")
    if "," in text and "." in text:
        # 1,234.56 - comma is a group separator
        return text.replace(",", "")
    if text.count(",") == 1:
        head, tail = text.split(",")
        if len(tail) == 3 and len(head) <= 3 and "." not in text:
            # Ambiguous "1,250".  Drilling magnitudes make a decimal comma
            # almost impossible at this scale, so treat it as grouping and
            # record the decision for auditability.
            return text.replace(",", "")
        return text.replace(",", ".")
    return text


@dataclass
class FieldExtractor:
    """Applies the rule set to the structured units of a normalised document."""

    rules: tuple[FieldRule, ...] = _RULES
    #: A document may legitimately state the same field many times (per row).
    max_per_name: int = 60
    extract_dates: bool = True

    # -- API ----------------------------------------------------------------
    def scan_text(
        self,
        text: str,
        provenance: Provenance | None = None,
        *,
        source: str = "paragraph",
        section: str = "",
    ) -> list[DataField]:
        """Extract every field mentioned in one unit of text (paragraph or row).

        Two passes: label-anchored rules first, then the unit-only fallbacks.  A
        rule that has already consumed a number *claims* it, so a fallback cannot
        re-report the same digits under a different field name.  Without that
        ordering ``EMW (ppg) 15.8`` would also be recorded as a static mud weight,
        and a wrong value in the knowledge base is far worse than a missing one.
        """
        if not text:
            return []
        fields: list[DataField] = []
        claimed: list[tuple[int, int]] = []
        labelled = tuple(rule for rule in self.rules if not _is_value_first(rule))
        fallbacks = tuple(rule for rule in self.rules if _is_value_first(rule))
        for rules in (labelled, fallbacks):
            for rule in rules:
                for match in rule.pattern.finditer(text):
                    span = _value_span(match)
                    if any(span[0] < end and span[1] > start for start, end in claimed):
                        continue
                    window = text[max(0, match.start() - CONTEXT_WINDOW) : min(len(text), match.end() + 24)]
                    # Qualifiers count when they precede the value or sit between the
                    # label and the value; they are ignored in unrelated trailing text.
                    reject_window = text[max(0, match.start() - CONTEXT_WINDOW) : match.end()]
                    if not rule.matches_context(window, reject_window):
                        continue
                    field = self._to_field(rule, match, provenance, source, section, window)
                    if field is None:
                        continue
                    if _is_value_first(rule):
                        # The unit identified the quantity, not a label: say so, so a
                        # reviewer sees the inference instead of trusting it silently.
                        field = replace(
                            field,
                            note=(field.note + "; " if field.note else "") + "field inferred from the unit alone (no label in the source text)",
                        )
                    fields.append(field)
                    claimed.append(span)
        fields = _dedupe(fields)
        if self.extract_dates:
            fields.extend(self._dates(text, provenance, source))
        return fields

    def apply(self, document: Any, max_per_name: int | None = None) -> list[DataField]:
        """Extract fields from a :class:`NormalizedDocument` (paragraphs + table rows)."""
        limit = max_per_name or self.max_per_name
        tally: dict[str, int] = {}
        results: list[DataField] = []
        for paragraph in document.paragraphs:
            for item in self.scan_text(paragraph.text, paragraph.provenance, source="paragraph", section=paragraph.section):
                if tally.get(item.name, 0) >= limit:
                    continue
                tally[item.name] = tally.get(item.name, 0) + 1
                results.append(item)
        for table in document.tables:
            provenance = table.provenance
            for row_index, row in table.iter_data_rows():
                text = " ".join(cell for cell in row if cell)
                if not text.strip():
                    continue
                located = replace(provenance, excerpt=text[:400]) if provenance is not None else None
                for item in self.scan_text(
                    text,
                    located,
                    source=f"table:{table.table_id}:row{row_index}",
                    section=table.sheet or table.caption,
                ):
                    if tally.get(item.name, 0) >= limit:
                        continue
                    tally[item.name] = tally.get(item.name, 0) + 1
                    results.append(item)
        # A row is usually reachable both as a searchable paragraph and as a table
        # row; the same fact in the same place is one fact, not two.
        return _dedupe(results)

    # -- internals ----------------------------------------------------------
    def _dates(self, text: str, provenance: Provenance | None, source: str) -> list[DataField]:
        fields: list[DataField] = []
        claimed: list[tuple[int, int]] = []
        for name, pattern, confidence in _DATE_PATTERNS:
            for match in list(pattern.finditer(text))[:4]:
                span = (match.start(), match.end())
                if any(span[0] < end and span[1] > start for start, end in claimed):
                    continue  # a more specific date pattern already claimed this text
                claimed.append(span)
                fields.append(
                    DataField(
                        name=name,
                        value=match.group("value"),
                        unit="",
                        dimension="DATE",
                        quality=DataQuality.VALID,
                        provenance=(replace(provenance, excerpt=match.group(0).strip()[:200]) if provenance else None),
                        confidence=confidence,
                        method=f"{source}:{name}",
                    )
                )
        return fields

    def _to_field(
        self,
        rule: FieldRule,
        match: re.Match[str],
        provenance: Provenance | None,
        source: str,
        section: str,
        window: str,
    ) -> DataField | None:
        raw = (match.group("value") or "").strip()
        if not raw:
            return None
        try:
            if rule.name.endswith("_in"):
                value = nominal_inches(raw)
                unit = "in"
            else:
                value = float(normalise_number(raw))
                unit = _normalise_unit(match.groupdict().get("unit"), window, rule)
        except ValueError as exc:
            return DataField(
                name=rule.name,
                value=raw,
                unit="",
                quality=DataQuality.INVALID,
                provenance=(replace(provenance, excerpt=match.group(0).strip()[:400]) if provenance else None),
                confidence=0.0,
                method=f"{source}:{rule.name}",
                note=f"unparsable value {raw!r}: {exc}",
            )

        if unit:
            # Store the canonical symbol so that comparisons and conflict keys see
            # "kPa/m" whether the source wrote "kpa/m", "KPa/m" or "kPa / m".
            try:
                unit = resolve_unit(unit).symbol
            except Exception:  # noqa: BLE001 - an unknown unit stays as written, flagged below
                pass
        unit_source = ""
        if not unit:
            if rule.default_unit:
                unit, unit_source = rule.default_unit, f"unit assumed from field convention ({rule.default_unit})"
            else:
                if rule.require_unit:
                    return None
                unit, unit_source = "", "unit not stated in source"
        if rule.dimension is not None and unit:
            try:
                quantity = Quantity.of(value, resolve_unit(unit))
            except Exception as exc:  # noqa: BLE001 - an unparsable unit is a finding, not a crash
                return DataField(
                    name=rule.name,
                    value=value,
                    unit=unit,
                    dimension=rule.dimension.value,
                    quality=DataQuality.INVALID,
                    provenance=(replace(provenance, excerpt=match.group(0).strip()[:400]) if provenance else None),
                    confidence=0.0,
                    method=f"{source}:{rule.name}",
                    note=f"unit error: {exc}",
                )
            if not convertible(quantity.dimension, rule.dimension):
                return DataField(
                    name=rule.name,
                    value=value,
                    unit=unit,
                    dimension=quantity.dimension.value,
                    quality=DataQuality.INVALID,
                    provenance=(replace(provenance, excerpt=match.group(0).strip()[:400]) if provenance else None),
                    confidence=0.0,
                    method=f"{source}:{rule.name}",
                    note=f"dimension mismatch: {quantity.dimension.value} vs expected {rule.dimension.value}",
                )
        quality = DataQuality.VALID
        confidence = rule.confidence
        notes = [unit_source] if unit_source else []
        if unit_source:
            quality = DataQuality.UNVERIFIED
            confidence = round(rule.confidence * 0.8, 3)
        if rule.plausible_range is not None and unit:
            verdict = _plausible(value, unit, rule)
            low, high = rule.plausible_range
            if verdict is False:
                quality = DataQuality.INVALID
                confidence = round(rule.confidence * 0.3, 3)
                notes.append(f"outside plausible range [{low}, {high}] {rule.range_unit or rule.default_unit or unit}")
            elif verdict is None:
                notes.append(f"range not checked (unit {unit} incomparable with {rule.range_unit})")
        return DataField(
            name=rule.name,
            value=value,
            unit=unit,
            dimension=rule.dimension.value if rule.dimension else "",
            quality=quality,
            provenance=(replace(provenance, excerpt=match.group(0).strip()[:400]) if provenance else None),
            confidence=confidence,
            method=f"{source}:{rule.name}",
            note="; ".join(n for n in notes if n) or (f"section: {section}" if section else ""),
        )


# --------------------------------------------------------------------------- helpers
_UNIT_REPLACEMENTS = {
    "kg/m3": "kg/m3",
    "kg/m³": "kg/m3",
    "lb/gal": "ppg",
    "lb/gal3": "lb/ft3",
    "g/cm3": "g/cm3",
    "sg": "g/cm3",
    "ftlbf": "ft.lbf",
    "ft-lbf": "ft.lbf",
    "ft.lbf": "ft.lbf",
    "inlbf": "in.lbf",
    "in-lbf": "in.lbf",
    "knm": "kN.m",
    "kn.m": "kN.m",
    "nm": "N.m",
    "meters": "m",
    "metres": "m",
    "hrs": "h",
    "hours": "h",
    "hour": "h",
    "hr": "h",
    "days": "d",
    "day": "d",
    "mins": "min",
    "minutes": "min",
    "bbls": "bbl",
    "stb": "bbl",
    "fph": "ft/hr",
    "ft/h": "ft/hr",
    "m/h": "m/hr",
    "usd": "USD",
    "$": "USD",
    "eur": "EUR",
    "gbp": "GBP",
    "nok": "NOK",
    "kips": "kip",
    "klbs": "kip",
    "lbs": "lbf",
    '"': "in",
    "'": "ft",
    "ppg/ft": "ppg/ft",
}


def _normalise_unit(captured: str | None, window: str, rule: FieldRule) -> str:
    raw = (captured or "").strip().lower()
    if raw:
        return _UNIT_REPLACEMENTS.get(raw, raw)
    # "RPM 120" / "NPT 14" - the label itself states the unit.
    lowered = window.lower()
    for keyword, unit in rule.label_unit:
        if keyword in lowered:
            return unit
    return ""


_UNIT_SLUGS = frozenset(
    {re.sub(r"[^a-z0-9]+", "", name) for name in known_units()}
    | {
        "ppg", "sg", "kgm3", "psi", "kpa", "bar", "mpa", "ft", "m", "in", "mm", "gal", "bbl",
        "m3", "l", "hr", "h", "min", "s", "rps", "deg", "count", "ratio", "kbblpd", "kbd", "aday",
        "bblpd", "gpm", "ls", "kg", "tonnes", "psiperm", "barrelperday", "ftperhour",
    }
)


def canonical_field_name(label: str, rules: Sequence[FieldRule] = _RULES) -> str | None:
    """Map a spreadsheet or form label onto the canonical field it names.

    ``"Mud weight (ppg)"`` and ``"Mud weight, avg (ppg)"`` both describe
    ``mud_weight``, so a value read from a table can be keyed, compared and displayed as
    the same field as one read from prose.  A label that merely *starts* with a field
    name but continues with something that is not a unit (``"pressure test"``,
    ``"mud weight report date"``) is deliberately not mapped: keys of genuinely
    different facts must stay different, or conflict detection compares apples to
    casing-shoe depths.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (label or "").lower()).strip("_")
    if not slug:
        return None
    best: tuple[int, str] | None = None
    for rule in rules:
        if _is_value_first(rule):
            continue
        if slug == rule.name:
            rest = ""
        elif slug.startswith(rule.name + "_"):
            rest = slug[len(rule.name) + 1 :]
        else:
            continue
        if rest and not all(part in _UNIT_SLUGS or part.isdigit() for part in rest.split("_")):
            continue
        if best is None or len(rule.name) > best[0]:
            best = (len(rule.name), rule.name)
    return best[1] if best else None


def nominal_inches(text: str) -> float:
    """``12 1/4`` -> 12.25, ``9-5/8`` -> 9.625, ``8.33`` -> 8.33."""
    value = text.strip().rstrip('"').strip()
    fraction = re.match(r"^(\d+)[- ](\d)/(\d+)$", value)
    if fraction:
        whole, num, den = fraction.groups()
        if int(den) == 0:
            raise ValueError("zero denominator")
        return float(whole) + float(num) / float(den)
    return float(value.replace(",", "."))


def _plausible(value: float, unit: str, rule: FieldRule) -> bool | None:
    """Physical plausibility gate: nonsense is flagged, never silently accepted.

    The comparison happens in :attr:`FieldRule.range_unit`, so "1.24 SG" is
    checked as 10.35 ppg against the ppg limits rather than being rejected for
    being a small number.
    """
    if rule.plausible_range is None:
        return True
    low, high = rule.plausible_range
    quantity = Quantity.of(value, resolve_unit(unit))
    target = rule.range_unit or unit
    try:
        quantity = quantity.to(target)
    except Exception:  # noqa: BLE001 - incomparable units cannot be range-checked
        return None
    return bool(low <= quantity.value <= high)


def _dedupe(fields: list[DataField]) -> list[DataField]:
    """Keep the best-confidence record per (name, value, unit, provenance ref)."""
    best: dict[tuple[str, object, str, str], DataField] = {}
    for item in fields:
        key = (item.name, item.value if not isinstance(item.value, float) else round(item.value, 6), item.unit, item.provenance.ref if item.provenance else "")
        current = best.get(key)
        if current is None or (item.confidence or 0.0) > (current.confidence or 0.0):
            best[key] = item
    return list(best.values())


def merge_field_sets(primary: Iterable[DataField], secondary: Iterable[DataField]) -> list[DataField]:
    """Combine two field lists, letting the more directly cited one win.

    A value an extractor read out of a single cell (or a MinerU structured item) is
    better evidence than the same number found again by pattern in that sheet's prose:
    the citation is exact and the unit came from a units column rather than from our
    guesswork.  So the primary list keeps its provenance and the secondary one only
    contributes fields the primary never named.
    """
    merged: list[DataField] = []
    seen: set[tuple[str, object]] = set()

    def key(item: DataField) -> tuple[str, object]:
        return item.name, item.value if not isinstance(item.value, float) else round(item.value, 6)

    for item in primary:
        seen.add(key(item))
        merged.append(item)
    for item in secondary:
        if key(item) in seen:
            continue
        seen.add(key(item))
        merged.append(item)
    return _dedupe(merged)


def known_field_names(rules: Iterable[FieldRule] = _RULES) -> list[str]:
    return sorted({rule.name for rule in rules})


def rules_as_markdown() -> str:
    """Human-readable rule catalogue (documentation source and UI tooltip)."""
    lines = ["| field | context keywords | unit rule | confidence |", "| --- | --- | --- | --- |"]
    for rule in _RULES:
        context = ", ".join(rule.context) or "-"
        unit = rule.default_unit or ("required" if rule.require_unit else "captured or convention")
        lines.append(f"| `{rule.name}` | {context} | {unit} | {rule.confidence:.2f} |")
    return "\n".join(lines)


def rules_as_dicts() -> list[dict[str, Any]]:
    """Machine-readable rule registry (shown by the AI tool ``list_extraction_rules``)."""
    out = []
    for rule in _RULES:
        out.append(
            {
                "name": rule.name,
                "dimension": rule.dimension.value if rule.dimension else "",
                "default_unit": rule.default_unit or default_unit(rule.dimension).symbol if rule.dimension else "",
                "context": list(rule.context),
                "reject": list(rule.reject),
                "confidence": rule.confidence,
                "plausible_range": list(rule.plausible_range) if rule.plausible_range else None,
                "doc": rule.doc,
            }
        )
    return out


__all__ = [
    "COMPARE_UNITS",
    "CONTEXT_WINDOW",
    "WELL_QUANTITIES",
    "FieldExtractor",
    "FieldRule",
    "canonical_field_name",
    "known_field_names",
    "merge_field_sets",
    "nominal_inches",
    "normalise_number",
    "rules_as_dicts",
    "rules_as_markdown",
]
