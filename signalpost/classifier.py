"""Availability-state classifier + exact-entity match gate.

Doctrine (from the challenge): it is better to miss information than to publish
it under the wrong company. When the entity match is uncertain, the claim is
AMBIGUOUS — never AVAILABLE on a guess.
"""

from __future__ import annotations

from typing import Any

from .states import CompanyState


def exact_entity_match(registry_name: str | None, evidence_text: str) -> bool:
    """True when the evidence names the exact legal entity.

    Conservative rule: the full registry name must appear verbatim (case
    insensitive) in the evidence text. First-token/substring matches are NOT
    enough — parent/brand/franchise and similarly named companies fail this
    gate and go through the AMBIGUOUS path instead of becoming AVAILABLE.
    """
    if not registry_name or not evidence_text:
        return False
    hay = evidence_text.upper()
    name = registry_name.strip().upper()
    return bool(name) and name in hay


def evidence_refs_entity(org: str, registry_name: str | None, evidence_text: str) -> bool:
    """True when the evidence text refers to THIS exact legal entity.

    Passes when the evidence contains the 9-digit organisation number verbatim
    (strongest, self-anchoring) or the exact legal name. Similar-but-different
    names fail — they go through the AMBIGUOUS path in `classify_field`.
    """
    if not evidence_text:
        return False
    if org and org in evidence_text:
        return True
    return exact_entity_match(registry_name, evidence_text)


def classify_field(
    *,
    value: Any,
    source_checked: bool,
    source_blocked: bool = False,
    applies: bool = True,
    entity_match_ok: bool = True,
) -> tuple[CompanyState, float]:
    """Map fetch outcome -> (state, confidence). Returns (state, 0.0..1.0)."""
    if not applies:
        return CompanyState.NOT_APPLICABLE, 1.0
    if source_blocked:
        return CompanyState.BLOCKED, 1.0
    if not source_checked:
        return CompanyState.FAILED, 0.0  # the run broke before checking
    if not entity_match_ok:
        return CompanyState.AMBIGUOUS, 0.5
    if value is None or value == "" or value == []:
        # Checked and empty: "looked, nothing there". Never zero-filled.
        return CompanyState.NOT_AVAILABLE, 0.9
    return CompanyState.AVAILABLE, 0.99
