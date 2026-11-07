"""The remediation state machine from section 6 of the design doc.

Control flow is plain code so it can be tested, audited and resumed after a crash.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RunState(StrEnum):
    DETECTED = "detected"
    TRIAGED = "triaged"
    SUPPRESSED = "suppressed"
    REACHABILITY_ASSESSED = "reachability_assessed"
    REPORTED = "reported"
    PLANNED = "planned"
    BASELINE_VERIFIED = "baseline_verified"
    BUMPED = "bumped"
    MIGRATING = "migrating"
    VERIFIED = "verified"
    REVIEWED = "reviewed"
    PR_OPENED = "pr_opened"
    MERGED = "merged"
    CLOSED = "closed"
    ESCALATED = "escalated"


TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.DETECTED: frozenset({RunState.TRIAGED}),
    RunState.TRIAGED: frozenset({RunState.SUPPRESSED, RunState.REACHABILITY_ASSESSED}),
    RunState.REACHABILITY_ASSESSED: frozenset({RunState.REPORTED, RunState.PLANNED}),
    RunState.PLANNED: frozenset({RunState.BASELINE_VERIFIED}),
    RunState.BASELINE_VERIFIED: frozenset({RunState.ESCALATED, RunState.BUMPED}),
    RunState.BUMPED: frozenset({RunState.VERIFIED, RunState.MIGRATING}),
    RunState.MIGRATING: frozenset({RunState.VERIFIED, RunState.ESCALATED}),
    RunState.VERIFIED: frozenset({RunState.REVIEWED}),
    RunState.REVIEWED: frozenset({RunState.MIGRATING, RunState.PR_OPENED}),
    RunState.PR_OPENED: frozenset({RunState.MERGED, RunState.CLOSED}),
    RunState.SUPPRESSED: frozenset(),
    RunState.REPORTED: frozenset(),
    RunState.MERGED: frozenset(),
    RunState.CLOSED: frozenset(),
    RunState.ESCALATED: frozenset(),
}


class InvalidTransition(Exception):
    """A run was asked to move to a state its current state does not allow."""


class Transition(BaseModel):
    from_state: RunState
    to_state: RunState
    at: datetime
    reason: str = ""


class Run(BaseModel):
    """One remediation: one repository and one advisory or upgrade target."""

    id: str
    repo: str
    target: str
    state: RunState = RunState.DETECTED
    history: list[Transition] = Field(default_factory=list[Transition])

    @property
    def is_finished(self) -> bool:
        return not TRANSITIONS[self.state]

    def transition(self, to_state: RunState, reason: str = "") -> None:
        if to_state not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"cannot go from {self.state} to {to_state}")
        self.history.append(
            Transition(
                from_state=self.state,
                to_state=to_state,
                at=datetime.now(UTC),
                reason=reason,
            )
        )
        self.state = to_state
