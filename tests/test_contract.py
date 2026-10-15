"""Evaluation-contract validation gates (Day-3).

Encodes the official-run checks from
https://builderr.ai/docs/signalpost-evaluation-harness.md into
states.py / report.py validation gates:
- exactly one terminal envelope per input company (no missing rows);
- every `available` claim backed by resolvable, auditable evidence
  (source + retrieval time + valid sha256);
- non-available claims carry no value (missing values never fabricated);
- evidence ids unique; claim states valid; confidence in [0, 1].
"""

import copy

from signalpost.agent import ResearchAgent
from signalpost.batch import BatchInput
from signalpost.classifier import evidence_refs_entity
from signalpost.report import validate_envelope, validate_run
from signalpost.sources import BrregSource
from signalpost.states import validate_claim


def _good_claim():
    return {
        "field": "legal_name",
        "value": "TESTBEDRIFT AS",
        "availability": "available",
        "confidence": 0.99,
        "evidence_ids": ["ev-00001"],
    }


def _good_envelope():
    agent = ResearchAgent(source=BrregSource(live=False), run_id="t", simulated=True)
    return agent.research(BatchInput(organisation_number="917755736"))


# --------------------------------------------------------------------------
# validate_claim
# --------------------------------------------------------------------------

def test_validate_claim_accepts_good_claim():
    assert validate_claim(_good_claim()) == []


def test_validate_claim_rejects_unknown_state():
    claim = _good_claim()
    claim["availability"] = "maybe"
    assert validate_claim(claim)


def test_validate_claim_rejects_bad_confidence():
    for bad in (-0.1, 1.5, "high", None, True):
        claim = _good_claim()
        claim["confidence"] = bad
        assert validate_claim(claim), f"confidence {bad!r} should be rejected"


def test_validate_claim_accepts_confidence_bounds():
    for ok in (0.0, 1.0, 0.5):
        claim = _good_claim()
        claim["confidence"] = ok
        assert validate_claim(claim) == [], f"confidence {ok!r} should be accepted"


def test_validate_claim_rejects_missing_evidence_ids():
    claim = _good_claim()
    del claim["evidence_ids"]
    assert validate_claim(claim)


# --------------------------------------------------------------------------
# validate_envelope
# --------------------------------------------------------------------------

def test_validate_envelope_accepts_agent_envelope():
    envelope = _good_envelope()
    violations = validate_envelope(envelope)
    assert violations == [], violations


def test_validate_envelope_available_claim_needs_evidence():
    envelope = _good_envelope()
    claim = next(c for c in envelope["claims"] if c["availability"] == "available")
    claim["evidence_ids"] = []
    assert validate_envelope(envelope)


def test_validate_envelope_available_claim_needs_resolvable_evidence():
    envelope = _good_envelope()
    claim = next(c for c in envelope["claims"] if c["availability"] == "available")
    claim["evidence_ids"] = ["ev-does-not-exist"]
    violations = validate_envelope(envelope)
    assert any("unknown evidence id" in v for v in violations)


def test_validate_envelope_non_available_claim_must_not_carry_value():
    """Anti-fabrication gate: a not_available claim with a value is a
    contract violation (missing values are never silently converted)."""
    envelope = _good_envelope()
    claim = next(c for c in envelope["claims"] if c["availability"] != "available")
    claim["value"] = 0  # the classic silent zero-fill
    violations = validate_envelope(envelope)
    assert any("carries a value" in v for v in violations)


def test_validate_envelope_rejects_duplicate_evidence_ids():
    envelope = _good_envelope()
    envelope["evidence"] = envelope["evidence"] + copy.deepcopy(envelope["evidence"][:1])
    violations = validate_envelope(envelope)
    assert any("duplicate evidence id" in v for v in violations)


def test_validate_envelope_rejects_bad_evidence_hash():
    envelope = _good_envelope()
    record = envelope["evidence"][0]
    record["content_sha256"] = "not-a-sha256"
    violations = validate_envelope(envelope)
    assert any("bad sha256" in v for v in violations)


def test_validate_envelope_rejects_bad_terminal_status():
    envelope = _good_envelope()
    envelope["run"]["terminal_status"] = "in_progress"
    assert validate_envelope(envelope)


# --------------------------------------------------------------------------
# validate_run: one envelope per input, no missing rows
# --------------------------------------------------------------------------

def test_validate_run_accepts_matching_batch():
    envelopes = [_good_envelope()]
    assert validate_run(envelopes, ["917755736"]) == []


def test_validate_run_flags_missing_row():
    envelopes = [_good_envelope()]
    violations = validate_run(envelopes, ["917755736", "920100000"])
    assert any("missing envelopes" in v for v in violations)


def test_validate_run_flags_duplicate_envelopes():
    envelopes = [_good_envelope(), _good_envelope()]
    violations = validate_run(envelopes, ["917755736"])
    assert any("duplicate envelopes" in v for v in violations)


def test_validate_run_accepts_failed_envelope_for_invalid_input():
    """An invalid input still gets exactly one (failed) envelope — the empty
    org number is the honest value, not a contract violation."""
    agent = ResearchAgent(source=BrregSource(live=False), run_id="t", simulated=True)
    envelope = agent.research(BatchInput(organisation_number=""))
    assert envelope["organisation_number"] == ""
    assert envelope["run"]["terminal_status"] == "failed"
    assert validate_run([envelope], [""]) == []


# --------------------------------------------------------------------------
# Exact-entity gate: org number printed with separators still names THIS
# entity; other digit runs do not.
# --------------------------------------------------------------------------

def test_spaced_org_number_still_matches_entity():
    for text in (
        "Org.nr 923 609 016",
        "Organisasjonsnummer: 923.609.016",
        "org.nr. 923-609-016",
        "Foretaksregisteret 923609016",
    ):
        assert evidence_refs_entity("923609016", "EQUINOR ASA", text), text


def test_spaced_org_number_does_not_match_other_digits():
    assert not evidence_refs_entity("923609016", "EQUINOR ASA", "Org.nr 111 222 333")
    # A longer digit run containing the org digits is not an exact match.
    assert not evidence_refs_entity("923609016", "EQUINOR ASA", "ref 19236090167")
    assert not evidence_refs_entity("923609016", "EQUINOR ASA", "")


def test_exact_legal_name_still_matches():
    assert evidence_refs_entity("923609016", "EQUINOR ASA", "Velkommen til Equinor ASA")
    assert not evidence_refs_entity("923609016", "EQUINOR ASA", "Velkommen til Equinor")
