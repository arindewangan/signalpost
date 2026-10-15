"""Per-company / per-claim availability states.

Matches the Signalpost output contract: every input company produces exactly one
terminal envelope; each claim carries one of the six states below.
"""

from __future__ import annotations

from enum import Enum


class CompanyState(str, Enum):
    """The six terminal availability states for a claim."""

    AVAILABLE = "available"          # you found it
    NOT_AVAILABLE = "not_available"  # you looked, there is nothing there
    BLOCKED = "blocked"              # the source refused the request
    NOT_APPLICABLE = "not_applicable"  # the question does not apply
    AMBIGUOUS = "ambiguous"          # could not be sure it is the right company
    FAILED = "failed"                # the run broke on this one


_ALL_STATES = tuple(s.value for s in CompanyState)


def coerce_state(value: str) -> CompanyState:
    """Normalize a raw string to a CompanyState; raises ValueError if unknown."""
    try:
        return CompanyState(str(value).strip().lower())
    except ValueError:
        raise ValueError(
            f"unknown availability state {value!r}; expected one of {list(_ALL_STATES)}"
        ) from None


def is_terminal(state: CompanyState) -> bool:
    """Every state in the contract is terminal — the company got a final answer."""
    return state in CompanyState


def missing_value_ok(state: CompanyState) -> bool:
    """True when the state is a *valid answer* for 'found nothing'.

    These states satisfy the contract's rule: a missing row is not scorable, but
    'I found nothing' (any of these states) is.
    """
    return state in (
        CompanyState.NOT_AVAILABLE,
        CompanyState.BLOCKED,
        CompanyState.NOT_APPLICABLE,
        CompanyState.AMBIGUOUS,
    )


# ---------------------------------------------------------------------------
# Evaluation-contract validation gates
# (https://builderr.ai/docs/signalpost-evaluation-harness.md — official-run
# checks: exactly one terminal envelope per input; valid availability states;
# every published claim backed by evidence; missing values never fabricated.)
# ---------------------------------------------------------------------------

def validate_claim(claim: dict) -> list[str]:
    """Check one claim dict against the output contract.

    Returns a list of violation messages (empty => the claim is valid).
    """
    violations: list[str] = []
    field = claim.get("field", "?")
    if not claim.get("field"):
        violations.append("claim missing field name")
    if claim.get("availability") not in _ALL_STATES:
        violations.append(
            f"claim {field}: invalid availability state {claim.get('availability')!r}"
        )
    confidence = claim.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        violations.append(f"claim {field}: confidence {confidence!r} not in [0, 1]")
    if not isinstance(claim.get("evidence_ids"), list):
        violations.append(f"claim {field}: evidence_ids is not a list")
    return violations
