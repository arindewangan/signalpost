"""Entity-match gate: exact org number/name => available path;
similar-but-different names => ambiguous, never available."""

from signalpost.agent import FIELD_FOOTPRINT, ResearchAgent
from signalpost.batch import BatchInput
from signalpost.classifier import classify_field, evidence_refs_entity, exact_entity_match
from signalpost.sources import BrregSource, WebsiteOutcome
from signalpost.states import CompanyState


def test_exact_name_matches():
    assert exact_entity_match("SPAREBANK 1 SMN", "Welcome to SPAREBANK 1 SMN, org 917755736")
    assert exact_entity_match("EKSEMPELBEDRIFT AS", "ekseMPelbedrift as")  # case-insensitive


def test_similar_name_does_not_match():
    assert not exact_entity_match("SPAREBANK 1 SMN", "SPAREBANK 1 SR-BANK is a different bank")
    assert not exact_entity_match("OLA NORDMANN AS", "OLA NORDMANNEN AS")
    assert not exact_entity_match(None, "anything")
    assert not exact_entity_match("NAME AS", "")


def test_org_number_is_strongest_anchor():
    assert evidence_refs_entity("917755736", "SOMETHING ELSE AS",
                                "Contact us. Org.nr 917755736.")
    assert not evidence_refs_entity("917755736", "SPAREBANK 1 SMN",
                                     "A page about a different company entirely.")
    assert not evidence_refs_entity("917755736", "SPAREBANK 1 SMN", "")


def test_gate_allows_exact_evidence():
    s, _ = classify_field(value="x", source_checked=True,
                          entity_match_ok=evidence_refs_entity(
                              "917755736", "SPAREBANK 1 SMN", "SPAREBANK 1 SMN 917755736"))
    assert s is CompanyState.AVAILABLE


def test_gate_forces_ambiguous_on_uncertain_match():
    s, _ = classify_field(value="x", source_checked=True,
                          entity_match_ok=evidence_refs_entity(
                              "917755736", "SPAREBANK 1 SMN", "about SR-BANK"))
    assert s is CompanyState.AMBIGUOUS


class _AmbiguousWebsite:
    """Website fetched fine but contains no reference to the company."""

    def fetch(self, url, org, legal_name):
        return WebsiteOutcome.FETCHED_NO_REFERENCE, "generic landing page"


def test_website_without_reference_is_ambiguous_not_available():
    fixture = {
        "917755736": {
            "navn": "SPAREBANK 1 SMN",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
            "hjemmeside": "https://www.smn.no",
        }
    }
    agent = ResearchAgent(source=BrregSource(live=False, fixture_data=fixture),
                          website_source=_AmbiguousWebsite(),
                          run_id="t", simulated=False)
    envelope = agent.research(BatchInput(organisation_number="917755736"))
    footprint = next(c for c in envelope["claims"] if c["field"] == FIELD_FOOTPRINT)
    assert footprint["availability"] == CompanyState.AMBIGUOUS.value
    assert footprint["value"] is None
    assert footprint["evidence_ids"] == []


class _MatchedWebsite:
    def __init__(self):
        self.requests_made = 0

    def fetch(self, url, org, legal_name):
        self.requests_made += 1
        return WebsiteOutcome.MATCHED, f"About us. {legal_name}. Org.nr {org}."


def test_website_with_exact_reference_is_available():
    fixture = {
        "917755736": {
            "navn": "SPAREBANK 1 SMN",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
            "hjemmeside": "https://www.smn.no",
        }
    }
    agent = ResearchAgent(source=BrregSource(live=False, fixture_data=fixture),
                          website_source=_MatchedWebsite(),
                          run_id="t", simulated=False)
    envelope = agent.research(BatchInput(organisation_number="917755736"))
    footprint = next(c for c in envelope["claims"] if c["field"] == FIELD_FOOTPRINT)
    assert footprint["availability"] == CompanyState.AVAILABLE.value
    assert footprint["value"] == "https://www.smn.no"
    assert len(footprint["evidence_ids"]) == 1
    record = next(r for r in envelope["evidence"] if r["id"] == footprint["evidence_ids"][0])
    assert record["source_class"] == "company_owned"
