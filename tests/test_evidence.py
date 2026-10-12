"""Evidence records: required fields, real sha256, valid retrieved_at."""

import hashlib
from datetime import datetime, timezone

from signalpost.evidence import make_evidence, sha256_text, utc_now_iso


def test_evidence_record_fields():
    content = '{"navn": "SPAREBANK 1 SMN"}'
    record = make_evidence(
        evidence_id="ev-00001",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/917755736",
        source_class="official",
        claim_span="registry record for org 917755736",
        content=content,
    )
    assert record["id"] == "ev-00001"
    assert record["source_url"].startswith("https://")
    assert record["source_class"] == "official"
    assert record["claim_span"]
    assert record["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert len(record["content_sha256"]) == 64
    # retrieved_at is a real, recent ISO timestamp
    ts = datetime.fromisoformat(record["retrieved_at"].replace("Z", "+00:00"))
    delta = abs((datetime.now(timezone.utc) - ts).total_seconds())
    assert delta < 60


def test_simulated_labeling():
    record = make_evidence(
        evidence_id="ev-1", source_url="x", source_class="official",
        claim_span="y", content="z", simulated=True,
    )
    assert record["source_class"] == "simulated"


def test_sha256_helper():
    assert sha256_text("abc") == hashlib.sha256(b"abc").hexdigest()
    assert utc_now_iso().endswith("Z")
