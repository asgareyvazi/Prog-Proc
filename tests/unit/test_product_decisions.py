"""The decision register: five decisions, each with the parts an answer needs.

`docs/PRODUCT_DECISIONS_REQUIRED.md` is the record of what the owner still has
to decide.  Two things about it are checked here, because both have gone wrong
before:

* the **count**.  The register holds five open decisions and a separate list
  of excluded capabilities.  The end-to-end report said six for a long time,
  because the excluded list was counted as a decision.
* the **fail-closed behavior**.  Every decision says what the code does
  today, and the code has to do that today - a decision that is open may not
  be quietly implemented.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "PRODUCT_DECISIONS_REQUIRED.md"
DECISIONS = ROOT / "docs" / "DECISIONS.md"

#: The parts every decision carries, so that it can be answered from the
#: register alone.
REQUIRED_PARTS = (
    "**Missing decision.**",
    "**Current behavior.**",
    "**Safe default.**",
    "**What is blocked.**",
    "**Trade-off.**",
    "**Acceptance evidence before code.**",
    "**Source.**",
)

#: The five open decisions, in the order the register lists them.
EXPECTED_TITLES = (
    "OpenAI-compatible adapter",
    "Embedding sidecar",
    "Skills runtime",
    "Additional calculation method",
    "Additional document-class writer",
)


def _register_text() -> str:
    return REGISTER.read_text(encoding="utf-8")


def _sections() -> list[tuple[str, str]]:
    """The decision sections of the register, as (title, body) pairs."""
    text = _register_text()
    parts = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    return [
        (section.splitlines()[0].strip(), section)
        for section in parts
        if re.match(r"^\d+\. ", section)
    ]


def test_the_register_holds_five_open_decisions() -> None:
    sections = _sections()
    assert len(sections) == 5, [title for title, _body in sections]
    titles = tuple(title.split(". ", 1)[1] for title, _body in sections)
    assert titles == EXPECTED_TITLES


def test_every_decision_carries_the_parts_an_answer_needs() -> None:
    for title, body in _sections():
        for part in REQUIRED_PARTS:
            assert part in body, f"{title}: {part}"


def test_every_decision_points_at_an_adr_and_at_code_that_exists() -> None:
    adrs = set(re.findall(r"^## (ADR-\d+)", DECISIONS.read_text(encoding="utf-8"), re.MULTILINE))
    for title, body in _sections():
        cited = set(re.findall(r"ADR-\d+", body))
        assert cited, title
        for number in cited:
            assert number in adrs, f"{title}: {number} is not in DECISIONS.md"
        for path in re.findall(r"`((?:src|tests|docs)/[A-Za-z0-9_./-]+)`", body):
            assert (ROOT / path).exists(), f"{title}: {path}"


def test_the_excluded_capabilities_are_listed_and_not_counted_as_decisions() -> None:
    text = _register_text()
    assert "## Still excluded" in text
    for excluded in (
        "Risk scores",
        "currency conversion",
        "AFE rules",
        "plan-versus-actual dashboards",
        "inferred survey geometry",
        "Authentication, roles and multi-user server behavior",
    ):
        assert excluded in text, excluded
    # The excluded list is not a decision: it has no number in front of it.
    assert not re.search(r"^## \d+\. Still excluded", text, re.MULTILINE)


def test_the_reports_no_longer_claim_a_different_number_of_decisions() -> None:
    """The original sentence stays; the correction has to sit next to it."""
    for name in ("END_TO_END_FINAL_REPORT.md", "END_TO_END_PROGRESS.md"):
        path = ROOT / "docs" / "mission-reports" / name
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if "product decisions remain open" not in line:
                continue
            window = "\n".join(lines[index : index + 8])
            assert "Correction (V8.17" in window, f"{name}:{index + 1} has no correction"
            assert "five" in window, f"{name}:{index + 1} does not state the count"


def test_the_openai_compatible_provider_is_still_unsupported_and_offline() -> None:
    """Decision 1 is open, so the name must not open a socket."""
    from drilling_intelligence.integrations.ai.contract import ProviderState, configured_status

    class _Config:
        provider = "openai-compatible"
        openai_model = "gpt-4o-mini"
        openai_endpoint = "https://api.openai.com/v1"
        endpoint = ""
        model = ""
        embedding_model = ""

    status = configured_status(_Config())
    assert status.state is ProviderState.UNSUPPORTED
    assert "no request is sent" in status.reason


def test_the_vector_store_is_still_the_unavailable_one() -> None:
    """Decision 2 is open, so keyword search stays the only retrieval path."""
    from drilling_intelligence.integrations.ai.contract import (
        ProviderState,
        UnavailableVectorStore,
    )

    status = UnavailableVectorStore().status()
    assert status.state is ProviderState.UNSUPPORTED


def test_no_service_reads_the_skill_tables() -> None:
    """Decision 3 is open, so the tables stay declared and unused."""
    import ast

    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        if path.name in {"models.py", "integrity.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            if any(name in {"Skill", "SkillVersion"} for name in names):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, offenders
