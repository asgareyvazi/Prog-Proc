"""Adversarial coverage for the shared table-reading helpers.

``operations/tableshape.py`` is the one module that knows *how* a stored table is walked; every
source-shaped contract (mud, BHA, bit, directional survey) reads through it.  A wrong answer here is
not a local bug - it is the same wrong answer in four domains at once, and it is invisible downstream
because the value looks perfectly ordinary by the time it reaches a row.

These tests therefore attack the helper rather than the domains: unit-like words inside column names,
annotations that are not units, ambiguous numeric punctuation, repeated headers, and the vocabulary
collisions that only appear when one domain's aliases are read by another's rules.
"""

from __future__ import annotations

import pytest

from drilling_intelligence.operations.bha import BHA_UNIT_TOKENS, COMPONENT_ALIASES
from drilling_intelligence.operations.bit_record import BIT_ALIASES, BIT_UNIT_TOKENS
from drilling_intelligence.operations.mud import DAILY_ALIASES, SUMMARY_ALIASES
from drilling_intelligence.operations.survey import STATION_ALIASES, SURVEY_UNIT_TOKENS
from drilling_intelligence.operations.tableshape import (
    DEFAULT_UNIT_TOKENS,
    alias_column,
    cell_text,
    header_index,
    header_unit,
    normalise_label,
    numeric,
    table_key,
    tables,
    without_units,
)


# ---------------------------------------------------------------- label normalisation
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  OD   (in) ", "od (in)"),
        ("Mud Weight", "mud weight"),
        ("lb/100ft²", "lb/100ft2"),
        ("Gel Strength: 10s", "gel strength 10s"),
        ("Depth, MD", "depth md"),
        ("A;B", "a b"),
        (None, ""),
        ("", ""),
        (12.5, "12.5"),
    ],
)
def test_normalise_label_folds_case_punctuation_and_superscripts(value, expected) -> None:
    assert normalise_label(value) == expected


def test_normalise_label_keeps_hyphens_and_slashes_because_they_carry_meaning() -> None:
    """``6-1/4`` and ``x-over`` are not punctuation noise; erasing them would merge distinct things."""
    assert normalise_label("6-1/4") == "6-1/4"
    assert normalise_label("X-Over sub") == "x-over sub"
    assert normalise_label("mg/l") == "mg/l"


# ---------------------------------------------------------------- unit decoration
def test_without_units_removes_a_decorated_unit_but_not_a_meaningful_word() -> None:
    assert without_units("OD (in)", BHA_UNIT_TOKENS) == "od"
    assert without_units("Length ft", BHA_UNIT_TOKENS) == "length"
    assert without_units("Length", BHA_UNIT_TOKENS) == "length"
    # "in" is a unit token, but inside a word it is part of the name.
    assert without_units("Inclination", SURVEY_UNIT_TOKENS) == "inclination"
    assert without_units("Inner diameter", BHA_UNIT_TOKENS) == "inner diameter"


def test_without_units_prefers_the_longest_unit_token() -> None:
    """``inches`` must not be half-matched as ``in``, which would leave a stray ``ches``."""
    assert without_units("OD inches", DEFAULT_UNIT_TOKENS) == "od"
    assert without_units("OD in", DEFAULT_UNIT_TOKENS) == "od"
    assert without_units("Degrees", DEFAULT_UNIT_TOKENS) == ""


# ---------------------------------------------------------------- header indexing
def test_repeated_headers_keep_the_first_spelling_and_never_consult_the_second() -> None:
    headers = header_index(["Description", "Make", "Description (mm)"], strip_units=True)
    assert headers["description"] == 0, "guessing between two 'Description' columns is arbitrary"
    # The second column's own label is still addressable by its distinct text.
    assert headers["make"] == 1


def test_strip_units_indexes_the_decorated_and_bare_spellings_to_the_same_column() -> None:
    headers = header_index(["No", "Description", "OD (in)", "Length (ft)"], strip_units=True)
    assert headers["od (in)"] == 2 and headers["od"] == 2
    assert headers["length (ft)"] == 3 and headers["length"] == 3


def test_header_index_is_empty_for_a_header_row_of_blanks() -> None:
    assert header_index([None, "", "   "], strip_units=True) == {}


# ---------------------------------------------------------------- unit extraction
@pytest.mark.parametrize(
    ("header", "units", "expected"),
    [
        ("OD (in)", BHA_UNIT_TOKENS, "in"),
        ("ID (in)", BHA_UNIT_TOKENS, "in"),
        ("Length (ft)", BHA_UNIT_TOKENS, "ft"),
        ("Size (in)", BIT_UNIT_TOKENS, "in"),
        ("Depth In (ft)", BIT_UNIT_TOKENS, "ft"),
        ("Depth Out (ft)", BIT_UNIT_TOKENS, "ft"),
        ("Footage (ft)", BIT_UNIT_TOKENS, "ft"),
        ("MD (ft)", SURVEY_UNIT_TOKENS, "ft"),
        ("MD (m)", SURVEY_UNIT_TOKENS, "m"),
        ("Inclination (deg)", SURVEY_UNIT_TOKENS, "deg"),
        ("DLS (deg/100ft)", SURVEY_UNIT_TOKENS, "deg/100ft"),
        ("MW in (ppg)", DEFAULT_UNIT_TOKENS, "ppg"),
    ],
)
def test_header_unit_reads_the_unit_the_source_delimited(header, units, expected) -> None:
    assert header_unit(header, "", units) == expected


@pytest.mark.parametrize(
    "header",
    [
        "Remarks (optional)",
        "Qty (approx)",
        "Description (from tally sheet)",
        "Date (as reported by driller)",
        "Nozzle Size (3 x 16)",
        "Serial No (S/N)",
        "Notes (see overleaf)",
    ],
)
def test_header_unit_refuses_a_bracketed_annotation_that_is_not_a_unit(header) -> None:
    """A clarification the source printed is not a unit, and must not be stored as one.

    This is not cosmetic.  A measurement's quality is derived from whether its unit is known, so
    accepting any bracketed text would mark a value *verified* while storing a sentence in a
    16-character unit column - a confident, wrong answer with no diagnostic behind it.
    """
    assert header_unit(header, "", BHA_UNIT_TOKENS) == ""
    assert header_unit(header, "", BIT_UNIT_TOKENS) == ""
    assert header_unit(header, "", SURVEY_UNIT_TOKENS) == ""


def test_header_unit_does_not_read_a_unit_from_the_column_name() -> None:
    """The reported ``Depth In (ft)`` defect: ``in`` here is the column's name, not its unit."""
    assert header_unit("Depth In (ft)", "", BIT_UNIT_TOKENS) == "ft"
    # With no delimited unit at all there is nothing to read, and nothing may be assumed.
    assert header_unit("Depth In", "", BIT_UNIT_TOKENS) == ""
    assert header_unit("Depth In ft", "", BIT_UNIT_TOKENS) == ""


def test_header_unit_never_supplies_the_unit_a_property_conventionally_uses() -> None:
    """No parenthetical means no unit, whatever unit this property usually travels with."""
    assert header_unit("Mud Weight", "", DEFAULT_UNIT_TOKENS) == ""
    assert header_unit("True Vertical Depth", "", SURVEY_UNIT_TOKENS) == ""
    assert header_unit("Outside Diameter", "", BHA_UNIT_TOKENS) == ""


def test_header_unit_returns_the_callers_default_only_when_it_states_no_unit() -> None:
    """The default is the caller's fallback, not a unit the helper discovered."""
    assert header_unit("Mud Weight", "ppg", DEFAULT_UNIT_TOKENS) == "ppg"
    assert header_unit("Mud Weight (cP)", "ppg", DEFAULT_UNIT_TOKENS) == "cP"


def test_header_unit_returns_the_default_for_empty_and_none_headers() -> None:
    assert header_unit(None, "x", DEFAULT_UNIT_TOKENS) == "x"
    assert header_unit("", "x", DEFAULT_UNIT_TOKENS) == "x"
    assert header_unit("()", "", DEFAULT_UNIT_TOKENS) == ""


# ---------------------------------------------------------------- numeric parsing
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (8.5, 8.5),
        (3, 3.0),
        ("8.5", 8.5),
        ("  8.5  ", 8.5),
        ("-3", -3.0),
        ("+3", 3.0),
        ("1,234", 1234.0),
        ("1,234,567.5", 1234567.5),
        ("10.5 ppg", 10.5),
        ("10.5ppg", 10.5),
        ("9000 ft", 9000.0),
    ],
)
def test_numeric_parses_values_the_source_stated(value, expected) -> None:
    assert numeric(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "n/a",
        "TBD",
        "=SUM(A1:A2)",
        "=AVERAGE(C2:C5)",
        "6-1/4",
        "8 1/2",
        "1e3",
        ".5",
        "5.",
        "12 inches approx",
        True,
        False,
        # Ambiguous punctuation: twelve-and-a-half in much of the world, 125 if the comma is
        # silently dropped.  Neither reading may be chosen on the reader's behalf.
        "12,5",
        "1,2,3",
        "1,23",
    ],
)
def test_numeric_refuses_anything_it_cannot_read_unambiguously(value) -> None:
    assert numeric(value) is None


def test_numeric_accepts_a_thousands_separator_only_in_a_valid_grouping() -> None:
    assert numeric("1,234") == 1234.0
    assert numeric("12,3456") is None
    assert numeric("1234,567") is None


def test_numeric_honours_the_vocabulary_it_is_given() -> None:
    assert numeric("10.5 ppg", ("ppg",)) == 10.5
    assert numeric("10.5 ppg", ("cP",)) is None
    assert numeric("90 deg", SURVEY_UNIT_TOKENS) == 90.0


def test_numeric_does_not_convert_units_it_recognises() -> None:
    """A value in feet stays the number of feet; recognising the unit is not licence to change it."""
    assert numeric("30 m", DEFAULT_UNIT_TOKENS) == 30.0
    assert numeric("30 ft", DEFAULT_UNIT_TOKENS) == 30.0


# ---------------------------------------------------------------- cell reading
def test_cell_text_is_empty_for_an_absent_column_rather_than_raising() -> None:
    assert cell_text(["a", "b"], -1) == ""
    assert cell_text(["a", "b"], 2) == ""
    assert cell_text(["a", None], 1) == ""
    assert cell_text(["  a  "], 0) == "a"
    assert cell_text([12.5], 0) == "12.5"


# ---------------------------------------------------------------- alias resolution
def test_alias_column_consults_exact_labels_in_the_order_the_contract_listed_them() -> None:
    headers = header_index(["Bit No", "Bit Size", "Size"], strip_units=True)
    assert alias_column(headers, BIT_ALIASES["bit_number"]) == 0
    # BIT_ALIASES["size"] lists "size" before "bit size", so the exact column called "Size" wins.
    # Which spelling wins is the contract's decision, not the reader's.
    assert BIT_ALIASES["size"][0] == "size"
    assert alias_column(headers, BIT_ALIASES["size"]) == 2
    # Reverse the order and the answer follows the contract, which is the whole point.
    assert alias_column(headers, ("bit size", "size")) == 1


def test_prefix_matching_is_opt_in_because_it_turns_one_column_into_another() -> None:
    headers = header_index(["Bit size", "Bit number"])
    # Exact-only: "bit" alone names neither column.
    assert alias_column(headers, ("bit",)) == -1
    # Opted in, it does - which is exactly why contracts that do not need it must not ask for it.
    assert alias_column(headers, ("bit",), prefix=True) == 0


def test_a_prefix_match_requires_a_word_boundary() -> None:
    headers = header_index(["MW in (ppg)", "MW out (ppg)"])
    assert alias_column(headers, DAILY_ALIASES["mud_weight_in"], prefix=True, strip_units=False) == 0
    assert alias_column(headers, DAILY_ALIASES["mud_weight_out"], prefix=True, strip_units=False) == 1
    # "Mud Weight(ppg)Active" is the active system, not a daily sample: no boundary, no match.
    glued = header_index(["Mud Weight(ppg)Active"])
    assert alias_column(glued, ("mud weight", "mw"), prefix=True, strip_units=False) == -1


def test_the_bit_vocabulary_does_not_confuse_the_bit_with_its_size_or_run() -> None:
    headers = header_index(
        ["Bit No", "Run", "Size (in)", "Depth In (ft)", "Depth Out (ft)", "Footage (ft)"],
        strip_units=True,
    )
    assert alias_column(headers, BIT_ALIASES["bit_number"]) == 0
    assert alias_column(headers, BIT_ALIASES["run_number"]) == 1
    assert alias_column(headers, BIT_ALIASES["size"]) == 2
    assert alias_column(headers, BIT_ALIASES["depth_in"]) == 3
    assert alias_column(headers, BIT_ALIASES["depth_out"]) == 4
    assert alias_column(headers, BIT_ALIASES["footage"]) == 5


def test_the_bha_vocabulary_does_not_confuse_od_id_and_length() -> None:
    headers = header_index(
        ["No", "Description", "OD (in)", "ID (in)", "Length (ft)", "Qty"], strip_units=True
    )
    assert alias_column(headers, COMPONENT_ALIASES["description"]) == 1
    assert alias_column(headers, COMPONENT_ALIASES["od"]) == 2
    assert alias_column(headers, COMPONENT_ALIASES["inner_diameter"]) == 3
    assert alias_column(headers, COMPONENT_ALIASES["length"]) == 4
    assert alias_column(headers, COMPONENT_ALIASES["quantity"]) == 5


def test_the_survey_vocabulary_keeps_md_tvd_and_the_angles_apart() -> None:
    headers = header_index(
        ["Station", "MD (ft)", "TVD (ft)", "Inclination (deg)", "Azimuth (deg)", "DLS (deg/100ft)"],
        strip_units=True,
    )
    assert alias_column(headers, STATION_ALIASES["station"]) == 0
    assert alias_column(headers, STATION_ALIASES["md"]) == 1
    assert alias_column(headers, STATION_ALIASES["tvd"]) == 2
    assert alias_column(headers, STATION_ALIASES["inclination"]) == 3
    assert alias_column(headers, STATION_ALIASES["azimuth"]) == 4
    assert alias_column(headers, STATION_ALIASES["dls"]) == 5


def test_a_column_no_contract_declares_is_never_matched() -> None:
    """The vocabulary is closed: an unrecognised column is absent, not approximately present."""
    headers = header_index(["Something Else", "Another Thing"])
    for aliases in (*BIT_ALIASES.values(), *COMPONENT_ALIASES.values(), *STATION_ALIASES.values()):
        assert alias_column(headers, aliases) == -1, aliases


def test_the_mud_summary_vocabulary_resolves_its_closed_alias_set() -> None:
    headers = header_index(["Mud weight (ppg)", "PV (cP)", "Depth MD (ft)"], strip_units=True)
    assert alias_column(headers, ("mud weight", "mw")) == 0
    assert alias_column(headers, ("plastic viscosity", "pv")) == 1
    assert len(SUMMARY_ALIASES) > 10, "the summary vocabulary is a declared set, not a guess"


# ---------------------------------------------------------------- table identity
def test_table_key_identifies_a_table_by_position_not_by_object() -> None:
    assert table_key({"table_id": "t1", "sheet": "S", "anchor": "A1", "page": 2}) == "t1|S|A1|2"
    assert table_key({"table_id": "t1"}) == "t1|||"
    assert table_key({}) == "|||"
    # Two equal dicts are the same table even though they are different objects.
    assert table_key({"table_id": "t1"}) == table_key({"table_id": "t1"})


def test_tables_returns_stored_tables_in_stored_order_and_ignores_junk() -> None:
    payload = {"tables": [{"table_id": "a"}, "not a table", None, {"table_id": "b"}]}
    assert [table["table_id"] for table in tables(payload)] == ["a", "b"]
    assert tables({}) == []
    assert tables({"tables": None}) == []


def test_tables_copies_so_a_caller_cannot_mutate_the_stored_artefact() -> None:
    stored = {"table_id": "a"}
    payload = {"tables": [stored]}
    result = tables(payload)
    result[0]["table_id"] = "mutated"
    assert stored["table_id"] == "a"


# ---------------------------------------------------------------- mud header unit vocabulary
def test_the_mud_header_vocabulary_covers_the_units_a_mud_column_states() -> None:
    """A mud column states its unit in the header, not behind each number.

    ``_MUD_VALUE_UNITS`` is the vocabulary for a unit *inside a cell* (``"10.2 ppg"``); it has no
    ``ft``, ``m``, ``in`` or ``h``.  A header check run against that narrower list silently dropped the
    unit of a depth, hole-size or time column, and because promotion marks a measurement ``VALID``
    exactly when it has a source unit, the loss downgraded a real measurement to ``UNVERIFIED``.  The
    generated corpus does not exercise those headers, so only this test stands between the bug and a
    workspace.
    """
    from drilling_intelligence.operations import mud

    stated = {
        "Mud Weight (ppg)": "ppg",
        "Depth MD (ft)": "ft",
        "Depth (m)": "m",
        "Hole Size (in)": "in",
        "Time (h)": "h",
        "PV (cP)": "cP",
        "Solids (%)": "%",
        "Chlorides (mg/l)": "mg/l",
        "Yield Point (lb/100ft2)": "lb/100ft2",
        "ECD Gradient (psi/ft)": "psi/ft",
        "Volume (bbl)": "bbl",
    }
    for header, expected in stated.items():
        assert mud.header_unit(header, "") == expected, header


def test_the_mud_header_vocabulary_still_refuses_an_annotation() -> None:
    """Widening the vocabulary must not reopen the door it was widened to keep shut."""
    from drilling_intelligence.operations import mud

    for header in (
        "Remarks (optional)",
        "Date (as reported by driller)",
        "Notes (see attached)",
        "Mud Weight",
        "Mud Weight ppg",
    ):
        assert mud.header_unit(header, "") == "", header


def test_the_mud_header_vocabulary_is_a_closed_union_not_an_open_rule() -> None:
    """The fix widens an explicit list; it does not accept any bracketed word."""
    from drilling_intelligence.operations import mud
    from drilling_intelligence.operations.tableshape import DEFAULT_UNIT_TOKENS

    assert set(mud._MUD_HEADER_UNITS) == set(mud._MUD_LABEL_UNITS) | set(DEFAULT_UNIT_TOKENS)
    # A word that is in neither vocabulary is not a unit, however plausible it reads.
    assert mud.header_unit("Density (approximately)", "") == ""
    assert mud.header_unit("Depth (true vertical)", "") == ""
