"""Day-2: sub-units, konsernstruktur, accounts list-shape, jobs, blocked mapping."""

import json

import pytest
import requests

from signalpost.agent import (
    ALL_CLAIM_FIELDS,
    FIELD_JOBS,
    FIELD_KONSERN,
    FIELD_ROLES,
    FIELD_SUBUNITS,
    ResearchAgent,
)
from signalpost.batch import BatchInput
from signalpost.sources import (
    BlockedError,
    BrregSource,
    WebsiteOutcome,
    WebsiteSource,
)
from signalpost.states import CompanyState


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        raise requests.HTTPError(f"HTTP {self.status_code}")


class StubSource(BrregSource):
    """BrregSource with canned _get responses keyed by URL fragment."""

    def __init__(self, canned):
        super().__init__(live=True, min_interval=0.0)
        self.canned = canned

    def _get(self, url):
        from urllib.parse import urlparse

        self.requests_made += 1
        path = urlparse(url).path

        def _resp(fragment):
            resp = self.canned[fragment]
            if isinstance(resp, Exception):
                raise resp
            return resp

        # Path-suffix matches win (disambiguates /enheter/{org} vs /enheter/{org}/roller).
        for fragment in sorted(self.canned, key=len, reverse=True):
            if path.endswith(fragment):
                return _resp(fragment)
        # Substring fallback for endpoints whose path ends in the org number.
        for fragment in sorted(self.canned, key=len, reverse=True):
            if fragment.strip("/") and fragment in path:
                return _resp(fragment)
        return FakeResp(404, {})


def _entity(org="123456789"):
    return {
        "organisasjonsnummer": org,
        "navn": "TESTBEDRIFT AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "hjemmeside": "https://example.com",
        "erIKonsern": True,
    }


def test_accounts_list_shape_picks_latest_year():
    records = [
        {"regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
         "valuta": "NOK",
         "resultatregnskapResultat": {
             "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 100.0},
                                "driftsresultat": 10.0},
             "aarsresultat": 5.0},
         "eiendeler": {"sumEiendeler": 1000.0},
         "egenkapitalGjeld": {"sumEgenkapitalGjeld": 1000.0}},
        {"regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
         "valuta": "NOK",
         "resultatregnskapResultat": {
             "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 200.0},
                                "driftsresultat": 20.0},
             "aarsresultat": 15.0},
         "eiendeler": {"sumEiendeler": 2000.0},
         "egenkapitalGjeld": {"sumEgenkapitalGjeld": 2000.0}},
    ]
    summary = ResearchAgent._summarize_accounts(records)
    assert summary["fiscal_year"] == "2024-12-31"
    assert summary["revenue"] == 200.0
    assert summary["operating_result"] == 20.0
    assert summary["net_result"] == 15.0
    assert summary["total_assets"] == 2000.0
    assert ResearchAgent._summarize_accounts([]) is None
    assert ResearchAgent._summarize_accounts(None) is None


def test_accounts_empty_list_is_not_available_not_zero():
    canned = {
        "/enheter/123456789": FakeResp(200, _entity()),
        "/regnskap/123456789": FakeResp(200, []),
        "/roller": FakeResp(404, {}),
        "/underenheter": FakeResp(200, {"_embedded": {"underenheter": []}}),
        "/konsernstruktur/": FakeResp(404, {}),
    }
    agent = ResearchAgent(source=StubSource(canned), run_id="t", simulated=False)
    envelope = agent.research(BatchInput(organisation_number="123456789"))
    accounts = next(c for c in envelope["claims"] if c["field"] == "financials_latest")
    assert accounts["availability"] == CompanyState.NOT_AVAILABLE.value
    assert accounts["value"] is None  # never zero-filled


def test_subunits_and_konsern_available_with_evidence():
    subunits_payload = {
        "_embedded": {"underenheter": [
            {"organisasjonsnummer": "111111111", "navn": "TESTBEDRIFT AVD OSLO",
             "beliggenhetsadresse": {"adresse": "Gate 1", "postnummer": "0001",
                                     "poststed": "OSLO"}},
            # production returns adresse as a LIST of lines
            {"organisasjonsnummer": "444444444", "navn": "TESTBEDRIFT AVD BERGEN",
             "beliggenhetsadresse": {"adresse": ["Brygge 2", "Etasje 3"],
                                     "postnummer": "5003", "poststed": "BERGEN"}},
        ]}
    }
    konsern_payload = {"children": [
        {"organisasjonsnummer": "222222222", "navn": "TESTBEDRIFT DATTER AS",
         "knytningsform": {"beskrivelse": "Konsern datter", "kode": "KDAT"},
         "nivaa": 1, "children": [
             {"organisasjonsnummer": "333333333", "navn": "DATTERDATTER AS",
              "knytningsform": {"beskrivelse": "Konsern datter", "kode": "KDAT"},
              "nivaa": 2},
         ]},
    ]}
    canned = {
        "/enheter/123456789": FakeResp(200, _entity()),
        "/regnskap/123456789": FakeResp(404, {}),
        "/roller": FakeResp(404, {}),
        "/underenheter": FakeResp(200, subunits_payload),
        "/konsernstruktur/": FakeResp(200, konsern_payload),
    }
    agent = ResearchAgent(source=StubSource(canned), run_id="t", simulated=False)
    envelope = agent.research(BatchInput(organisation_number="123456789"))
    subs = next(c for c in envelope["claims"] if c["field"] == FIELD_SUBUNITS)
    assert subs["availability"] == CompanyState.AVAILABLE.value
    assert subs["value"][0]["organisasjonsnummer"] == "111111111"
    assert subs["value"][1]["adresse"] == "Brygge 2, Etasje 3, 5003, BERGEN"
    assert len(subs["evidence_ids"]) == 1
    ks = next(c for c in envelope["claims"] if c["field"] == FIELD_KONSERN)
    assert ks["availability"] == CompanyState.AVAILABLE.value
    assert ks["value"]["total_subsidiaries"] == 2  # recursive flatten
    assert ks["value"]["in_konsern"] is True
    assert len(ks["evidence_ids"]) == 1
    # every emitted field is present
    assert {c["field"] for c in envelope["claims"]} == set(ALL_CLAIM_FIELDS)


def test_roles_blocked_maps_to_blocked_not_failed():
    canned = {
        "/enheter/123456789": FakeResp(200, _entity()),
        "/regnskap/123456789": FakeResp(404, {}),
        "/roller": FakeResp(429, {}),
        "/underenheter": FakeResp(200, {"_embedded": {"underenheter": []}}),
        "/konsernstruktur/": FakeResp(404, {}),
    }
    agent = ResearchAgent(source=StubSource(canned), run_id="t", simulated=False)
    envelope = agent.research(BatchInput(organisation_number="123456789"))
    roles = next(c for c in envelope["claims"] if c["field"] == FIELD_ROLES)
    assert roles["availability"] == CompanyState.BLOCKED.value


class StubCareersWebsite:
    """Stubbed website source: homepage links to /karriere with canned HTML."""

    def __init__(self, homepage_html, careers_html):
        self.homepage_html = homepage_html
        self.careers_html = careers_html
        self.requests_made = 0

    def fetch(self, url, org, legal_name):
        self.requests_made += 1
        return WebsiteOutcome.MATCHED, f"About TESTBEDRIFT AS. Org.nr {org}."

    def fetch_careers(self, homepage_url, org, legal_name):
        self.requests_made += 2
        combined = self.careers_html
        anchored = org in combined or legal_name.upper() in combined.upper()
        if not anchored:
            return WebsiteOutcome.FETCHED_NO_REFERENCE, "no reference"
        from signalpost.sources import WebsiteSource as _WS
        jobs = _WS.extract_job_titles(combined)
        return WebsiteOutcome.MATCHED, {
            "jobs": jobs, "source_page": homepage_url + "/karriere",
            "page_text": combined[:20000],
        }


def _agent_with_website(website):
    canned = {
        "/enheter/123456789": FakeResp(200, _entity()),
        "/regnskap/123456789": FakeResp(404, {}),
        "/roller": FakeResp(404, {}),
        "/underenheter": FakeResp(200, {"_embedded": {"underenheter": []}}),
        "/konsernstruktur/": FakeResp(404, {}),
    }
    return ResearchAgent(source=StubSource(canned), website_source=website,
                         run_id="t", simulated=False)


def test_jobs_available_when_careers_page_is_entity_anchored():
    website = StubCareersWebsite(
        "<html></html>",
        "<html><title>Karriere i TESTBEDRIFT AS</title>"
        "<h1>Ledige stillinger hos TESTBEDRIFT AS, org 123456789</h1>"
        "<h2>Senior utvikler</h2><h2>Designer</h2></html>",
    )
    envelope = _agent_with_website(website).research(BatchInput(organisation_number="123456789"))
    jobs = next(c for c in envelope["claims"] if c["field"] == FIELD_JOBS)
    assert jobs["availability"] == CompanyState.AVAILABLE.value
    assert jobs["value"]["count"] >= 2
    assert len(jobs["evidence_ids"]) == 1
    record = next(r for r in envelope["evidence"] if r["id"] == jobs["evidence_ids"][0])
    assert record["source_class"] == "company_owned"


def test_jobs_ambiguous_when_careers_page_not_entity_anchored():
    website = StubCareersWebsite(
        "<html></html>",
        "<html><title>Jobs at Some Other Corp</title><h1>Open roles</h1></html>",
    )
    envelope = _agent_with_website(website).research(BatchInput(organisation_number="123456789"))
    jobs = next(c for c in envelope["claims"] if c["field"] == FIELD_JOBS)
    assert jobs["availability"] == CompanyState.AMBIGUOUS.value
    assert jobs["value"] is None
    assert jobs["evidence_ids"] == []


def test_find_careers_links_and_title_extraction():
    html = ('<a href="/karriere">Karriere</a>'
            '<a href="https://other.example/jobs">x</a>'
            '<a href="/om-oss">Om oss</a>')
    links = WebsiteSource().find_careers_links(html, "https://example.com")
    assert links == ["https://example.com/karriere"]
    titles = WebsiteSource.extract_job_titles(
        "<html><title>T</title><h1>Utvikler</h1><h2>Designer</h2></html>")
    assert titles == ["Utvikler", "Designer", "T"]


def test_get_retries_transient_transport_errors():
    calls = {"n": 0}

    class Flaky(BrregSource):
        def __init__(self):
            super().__init__(live=True, min_interval=0.0)

    src = Flaky()
    real_get = src.session.get

    def flaky_get(url, timeout=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("proxy hiccup")
        return FakeResp(200, {"ok": True})

    src.session.get = flaky_get
    resp = src._get("https://data.brreg.no/x")
    assert resp.status_code == 200
    assert calls["n"] == 3
    src.session.get = real_get


def test_html_report_renders_companies_with_evidence_links():
    from signalpost.html_report import render

    envelope = {
        "organisation_number": "123456789",
        "run": {"run_id": "t", "terminal_status": "completed"},
        "claims": [
            {"field": "legal_name", "value": "TESTBEDRIFT AS",
             "availability": "available", "confidence": 0.99,
             "evidence_ids": ["ev-00001"]},
            {"field": "open_jobs", "value": None, "availability": "ambiguous",
             "confidence": 0.5, "evidence_ids": []},
        ],
        "evidence": [
            {"id": "ev-00001", "source_url": "https://data.brreg.no/x",
             "source_class": "official", "retrieved_at": "2026-10-12T00:00:00Z",
             "content_sha256": "a" * 64, "claim_span": "registry record"},
        ],
        "changes": [], "errors": [],
    }
    profile = {"organisation_number": "123456789", "summary": "Test summary.",
               "unknown_fields": ["open_jobs"]}
    report = {"run_id": "t", "companies": 1,
              "claim_state_counts": {"available": 1, "ambiguous": 1},
              "requests_total": 3, "runtime_ms_total": 100,
              "third_party_cost_usd": 0.0,
              "self_score": {"total": 70.0}}
    page = render([envelope], [profile], report)
    assert "123456789" in page
    assert "https://data.brreg.no/x" in page
    assert "2026-10-12T00:00:00Z" in page  # source date shown
    assert "aaaaaaaaaaaaaaaa" in page  # sha256 fragment shown
    assert "ambiguous" in page


def test_roles_parses_production_rollegrupper_shape():
    payload = {
        "rollegrupper": [
            {"roller": [
                {"avregistrert": False,
                 "person": {"erDoed": False, "fodselsdato": "1968-05-04",
                            "navn": {"fornavn": "Anders", "etternavn": "Opedal"}},
                 "type": {"beskrivelse": "Daglig leder", "kode": "DAGL"}},
                {"avregistrert": True,  # deregistered: skipped
                 "person": {"navn": {"fornavn": "Gammel", "etternavn": "Rolle"}},
                 "type": {"beskrivelse": "Styremedlem"}},
                {"avregistrert": False,
                 "person": {"erDoed": True,  # deceased: skipped
                            "navn": {"fornavn": "Død", "etternavn": "Person"}},
                 "type": {"beskrivelse": "Styremedlem"}},
            ]}
        ]
    }
    canned = {
        "/enheter/123456789": FakeResp(200, _entity()),
        "/regnskap/123456789": FakeResp(404, {}),
        "/roller": FakeResp(200, payload),
        "/underenheter": FakeResp(200, {"_embedded": {"underenheter": []}}),
        "/konsernstruktur/": FakeResp(404, {}),
    }
    agent = ResearchAgent(source=StubSource(canned), run_id="t", simulated=False)
    envelope = agent.research(BatchInput(organisation_number="123456789"))
    roles = next(c for c in envelope["claims"] if c["field"] == FIELD_ROLES)
    assert roles["availability"] == CompanyState.AVAILABLE.value
    assert roles["value"] == [{"type": "Daglig leder", "name": "Anders Opedal"}]
    # birth dates never stored
    assert "1968-05-04" not in json.dumps(roles["value"])
    assert len(roles["evidence_ids"]) == 1


def test_website_url_without_scheme_is_normalized():
    from signalpost.sources import _normalize_url

    assert _normalize_url("www.example.no") == "https://www.example.no"
    assert _normalize_url("https://example.no/x") == "https://example.no/x"
    assert _normalize_url("") == ""


def test_website_fetch_retries_transient_error_then_succeeds():
    import requests as _rq

    ws = WebsiteSource()
    calls = {"n": 0}

    class Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        raw = None

        def __init__(self, text):
            self._text = text.encode()

        @property
        def raw(self):
            outer = self

            class Raw:
                def read(self, n, decode_content=True):
                    return outer._text

            return Raw()

    def flaky(url, timeout=None, stream=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rq.ConnectionError("proxy hiccup")
        return Resp("<html>TESTBEDRIFT AS org 123456789</html>")

    ws.session.get = flaky
    ws._robots_disallows = lambda url: False
    outcome, text = ws.fetch("https://example.no", "123456789", "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.MATCHED
    assert calls["n"] == 2
