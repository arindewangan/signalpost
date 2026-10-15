"""Signalpost run command.

THE documented run command (paste-able, single step):

    python -m signalpost.main \\
        --organisations <batch.jsonl> \\
        --output out/envelopes.jsonl \\
        --profiles-output out/profiles.jsonl \\
        --report out/run-report.json \\
        --run-id <id> [--expected-count N] [--snapshot prev.jsonl] [--dry-run]

Reads a JSONL batch of Norwegian organisation numbers (9 digits per line,
{"organisation_number": "..."} or a bare string), researches every company,
and emits:
  - envelopes JSONL: one terminal envelope per input company (the contract).
  - profiles JSONL:  human-readable synthesis per company.
  - report JSON:     run id, runtime, state counts, request counts,
                     third_party_cost_usd, self-score estimate.

--dry-run runs the full pipeline against the bundled simulated fixture
(envelopes carry "simulated": true and "source_class": "simulated") — used for
CI and the clean-machine install check. Without --dry-run, SIGNALPOST_LIVE=1
enables live Brønnøysund requests (rate-limited to ~2 req/s).

Exit code 0 only when every input produced exactly one terminal envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

from .agent import ResearchAgent
from .batch import read_batch
from .evidence import utc_now_iso
from .report import build_report, load_snapshot, now_iso, replay, validate_run
from .sources import BrregSource, WebsiteSource

DEFAULT_WORKERS = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Signalpost company research agent — one envelope per input company."
    )
    parser.add_argument("--organisations", required=True,
                        help="JSONL batch file of organisation numbers (9 digits each)")
    parser.add_argument("--output", required=True,
                        help="where to write envelopes JSONL")
    parser.add_argument("--profiles-output", default=None,
                        help="where to write profiles JSONL")
    parser.add_argument("--report", required=True,
                        help="where to write the run-report JSON")
    parser.add_argument("--run-id", default=None,
                        help="run id (defaults to a generated UUID)")
    parser.add_argument("--expected-count", type=int, default=None,
                        help="fail if the batch does not contain exactly this many inputs")
    parser.add_argument("--snapshot", default=None,
                        help="previous run's envelopes JSONL for refresh replay")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the full pipeline against the simulated fixture")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="concurrent companies in flight (default 2; rate limiter shared)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started_iso = now_iso()
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:12]}"

    dry_run = args.dry_run
    live = not dry_run and bool(int(os.environ.get("SIGNALPOST_LIVE", "0")))

    try:
        inputs = read_batch(args.organisations)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.expected_count is not None and len(inputs) != args.expected_count:
        print(
            f"error: expected {args.expected_count} inputs, got {len(inputs)}",
            file=sys.stderr,
        )
        return 2

    simulated = dry_run or not live
    source = BrregSource(live=live)
    agent = ResearchAgent(source=source, website_source=WebsiteSource(),
                          run_id=run_id, simulated=simulated)

    envelopes: list[dict] = [None] * len(inputs)  # type: ignore[list-item]
    if args.workers and args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, envelope in enumerate(pool.map(agent.research, inputs)):
                envelopes[i] = envelope
    else:
        for i, batch_input in enumerate(inputs):
            envelopes[i] = agent.research(batch_input)

    # Refresh replay: material changes vs the previous snapshot (idempotent).
    previous = load_snapshot(args.snapshot) if args.snapshot else {}
    replay(previous, envelopes)

    # Evaluation-contract gate: one terminal envelope per input; every
    # available claim backed by auditable evidence; non-available claims carry
    # no value. Violations are recorded loudly on the envelope and the report.
    expected_orgs = [b.organisation_number for b in inputs]
    violations = validate_run(envelopes, expected_orgs)
    per_org: dict[str, list[str]] = {}
    for envelope in envelopes:
        org_violations = [
            v for v in violations if v.startswith(f"{envelope.get('organisation_number')}: ")
        ]
        envelope["validation_violations"] = org_violations
        per_org[envelope.get("organisation_number", "?")] = org_violations
    run_level_violations = [
        v for v in violations
        if not any(v.startswith(f"{org}: ") for org in per_org)
    ]
    for message in run_level_violations:
        print(f"CONTRACT VIOLATION: {message}", file=sys.stderr)

    profiles = [agent.build_profile(e) for e in envelopes]

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        for envelope in envelopes:
            fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")

    profiles_path = args.profiles_output or args.output.replace(".jsonl", "-profiles.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(profiles_path)), exist_ok=True)
    with open(profiles_path, "w", encoding="utf-8") as fh:
        for profile in profiles:
            fh.write(json.dumps(profile, ensure_ascii=False) + "\n")

    completed_iso = now_iso()
    report = build_report(
        run_id=run_id,
        envelopes=envelopes,
        started_iso=started_iso,
        completed_iso=completed_iso,
        third_party_cost_usd=0.0,  # deterministic path: no paid APIs called
        simulated=simulated,
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    complete = all(
        isinstance(e, dict) and e.get("run", {}).get("terminal_status") in ("completed", "failed")
        and isinstance(e.get("claims"), list)
        for e in envelopes
    ) and len(envelopes) == len(inputs)
    print(
        f"run {run_id}: {len(envelopes)} envelopes -> {args.output}; "
        f"report -> {args.report}; self-score {report['self_score']['total']}/100; "
        f"contract violations {report['validation_violations_total']}"
    )
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
