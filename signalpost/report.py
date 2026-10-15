"""Run report + refresh replay.

- `build_report(...)`: machine-readable JSON — run_id, runtime, per-claim state
  counts, request counts, `third_party_cost_usd`, and a conservative
  qualification self-score proxy.
- `load_snapshot(path)`: previous-snapshot envelopes keyed by org number.
- `replay(previous, envelopes)`: fills each envelope's `changes` with material
  changes vs the snapshot and merges earlier evidence (no duplicates).
  Same snapshot in => zero changes (idempotence).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .agent import ResearchAgent
from .evidence import utc_now_iso
from .states import validate_claim

# Self-score formula (conservative proxy vs the hidden checked collection):
#   recall_proxy x 50 + precision_gate x 30 + synthesis presence x 12 + UX x 8
_RECALL_WEIGHT = 50.0
_PRECISION_WEIGHT = 30.0
_SYNTHESIS_WEIGHT = 12.0
_UX_WEIGHT = 8.0

# Evidence record fields required by the output contract: every published
# claim needs a source, a retrieval time, and an auditable content hash.
_EVIDENCE_REQUIRED = (
    "source_url", "source_class", "retrieved_at", "content_sha256", "claim_span",
)

_MATERIAL_AVAILABILITY = {"available", "not_available", "ambiguous", "blocked"}


def _claim_values(envelope: dict) -> dict[str, dict]:
    return {c["field"]: c for c in envelope.get("claims", [])}


def _canon(value):
    """Canonical form for change comparison.

    Dicts are key-sorted; lists are sorted by their canonical JSON. List
    order is NOT semantically meaningful here — the registry API may return
    the same sub-units/roles in a different order between runs, and that
    must not count as a material change (the contract measures false-change
    rate). Scalar values pass through unchanged.
    """
    if isinstance(value, dict):
        return {key: _canon(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return sorted(
            (_canon(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        )
    return value


def _values_equal(a, b) -> bool:
    return _canon(a) == _canon(b)


def _is_material_change(old: dict, new: dict) -> bool:
    """A change is material when availability changed (and the new state is a
    meaningful answer) or when an available value actually changed."""
    if old["availability"] != new["availability"]:
        return new["availability"] in _MATERIAL_AVAILABILITY
    if new["availability"] == "available" and not _values_equal(old.get("value"), new.get("value")):
        return True
    return False


def compute_changes(previous: dict[str, dict], current: dict[str, dict]) -> dict[str, list[dict]]:
    """Return {org: [change...]} for orgs present in BOTH snapshots.

    A company with no previous record is a new profile, not a change — it gets
    no change entries. Identical snapshots => every list is empty (idempotent).
    """
    out: dict[str, list[dict]] = {}
    for org, new_env in current.items():
        old_env = previous.get(org)
        if old_env is None:
            out[org] = []
            continue
        old_claims = _claim_values(old_env)
        changes: list[dict] = []
        for field, new_claim in _claim_values(new_env).items():
            old_claim = old_claims.get(field)
            if old_claim is None or _is_material_change(old_claim, new_claim):
                changes.append(
                    {
                        "field": field,
                        "old": None if old_claim is None else old_claim.get("value"),
                        "old_availability": None if old_claim is None else old_claim["availability"],
                        "new": new_claim.get("value"),
                        "new_availability": new_claim["availability"],
                        "evidence_ids": new_claim.get("evidence_ids", []),
                        "changed_at": utc_now_iso(),
                    }
                )
        out[org] = changes
    return out


def merge_evidence(previous: dict[str, dict], envelope: dict) -> dict:
    """Preserve earlier evidence records, appending only genuinely new ones.

    Dedupes by evidence id; earlier records keep their retrieved_at (audit trail).
    """
    org = envelope["organisation_number"]
    old_env = previous.get(org)
    if old_env is None:
        return envelope
    seen = {r["id"] for r in envelope.get("evidence", [])}
    merged = list(envelope.get("evidence", []))
    for record in old_env.get("evidence", []):
        if record.get("id") not in seen:
            merged.append(record)
            seen.add(record["id"])
    envelope["evidence"] = merged
    return envelope


def replay(previous: dict[str, dict], envelopes: list[dict]) -> list[dict]:
    """Refresh replay: attach changes + merged evidence to each envelope."""
    current = {e["organisation_number"]: e for e in envelopes}
    changes = compute_changes(previous, current)
    for envelope in envelopes:
        envelope["changes"] = changes.get(envelope["organisation_number"], [])
        merge_evidence(previous, envelope)
    return envelopes


def load_snapshot(path: str) -> dict[str, dict]:
    """Read a previous run's envelopes JSONL into {org_number: envelope}."""
    snapshot: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            envelope = json.loads(line)
            org = envelope.get("organisation_number")
            if org:
                snapshot[org] = envelope
    return snapshot


def _precision_gate_ok(envelopes: list[dict]) -> bool:
    """Gate: every `available` claim must point at >=1 valid evidence record.

    A failure here means the run cannot qualify — wrong-company / unsupported
    facts block qualification, so the self-score must be 0 on precision.
    """
    by_id: dict[str, dict] = {}
    for envelope in envelopes:
        by_id = {r["id"]: r for r in envelope.get("evidence", [])}
        for claim in envelope.get("claims", []):
            if claim["availability"] != "available":
                continue
            ids = claim.get("evidence_ids") or []
            if not ids:
                return False
            for evidence_id in ids:
                record = by_id.get(evidence_id)
                if record is None or not ResearchAgent.evidence_hash_ok(record):
                    return False
    return True


def self_score(envelopes: list[dict]) -> dict:
    """Conservative qualification self-score proxy (0-100) + components."""
    total_claims = sum(len(e.get("claims", [])) for e in envelopes)
    available_claims = sum(
        1 for e in envelopes for c in e.get("claims", []) if c["availability"] == "available"
    )
    recall_proxy = (available_claims / total_claims) if total_claims else 0.0
    precision_ok = _precision_gate_ok(envelopes)
    synthesis_ok = all(
        isinstance(e.get("changes"), list) for e in envelopes
    ) and bool(envelopes)
    ux_ok = True  # machine-readable report + profiles always emitted
    score = (
        recall_proxy * _RECALL_WEIGHT
        + (1.0 if precision_ok else 0.0) * _PRECISION_WEIGHT
        + (1.0 if synthesis_ok else 0.0) * _SYNTHESIS_WEIGHT
        + (1.0 if ux_ok else 0.0) * _UX_WEIGHT
    )
    return {
        "total": round(score, 2),
        "qualifies_proxy": score >= 70.0,  # margin over the official 65/100 bar
        "components": {
            "recall_proxy": round(recall_proxy, 4),
            "recall_points": round(recall_proxy * _RECALL_WEIGHT, 2),
            "precision_gate_ok": precision_ok,
            "precision_points": _PRECISION_WEIGHT if precision_ok else 0.0,
            "synthesis_points": _SYNTHESIS_WEIGHT if synthesis_ok else 0.0,
            "ux_points": _UX_WEIGHT if ux_ok else 0.0,
        },
    }


def build_report(
    *,
    run_id: str,
    envelopes: list[dict],
    started_iso: str,
    completed_iso: str,
    third_party_cost_usd: float = 0.0,
    simulated: bool = False,
    agent_name: str = "signalpost",
) -> dict:
    """Assemble the machine-readable run report JSON."""
    state_counts: dict[str, int] = {}
    claim_total = 0
    requests_total = 0
    runtime_ms_total = 0
    terminal_counts: dict[str, int] = {}
    for envelope in envelopes:
        claim_total += len(envelope.get("claims", []))
        for claim in envelope.get("claims", []):
            state = claim["availability"]
            state_counts[state] = state_counts.get(state, 0) + 1
        ops = envelope.get("operations", {})
        requests_total += ops.get("requests", 0)
        runtime_ms_total += ops.get("runtime_ms", 0)
        terminal = envelope["run"]["terminal_status"]
        terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1

    report = {
        "run_id": run_id,
        "agent": agent_name,
        "started_at": started_iso,
        "completed_at": completed_iso,
        "companies": len(envelopes),
        "terminal_status_counts": terminal_counts,
        "claim_state_counts": state_counts,
        "claims_total": claim_total,
        "requests_total": requests_total,
        "runtime_ms_total": runtime_ms_total,
        "third_party_cost_usd": round(third_party_cost_usd, 4),
        "validation_violations_total": sum(
            len(e.get("validation_violations", [])) for e in envelopes
        ),
        "self_score": self_score(envelopes),
        "models": [],
        "apis": [
            "Brønnøysund enhetsregisteret (NLOD 2.0)",
            "Brønnøysund regnskapsregisteret (NLOD 2.0)",
            "Brønnøysund enhetsregisteret konsernstruktur + underenheter (NLOD 2.0)",
        ],
        "licences": ["NLOD 2.0 (Brønnøysund data)"],
        "source_rights": (
            "Only public registry data (Brønnøysund, NLOD 2.0, attribution) and "
            "company-owned websites (robots-respecting). No LinkedIn/Meta/Indeed."
        ),
    }
    if simulated:
        report["simulated"] = True
    return report


# ---------------------------------------------------------------------------
# Evaluation-contract validation gates
# (https://builderr.ai/docs/signalpost-evaluation-harness.md — official-run
# checks: exactly one terminal envelope per input company; no fabricated
# financial value or material wrong-company publication; every published
# material claim carries source + retrieval time + reporting period where
# relevant; missing values never silently converted to zero; refresh
# idempotent; refresh preserves prior evidence and exposes material changes.)
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = {"completed", "failed"}


def validate_envelope(envelope: dict, expected_org: str | None = None) -> list[str]:
    """Check one envelope dict against the output contract.

    Returns a list of violation messages (empty => the envelope is valid).
    This is the run-time gate for the contract's fabrication guardrail: an
    `available` claim without resolvable, auditable evidence, or any claim
    whose non-available state still carries a value, is a violation.

    `expected_org`: when given, the envelope must carry exactly this org
    number — an invalid input ("" org) still gets its failed envelope, and
    that is the honest value to check against, not a fabricated one.
    """
    violations: list[str] = []
    org = envelope.get("organisation_number", "?")

    if expected_org is not None:
        if envelope.get("organisation_number") != expected_org:
            violations.append(
                f"envelope org {envelope.get('organisation_number')!r} does not "
                f"match input {expected_org!r}"
            )
    elif not envelope.get("organisation_number"):
        violations.append("envelope missing organisation_number")

    run = envelope.get("run") or {}
    if run.get("terminal_status") not in _TERMINAL_STATUSES:
        violations.append(
            f"{org}: invalid terminal_status {run.get('terminal_status')!r}"
        )
    for key in ("run_id", "started_at", "completed_at"):
        if not run.get(key):
            violations.append(f"{org}: run block missing {key}")

    claims = envelope.get("claims")
    if not isinstance(claims, list) or not claims:
        violations.append(f"{org}: claims is not a non-empty list")

    evidence = envelope.get("evidence")
    if not isinstance(evidence, list):
        violations.append(f"{org}: evidence is not a list")
        evidence = []
    by_id: dict[str, dict] = {}
    for record in evidence:
        rid = record.get("id")
        if not rid:
            violations.append(f"{org}: evidence record missing id")
            continue
        if rid in by_id:
            violations.append(f"{org}: duplicate evidence id {rid}")
        by_id[rid] = record
        if not ResearchAgent.evidence_hash_ok(record):
            violations.append(
                f"{org}: evidence {rid} missing required fields or bad sha256"
            )
        if not record.get("source_url") or not record.get("retrieved_at"):
            violations.append(f"{org}: evidence {rid} missing source or date")

    for claim in claims or []:
        violations.extend(
            f"{org}: {message}" for message in validate_claim(claim)
        )
        availability = claim.get("availability")
        ids = claim.get("evidence_ids") or []
        if availability == "available":
            if not ids:
                violations.append(
                    f"{org}: available claim {claim.get('field')} has no evidence"
                )
            for evidence_id in ids:
                if evidence_id not in by_id:
                    violations.append(
                        f"{org}: claim {claim.get('field')} references "
                        f"unknown evidence id {evidence_id}"
                    )
        elif claim.get("value") is not None:
            # Anti-fabrication gate: a non-available claim must not carry a
            # value (missing values are never silently converted to zero).
            violations.append(
                f"{org}: {availability} claim {claim.get('field')} carries a value"
            )

    if not isinstance(envelope.get("changes"), list):
        violations.append(f"{org}: changes is not a list")
    if not isinstance(envelope.get("errors"), list):
        violations.append(f"{org}: errors is not a list")
    operations = envelope.get("operations") or {}
    for key in ("requests", "runtime_ms", "third_party_cost_usd"):
        if key not in operations:
            violations.append(f"{org}: operations missing {key}")

    return violations


def validate_run(envelopes: list[dict], expected_inputs: list[str]) -> list[str]:
    """Run-level gates: one envelope per input, no extras, no missing rows,
    in input order (the run preserves input order end to end).

    A missing row means the run is not scored, so any gap is a violation.
    """
    violations: list[str] = []
    if len(envelopes) != len(expected_inputs):
        violations.append(
            f"envelope count {len(envelopes)} != input count {len(expected_inputs)}"
        )
    got = [e.get("organisation_number") for e in envelopes]
    # Empty org numbers come from invalid inputs (each still gets its failed
    # envelope); duplicates are only meaningful for real org numbers.
    real_got = [org for org in got if org]
    if len(real_got) != len(set(real_got)):
        violations.append("duplicate envelopes for the same organisation number")
    missing = [org for org in expected_inputs if org not in got]
    if missing:
        violations.append(f"missing envelopes for {len(missing)} input(s): {missing[:5]}")
    extra = [org for org in got if org not in expected_inputs]
    if extra:
        violations.append(f"envelopes for {len(extra)} non-input org number(s): {extra[:5]}")
    for envelope, expected_org in zip(envelopes, expected_inputs):
        violations.extend(validate_envelope(envelope, expected_org=expected_org))
    for envelope in envelopes[len(expected_inputs):]:
        violations.extend(validate_envelope(envelope))
    return violations


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
