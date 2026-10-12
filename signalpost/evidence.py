"""Evidence capture: every `available` claim must point at ≥1 evidence record."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_evidence(
    *,
    evidence_id: str,
    source_url: str,
    source_class: str,
    claim_span: str,
    content: str = "",
    simulated: bool = False,
) -> dict:
    """Build one evidence record for the envelope.

    source_class: official | company_owned | licensed_api | public_web | simulated.
    content: the exact bytes/string the claim was read from; hashed for audit.
    """
    return {
        "id": evidence_id,
        "source_url": source_url,
        "source_class": "simulated" if simulated else source_class,
        "retrieved_at": utc_now_iso(),
        "content_sha256": sha256_text(content),
        "claim_span": claim_span,
    }
