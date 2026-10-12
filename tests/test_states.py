"""The six availability states: classifier decisions for every state."""

from signalpost.classifier import classify_field
from signalpost.states import CompanyState, coerce_state, is_terminal, missing_value_ok


def test_all_six_states_decided():
    # available
    s, c = classify_field(value="SpareBank 1 SMN", source_checked=True)
    assert s is CompanyState.AVAILABLE and c > 0.9
    # not_available: checked and empty (never zero-filled)
    s, _ = classify_field(value=None, source_checked=True)
    assert s is CompanyState.NOT_AVAILABLE
    s, _ = classify_field(value="", source_checked=True)
    assert s is CompanyState.NOT_AVAILABLE
    s, _ = classify_field(value=[], source_checked=True)
    assert s is CompanyState.NOT_AVAILABLE
    # blocked: the source refused
    s, _ = classify_field(value=None, source_checked=True, source_blocked=True)
    assert s is CompanyState.BLOCKED
    # not_applicable: the question does not apply
    s, _ = classify_field(value=None, source_checked=True, applies=False)
    assert s is CompanyState.NOT_APPLICABLE
    # ambiguous: entity match uncertain
    s, _ = classify_field(value="Some AS", source_checked=True, entity_match_ok=False)
    assert s is CompanyState.AMBIGUOUS
    # failed: the run broke before checking
    s, _ = classify_field(value=None, source_checked=False)
    assert s is CompanyState.FAILED


def test_value_zero_is_available_not_missing():
    # A genuine zero (e.g. 0 employees reported) is data, not "nothing found".
    s, _ = classify_field(value=0, source_checked=True)
    assert s is CompanyState.AVAILABLE


def test_state_contract_helpers():
    assert coerce_state("Available") is CompanyState.AVAILABLE
    assert all(is_terminal(s) for s in CompanyState)
    # "found nothing" states satisfy the contract; available/failed do not.
    for s in (CompanyState.NOT_AVAILABLE, CompanyState.BLOCKED,
              CompanyState.NOT_APPLICABLE, CompanyState.AMBIGUOUS):
        assert missing_value_ok(s)
    assert not missing_value_ok(CompanyState.AVAILABLE)
    assert not missing_value_ok(CompanyState.FAILED)
