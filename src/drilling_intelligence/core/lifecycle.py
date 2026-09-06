"""Lifecycle machines for the records that have one, in one place.

A well, a procedure, a program, a lesson, a risk, a recommendation and a promoted operational row
all move through states, and each of them needs the same three answers: which state does a new row start in, which
state may follow this one, and what to say when somebody asks for a jump that is not allowed.  The
well had its table inline in :mod:`drilling_intelligence.core.enums` and its check inline in the
repository; the six record types that arrived later share the helper instead of copying the loop,
because a transition rule that lives in six places is six chances for one of them to be lax.

Two rules the helper keeps that are easy to get wrong when each caller writes its own:

*   an unknown state is an error, never a silent pass-through - a typo in a lifecycle name would
    otherwise look like a legal transition of an unrecognised state;
*   moving to the state a record already has is an error unless the caller says it is a no-op it
    wants (``allow_same``), because "approve this approved procedure" is either idempotence or a
    double audit entry depending on whether anyone thought about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .enums import (
    LESSON_LIFECYCLE_TRANSITIONS,
    PROCEDURE_LIFECYCLE_TRANSITIONS,
    PROGRAM_LIFECYCLE_TRANSITIONS,
    RECOMMENDATION_LIFECYCLE_TRANSITIONS,
    RISK_LIFECYCLE_TRANSITIONS,
    ConfirmationStatus,
    LessonLifecycle,
    ProcedureLifecycle,
    ProgramLifecycle,
    RecommendationLifecycle,
    RiskLifecycle,
)
from .errors import ValidationError

#: How a promoted record moves between candidate, confirmed and rejected.
CONFIRMATION_TRANSITIONS: dict[ConfirmationStatus, tuple[ConfirmationStatus, ...]] = {
    ConfirmationStatus.CANDIDATE: (
        ConfirmationStatus.CONFIRMED,
        ConfirmationStatus.REJECTED,
    ),
    # Confirmed can go back: a person who confirmed a row and then found a better source has to be
    # able to say so, and a state machine with no way out of "confirmed" teaches people to never
    # confirm anything.
    ConfirmationStatus.CONFIRMED: (ConfirmationStatus.CANDIDATE, ConfirmationStatus.REJECTED),
    ConfirmationStatus.REJECTED: (ConfirmationStatus.CANDIDATE,),
}


@dataclass(frozen=True)
class Lifecycle:
    """One state machine: a name for messages, the states, the allowed edges and the first state."""

    name: str
    states: type[StrEnum]
    transitions: dict[Any, tuple[Any, ...]]
    initial: Any

    def parse(self, raw: object) -> Any:
        """Coerce ``raw`` to a state of this machine, or refuse.

        Refusing here rather than storing the string is the whole point: an unrecognised state in a
        ``status`` column is invisible until something queries for it, and by then every report has
        counted the row as "unknown".
        """
        if isinstance(raw, self.states):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValidationError(
                f"{self.name} status must not be empty",
                allowed=[state.value for state in self.states],
            )
        found = self.states.parse(text.upper().replace("-", "_").replace(" ", "_"))
        if found is None:
            found = self.states.parse(text)
        if found is None:
            raise ValidationError(
                f"{text!r} is not a {self.name} status",
                allowed=[state.value for state in self.states],
            )
        return found

    def allowed(self, current: object) -> tuple[Any, ...]:
        """What may follow ``current`` - empty for a terminal state."""
        return tuple(self.transitions.get(self.parse(current), ()))

    def require(self, current: object, target: object, *, allow_same: bool = False) -> Any:
        """Validate ``current -> target`` and return the target state, or raise.

        The error names the state the record is actually in and every state it could go to, because
        the caller of a rejected transition is nearly always a person reading a CLI message, not a
        stack trace.
        """
        now = self.parse(current)
        wanted = self.parse(target)
        if wanted is now:
            if allow_same:
                return now
            raise ValidationError(f"{self.name} is already {now.value}", status=now.value)
        allowed = self.allowed(now)
        if wanted not in allowed:
            raise ValidationError(
                f"illegal {self.name} transition {now.value} -> {wanted.value}",
                current=now.value,
                target=wanted.value,
                allowed=[state.value for state in allowed],
            )
        return wanted

    def is_terminal(self, current: object) -> bool:
        return not self.allowed(current)

    def to_dict(self) -> dict[str, Any]:
        """The machine itself, for ``doctor`` and for a UI that draws it.

        Exposed rather than kept private because a front end that hard-codes the states is a front
        end that disagrees with the platform the day a state is added.
        """
        return {
            "name": self.name,
            "initial": self.initial.value,
            "states": [state.value for state in self.states],
            "transitions": {
                state.value: [target.value for target in self.transitions.get(state, ())]
                for state in self.states
            },
        }


PROCEDURE_LIFECYCLE = Lifecycle(
    "procedure",
    ProcedureLifecycle,
    PROCEDURE_LIFECYCLE_TRANSITIONS,
    ProcedureLifecycle.DRAFT,
)
PROGRAM_LIFECYCLE = Lifecycle(
    "program",
    ProgramLifecycle,
    PROGRAM_LIFECYCLE_TRANSITIONS,
    ProgramLifecycle.DRAFT,
)
LESSON_LIFECYCLE = Lifecycle(
    "lesson",
    LessonLifecycle,
    LESSON_LIFECYCLE_TRANSITIONS,
    LessonLifecycle.DRAFT,
)
RISK_LIFECYCLE = Lifecycle(
    "risk",
    RiskLifecycle,
    RISK_LIFECYCLE_TRANSITIONS,
    RiskLifecycle.OPEN,
)
RECOMMENDATION_LIFECYCLE = Lifecycle(
    "recommendation",
    RecommendationLifecycle,
    RECOMMENDATION_LIFECYCLE_TRANSITIONS,
    RecommendationLifecycle.PROPOSED,
)
CONFIRMATION_LIFECYCLE = Lifecycle(
    "confirmation",
    ConfirmationStatus,
    CONFIRMATION_TRANSITIONS,
    ConfirmationStatus.CANDIDATE,
)

#: By table name, so an integrity check or a UI can ask which machine a row belongs to.
LIFECYCLES: dict[str, Lifecycle] = {
    "procedure_record": PROCEDURE_LIFECYCLE,
    "drilling_program": PROGRAM_LIFECYCLE,
    "lesson_learned": LESSON_LIFECYCLE,
    "risk_record": RISK_LIFECYCLE,
    "ddr_report": CONFIRMATION_LIFECYCLE,
    "well_operation": CONFIRMATION_LIFECYCLE,
    "well_event": CONFIRMATION_LIFECYCLE,
    "npt_record": CONFIRMATION_LIFECYCLE,
    "problem_occurrence": CONFIRMATION_LIFECYCLE,
    "field_pattern": CONFIRMATION_LIFECYCLE,
    "cost_item": CONFIRMATION_LIFECYCLE,
    # A best practice is approved, superseded and retired by the same rules as a procedure, so it
    # reuses that machine rather than growing a near-copy whose transitions drift a year later.
    "best_practice": PROCEDURE_LIFECYCLE,
    "recommendation": RECOMMENDATION_LIFECYCLE,
}


def lifecycle_for(table_name: str) -> Lifecycle | None:
    return LIFECYCLES.get(table_name)


__all__ = [
    "CONFIRMATION_LIFECYCLE",
    "CONFIRMATION_TRANSITIONS",
    "LESSON_LIFECYCLE",
    "LIFECYCLES",
    "PROCEDURE_LIFECYCLE",
    "PROGRAM_LIFECYCLE",
    "RECOMMENDATION_LIFECYCLE",
    "RISK_LIFECYCLE",
    "Lifecycle",
    "lifecycle_for",
]
