# Signalpost — company research agent

Researches Norwegian companies from their 9-digit organisation number and
emits **one terminal envelope per input company**, with every claim carrying
an availability state (`available` / `not_available` / `blocked` /
`not_applicable` / `ambiguous` / `failed`) and source evidence. Built for the
[Builderr Signalpost challenge](https://builderr.ai/challenges/signalpost).

## Install (clean venv)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Pinned deps (stdlib-first): `requests` (registry + website fetches), `pytest`
(test suite only). No LLM, no paid APIs — expected third-party cost per run:
**$0.00**.

## The one run command

```bash
.venv/bin/python -m signalpost.main \
  --organisations <batch.jsonl> \
  --output out/envelopes.jsonl \
  --profiles-output out/profiles.jsonl \
  --report out/run-report.json \
  --run-id <id> [--expected-count N] [--snapshot prev.jsonl] [--dry-run]
```

- `<batch.jsonl>`: JSONL of organisation numbers — `{"organisation_number":
  "917755736"}`, `{"org": ...}`, or a bare `"917755736"` per line. This is the
  batch the evaluator hands over; the agent never invents its own company list.
- `--snapshot prev.jsonl`: previous run's envelopes → refresh replay fills
  `changes` with material changes only (same snapshot ⇒ zero changes,
  idempotent, no duplicate evidence).
- `--dry-run`: full pipeline against the bundled simulated fixture (envelopes
  labeled `"simulated": true`, `"source_class": "simulated"`) — for CI and the
  clean-machine check. Without it, live Brønnøysund requests need
  `SIGNALPOST_LIVE=1` and are rate-limited to ~2 req/s.

Exit code 0 only when every input produced exactly one terminal envelope.

## What the official run contract expects

Per the challenge: the evaluator supplies a batch of organisation numbers at
run time; the agent reads that file, emits one envelope per company (a missing
row = run not scored), and reports runtime, request counts and
`third_party_cost_usd`. This agent:

- emits envelopes JSONL (`organisation_number`, `run` block, `claims` with
  evidence ids, `evidence` with `source_url` / `source_class` /
  `retrieved_at` / `content_sha256` / `claim_span`, `changes`, `errors`,
  `operations`) plus profiles JSONL plus a run-report JSON;
- never fabricates: no evidence ⇒ the claim cannot be `available`;
- entity-match gate: `available` only when the evidence names the exact legal
  entity (org number or exact legal name). Uncertain matches ⇒ `ambiguous`,
  never `available` — a wrong-company match blocks qualification, so missing
  information always beats a guessed match;
- HTTP mapping: 404 ⇒ `not_available` (never zero-filled); 410 ⇒ cached copy
  dropped; 429/timeout ⇒ `blocked`; unexpected errors ⇒ `failed` envelope
  (still one row per input);
- discards person birth dates at the source layer; never scrapes LinkedIn,
  Meta or Indeed;
- Brønnøysund data used under NLOD 2.0 (attribution in report).

## Smoke test

```bash
printf '%s\n' '{"organisation_number": "917755736"}' '"999999999"' '"bad"' > /tmp/batch.jsonl
.venv/bin/python -m signalpost.main --organisations /tmp/batch.jsonl \
  --output /tmp/out/envelopes.jsonl --profiles-output /tmp/out/profiles.jsonl \
  --report /tmp/out/report.json --run-id smoke-1 --dry-run --expected-count 3
.venv/bin/python -m pytest tests/ -q
```

Expect exit 0, 3 envelopes (completed/completed/failed), 0 contract violations,
and 87/87 tests passing. The dry-run self-score on fixture data (~58/100) is
a plumbing check only — it is NOT the qualification estimate; that needs the
real 100-company run (Day-2 work).

## HTML profile report (UX)

```bash
.venv/bin/python -m signalpost.html_report \
  --envelopes out/envelopes.jsonl --profiles out/profiles.jsonl \
  --report out/run-report.json --output out/report.html
```

Self-contained page: per-company profile with claims, state badges, evidence
links, source dates (retrieved_at) and sha256 fragments, material changes,
and a filter box. Mobile-responsive.

## Honest limitations (Day-2)

- Live Brønnøysund client exercised against production (Oct 12): entity,
  roles, regnskapsregisteret accounts, sub-units
  (`/underenheter?overordnetEnhet=`), konsernstruktur — entity-match gate
  passes on real registry JSON (5/5 probed). The accounts endpoint returns a
  LIST of year-records; the agent summarizes the latest filing.
- Footprint levers: sub-units + konsernstruktur (official layer) and open
  jobs from company-owned careers pages (entity-anchored, otherwise
  `ambiguous`). No jobs, no locations beyond registry addresses.
- Transport flakiness (proxy resets) is retried with backoff; HTTP 429/5xx
  ⇒ `blocked`, 404 ⇒ `not_available`, 410 ⇒ cached copy dropped.
- Per-envelope request accounting uses thread-local deltas on the shared
  sources (the run report total is the honest sum).
- Synthesis is deterministic templates (`llm: disabled`); no LLM is called.
- Rate limiter defaults to ~2 req/s to data.brreg.no.
