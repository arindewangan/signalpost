"""Availability-state classifier + exact-entity match gate.

Doctrine (from the challenge): it is better to miss information than to publish
it under the wrong company. When the entity match is uncertain, the claim is
AMBIGUOUS — never AVAILABLE on a guess.
"""

from __future__ import annotations

import re
from typing import Any

from .states import CompanyState


def _spaced_org_pattern(org: str) -> re.Pattern | None:
    """Match the 9-digit org number with optional separators between digit
    groups — the format Norwegian sites print in footers/contact pages
    (e.g. '923 609 016', '923.609.016', '923-609-016'). Boundaries keep it
    exact: it only matches this org's own digits, not a longer digit run."""
    if not org or not re.fullmatch(r"\d{9}", org):
        return None
    parts = [org[0:3], org[3:6], org[6:9]]
    return re.compile(r"(?<!\d)" + r"[\s.\-/]?".join(parts) + r"(?!\d)")


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

    Passes when the evidence contains the 9-digit organisation number with
    digit boundaries (verbatim, or printed with separators as Norwegian sites
    do: "Org.nr 923 609 016") or the exact legal name. A longer digit run
    that merely contains the org's digits is NOT a match. Similar-but-
    different names fail — they go through the AMBIGUOUS path in
    `classify_field`.
    """
    if not evidence_text:
        return False
    pattern = _spaced_org_pattern(org or "")
    if pattern is not None and pattern.search(evidence_text):
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
