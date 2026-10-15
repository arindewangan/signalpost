"""Refresh idempotence: same snapshot => zero changes; no duplicate evidence."""

from signalpost.agent import ResearchAgent
from signalpost.batch import BatchInput
from signalpost.report import compute_changes, load_snapshot, merge_evidence, replay
from signalpost.sources import BrregSource


def _run(orgs=("917755736", "920100000")):
    agent = ResearchAgent(source=BrregSource(live=False), run_id="t", simulated=True)
    return [agent.research(BatchInput(organisation_number=o)) for o in orgs]


def _snapshot(envelopes):
    return {e["organisation_number"]: e for e in envelopes}


def test_same_snapshot_zero_changes():
    first = _run()
    previous = _snapshot(first)
    second = _run()
    replay(previous, second)
    for envelope in second:
        assert envelope["changes"] == [], envelope["organisation_number"]


def test_material_change_detected():
    first = _run()
    second = _run()
    # Simulate a material change: a name appeared where there was none.
    for envelope in second:
        if envelope["organisation_number"] == "920100000":
            for claim in envelope["claims"]:
                if claim["field"] == "email":
                    claim["value"] = "post@eksempel.no"
                    claim["availability"] = "available"
                    claim["evidence_ids"] = [envelope["evidence"][0]["id"]]
    previous = _snapshot(first)
    changes = compute_changes(previous, _snapshot(second))
    changed = changes["920100000"]
    assert len(changed) == 1
    assert changed[0]["field"] == "email"
    assert changed[0]["old_availability"] == "not_available"
    assert changed[0]["new_availability"] == "available"
    # The untouched company has no changes.
    assert changes["917755736"] == []


def test_new_company_is_not_a_change():
    previous = _snapshot(_run(orgs=("917755736",)))
    second = _run(orgs=("917755736", "920100000"))
    changes = compute_changes(previous, _snapshot(second))
    assert changes["920100000"] == []


def test_evidence_merge_no_duplicates():
    first = _run()
    previous = _snapshot(first)
    second = _run()
    before = {e["organisation_number"]: len(e["evidence"]) for e in second}
    replay(previous, second)
    for envelope in second:
        org = envelope["organisation_number"]
        ids = [r["id"] for r in envelope["evidence"]]
        assert len(ids) == len(set(ids)), "duplicate evidence records"
        assert len(envelope["evidence"]) >= before[org]


def test_list_reordering_is_not_a_material_change():
    """The registry API may return the same sub-units/roles in a different
    order between runs — that is not a material change (false-change rate).
    Regression test for the Day-3 live replay finding (934930150)."""
    first = _run()
    second = _run()
    for envelope in second:
        for claim in envelope["claims"]:
            if isinstance(claim.get("value"), list):
                claim["value"] = list(reversed(claim["value"]))
    previous = _snapshot(first)
    changes = compute_changes(previous, _snapshot(second))
    for org, org_changes in changes.items():
        assert org_changes == [], (org, org_changes)


def test_load_snapshot_roundtrip(tmp_path):
    envelopes = _run()
    path = tmp_path / "prev.jsonl"
    import json
    path.write_text("\n".join(json.dumps(e) for e in envelopes), encoding="utf-8")
    snapshot = load_snapshot(str(path))
    assert set(snapshot) == {"917755736", "920100000"}
    assert snapshot["917755736"]["claims"]
