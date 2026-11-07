"""The state machine is what stops a fix from skipping verification or review."""

import pytest

from remediator.state import TRANSITIONS, InvalidTransition, Run, RunState


def run_in(state: RunState) -> Run:
    return Run(id="run-1", repo="acme/app", target="GHSA-xxxx", state=state)


ALLOWED = [(start, end) for start, ends in TRANSITIONS.items() for end in ends]
FORBIDDEN = [
    (start, end) for start in RunState for end in RunState if end not in TRANSITIONS[start]
]


def test_every_state_has_rules() -> None:
    assert set(TRANSITIONS) == set(RunState)


def test_new_run_starts_detected_with_no_history() -> None:
    run = Run(id="run-1", repo="acme/app", target="GHSA-xxxx")

    assert run.state is RunState.DETECTED
    assert run.history == []


@pytest.mark.parametrize(("start", "end"), ALLOWED)
def test_allowed_moves_work(start: RunState, end: RunState) -> None:
    run = run_in(start)

    run.transition(end)

    assert run.state is end


@pytest.mark.parametrize(("start", "end"), FORBIDDEN)
def test_forbidden_moves_raise_and_change_nothing(start: RunState, end: RunState) -> None:
    run = run_in(start)

    with pytest.raises(InvalidTransition):
        run.transition(end)

    assert run.state is start
    assert run.history == []


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (RunState.BUMPED, RunState.REVIEWED),  # Skips verification.
        (RunState.BUMPED, RunState.PR_OPENED),  # Skips verification and review.
        (RunState.MIGRATING, RunState.PR_OPENED),  # Skips verification and review.
        (RunState.VERIFIED, RunState.PR_OPENED),  # Skips review.
        (RunState.REVIEWED, RunState.MERGED),  # Merges without a pull request.
    ],
)
def test_safety_steps_cannot_be_skipped(start: RunState, end: RunState) -> None:
    with pytest.raises(InvalidTransition):
        run_in(start).transition(end)


@pytest.mark.parametrize(
    "state",
    [
        RunState.SUPPRESSED,
        RunState.REPORTED,
        RunState.MERGED,
        RunState.CLOSED,
        RunState.ESCALATED,
    ],
)
def test_final_states_are_finished(state: RunState) -> None:
    assert run_in(state).is_finished


def test_unfinished_state_is_not_finished() -> None:
    assert not run_in(RunState.PLANNED).is_finished


def test_a_full_fix_records_its_history() -> None:
    path = [
        RunState.TRIAGED,
        RunState.REACHABILITY_ASSESSED,
        RunState.PLANNED,
        RunState.BASELINE_VERIFIED,
        RunState.BUMPED,
        RunState.MIGRATING,
        RunState.VERIFIED,
        RunState.REVIEWED,
        RunState.MIGRATING,  # The reviewer rejected the change.
        RunState.VERIFIED,
        RunState.REVIEWED,
        RunState.PR_OPENED,
        RunState.MERGED,
    ]
    run = Run(id="run-1", repo="acme/app", target="GHSA-xxxx")

    for state in path:
        run.transition(state, reason=f"moved to {state}")

    assert run.is_finished
    assert [step.to_state for step in run.history] == path
    assert run.history[0].from_state is RunState.DETECTED
    assert run.history[0].reason == "moved to triaged"
    assert all(step.at.tzinfo is not None for step in run.history)


def test_a_saved_run_can_resume_where_it_stopped() -> None:
    run = run_in(RunState.DETECTED)
    run.transition(RunState.TRIAGED, reason="deduplicated")

    restored = Run.model_validate_json(run.model_dump_json())

    assert restored == run
    restored.transition(RunState.REACHABILITY_ASSESSED)
    assert restored.state is RunState.REACHABILITY_ASSESSED
