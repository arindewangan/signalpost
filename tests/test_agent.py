"""Agent contract: one terminal envelope per input — including invalid rows,
registry misses, blocked sources, and transport failures."""

import json

import pytest

from signalpost.agent import ResearchAgent
from signalpost.batch import BatchInput, read_batch
from signalpost.report import build_report, self_score
from signalpost.sources import BlockedError, BrregSource, GoneError, SourceError


def _agent(**kw):
    kw.setdefault("live", False)
    kw.setdefault("run_id", "t")
    return ResearchAgent(source=BrregSource(**{k: v for k, v in kw.items()
                                               if k in ("live", "fixture_data")}),
                         run_id=kw["run_id"], simulated=True)


def test_one_envelope_per_input_always():
    agent = _agent()
    inputs = ["917755736", "920100000", "999999999", "not-an-org", ""]
    envelopes = [agent.research(BatchInput(organisation_number=i if i != "not-an-org" and i != "" else "")) for i in inputs]
    assert len(envelopes) == len(inputs)
    for envelope in envelopes:
        assert envelope["run"]["terminal_status"] in ("completed", "failed")
        assert isinstance(envelope["claims"], list) and envelope["claims"]
        assert isinstance(envelope["evidence"], list)
        assert isinstance(envelope["changes"], list)
        assert isinstance(envelope["errors"], list)


def test_known_company_has_available_name_with_evidence():
    envelope = _agent().research(BatchInput(organisation_number="917755736"))
    name = next(c for c in envelope["claims"] if c["field"] == "legal_name")
    assert name["availability"] == "available"
    assert name["value"] == "SPAREBANK 1 SMN"
    assert name["evidence_ids"], "available claims must cite evidence"
    assert envelope["simulated"] is True


def test_unknown_company_is_not_available_not_failed():
    envelope = _agent().research(BatchInput(organisation_number="999999999"))
    assert envelope["run"]["terminal_status"] == "completed"
    name = next(c for c in envelope["claims"] if c["field"] == "legal_name")
    assert name["availability"] == "not_available"
    assert name["value"] is None


def test_invalid_org_number_failed_envelope():
    envelope = _agent().research(BatchInput(organisation_number=""))
    assert envelope["run"]["terminal_status"] == "failed"
    assert envelope["organisation_number"] == ""
    assert envelope["errors"]


def test_blocked_source_maps_to_blocked_claims():
    class BlockedSource(BrregSource):
        def fetch_entity(self, org):
            raise BlockedError("HTTP 429")

    agent = ResearchAgent(source=BlockedSource(live=False), run_id="t", simulated=True)
    envelope = agent.research(BatchInput(organisation_number="917755736"))
    assert envelope["run"]["terminal_status"] == "completed"
    accounts = next(c for c in envelope["claims"] if c["field"] == "financials_latest")
    assert accounts["availability"] == "blocked"


def test_gone_maps_to_not_available_with_note():
    class GoneSource(BrregSource):
        def fetch_entity(self, org):
            raise GoneError("410")

    agent = ResearchAgent(source=GoneSource(live=False), run_id="t", simulated=True)
    envelope = agent.research(BatchInput(organisation_number="917755736"))
    assert envelope["run"]["terminal_status"] == "completed"
    assert all(c["availability"] == "not_available" for c in envelope["claims"])
    assert any("410" in e for e in envelope["errors"])


def test_transport_error_maps_to_failed_envelope():
    class BrokenSource(BrregSource):
        def fetch_entity(self, org):
            raise SourceError("boom")

    agent = ResearchAgent(source=BrokenSource(live=False), run_id="t", simulated=True)
    envelope = agent.research(BatchInput(organisation_number="917755736"))
    assert envelope["run"]["terminal_status"] == "failed"
    assert envelope["errors"]


def test_report_counts_and_self_score():
    agent = _agent()
    envelopes = [agent.research(BatchInput(organisation_number=o))
                 for o in ("917755736", "920100000", "999999999")]
    report = build_report(run_id="t", envelopes=envelopes,
                          started_iso="x", completed_iso="y")
    assert report["companies"] == 3
    assert report["third_party_cost_usd"] == 0.0
    assert sum(report["claim_state_counts"].values()) == report["claims_total"]
    score = report["self_score"]
    assert 0 <= score["total"] <= 100
    # Every available claim has valid evidence -> precision gate passes.
    assert score["components"]["precision_gate_ok"] is True


def test_main_cli_end_to_end(tmp_path, monkeypatch):
    from signalpost import main as cli
    batch = tmp_path / "batch.jsonl"
    batch.write_text('{"organisation_number": "917755736"}\n"999999999"\n"bad"\n',
                     encoding="utf-8")
    out = tmp_path / "envelopes.jsonl"
    report_path = tmp_path / "report.json"
    monkeypatch.chdir(tmp_path)
    code = cli.main([
        "--organisations", str(batch),
        "--output", str(out),
        "--report", str(report_path),
        "--run-id", "test-run",
        "--dry-run",
        "--workers", "1",
    ])
    assert code == 0
    envelopes = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(envelopes) == 3
    assert [e["run"]["terminal_status"] for e in envelopes] == ["completed", "completed", "failed"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run_id"] == "test-run" and report["companies"] == 3
