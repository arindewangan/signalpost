# Signalpost — Build Plan

Timeline target: **Oct 18, 2026** (code freeze / last revision commit). Submit v1 as
soon as the agent passes a local 100-company dry run + real 100-company smoke test.

## Architecture (Python, stdlib-first + `requests`)

```
signalpost/
├── __init__.py        # version, public API
├── states.py          # CompanyState enum (6 states) + claim envelope helpers
├── batch.py           # batch I/O: read/validate Norwegian org numbers (9 digits)
├── sources.py         # Brønnøysund registry client (bulk CSV index + live REST)
├── agent.py           # ResearchAgent: per-company pipeline orchestration
├── classifier.py      # availability-state classifier per claim + entity-match gate
├── evidence.py        # evidence capture: sha256, retrieved_at, claim spans
├── report.py          # run-report + refresh-replay (idempotent change detection)
└── main.py            # CLI: one documented run command
```

### Pipeline per company (org number in → envelope out)
1. **Identity anchor** (`batch.py` + `sources.py`): normalize the 9-digit org number;
   look it up in the Brønnøysund bulk CSV index (downloaded once per run; NLOD 2.0
   licence, attribution). If absent ⇒ every claim `not_available`, envelope still
   emitted with `terminal_status: completed`.
2. **Official layer** (`sources.py`): live entity endpoint → name, form, status,
   industry, address, website, employees; roles endpoint → company-centric role
   holders; subunits; konsernstruktur; regnskapsregisteret → latest normalized
   accounts + filing years. HTTP errors mapped: 404 ⇒ `not_available` (with the
   not-zero distinction); 410 ⇒ drop cached copy; 429/timeout ⇒ `blocked`;
   exceptions ⇒ `failed` (still one envelope).
3. **External footprint** (Oct 18 slot, external-first differentiator): only sources
   with lawful, declared connectors — company-owned website (static HTML first,
   entity-match check on org number/name in page), Google Places (company-linked
   page only if the place page references the org number or exact legal name),
   jobs via company-owned careers pages / permitted APIs. Any uncertain match ⇒
   `ambiguous`, never `available`.
4. **Entity-match gate** (`classifier.py`): publish a claim as `available` only when
   the evidence names the exact legal entity (org number or exact legal name match).
   Parent/brand/franchise/similar names are NOT exact ⇒ `ambiguous`.
5. **Evidence capture** (`evidence.py`): every claim gets ≥1 evidence record —
   `source_url`, `source_class` (official / company_owned / licensed_api /
   public_web), `retrieved_at`, `content_sha256`, `claim_span`. No evidence ⇒
   claim cannot be `available`.
6. **Refresh replay** (`report.py`): compare against a previous-snapshot input;
   emit `changes` (field, old, new, evidence) only on material changes; same
   snapshot ⇒ zero changes, zero duplicates (idempotence gate, tested).
7. **Report** (`report.py`): machine-readable JSON — run_id, runtime, per-company
   state counts, request counts, latency, `third_party_cost_usd`, qualification
   self-score.

### Data ingestion
- Bulk: `https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv` downloaded
  at run start into a local SQLite/CSV index (org number → row). Streaming parse;
  memory-bounded.
- Universe file for local testing: `https://builderr.ai/signalpost-company-universe-2025.jsonl.gz`
  (optional — Builderr supplies the official batch; only needed for local batches).
- Checkpoint/resume: processed org numbers recorded per run-id; resume skips
  completed envelopes. Concurrency via `concurrent.futures` with a per-host rate
  limiter (~≤ 2 req/s to data.brreg.no to respect ~30 req/min filing-year limit).

## Model / API choices (free-tier-first, env-gated, no invented keys)
| Component | Choice | Cost lever |
|---|---|---|
| Entity matching, synthesis text | **No LLM by default** — deterministic templates + exact-match rules | $0 |
| Optional LLM enhancement | `SIGNALPOST_LLM_MODEL` + `SIGNALPOST_LLM_API_KEY` env vars (OpenAI-compatible). Absent ⇒ deterministic fallback path, clearly labeled `llm: disabled` in report | only if key supplied |
| Free-tier LLM candidate | Google AI Studio free tier (Gemini) — user-provided key at runtime; small budget (≤ 5 short calls/company worst case) | $0 at free tier |
| Search | No unlicensed scraping. Free: Brønnøysund (NLOD 2.0), company-owned sites, Google Places free quota | $0 |
| Ask Builderr | Page explicitly allows requesting a model key for scoring — request one if the free tier proves insufficient |

Rules: never hardcode keys; `report.third_party_cost_usd` computed from actual
metered usage (requests × declared unit price; 0 when no paid calls made).

## Evaluation strategy (self-score vs the 65/100 bar)
We cannot see the hidden collection, so build a local proxy:
1. **Coverage probe**: run on a 100-company local batch; count claims with
   `available` per company vs the union of all official-source fields that exist
   for those companies in Brønnøysund (measurable locally) — this proxies the
   50-pt recall component.
2. **Precision audit**: sample 20 companies manually; every `available` claim must
   have a valid evidence record with matching sha256; any wrong-company claim ⇒
   gate failure (score proxy = 0 on precision until fixed).
3. **Refresh idempotence**: run the same batch twice with a snapshot between ⇒
   second `changes` list must be empty and no duplicate evidence records.
4. **Failure envelope completeness**: assert one terminal envelope per input on a
   100-batch including invalid org numbers and simulated 404s/timeouts.
5. **Self-score formula** (conservative): recall_proxy × 50 + precision_gate × 30 +
   synthesis presence × 12 + UX (readable HTML report) × 8. Ship v1 only if the
   proxy ≥ 70 (margin over 65).

## Run-command design (single paste-able command)
```
python -m signalpost.main \
  --organisations <batch.jsonl> \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id <id> [--expected-count N] [--snapshot prev.jsonl] [--dry-run]
```
- Install step (documented): `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
- `--dry-run` runs the full pipeline against the bundled fixture (simulated sources,
  honestly labeled `simulated: true` in every envelope) — used for CI and the
  clean-machine check.
- Exit code 0 only when every input produced exactly one terminal envelope.

## Cost-per-run estimate (100 companies, worst case)
- Brønnøysund: $0 (public, NLOD 2.0).
- Company websites: $0 (direct fetch, robots-respecting).
- LLM (optional path): ≤ 5 short calls/company × 100 = 500 calls at free tier = **$0**;
  if a paid model key is supplied by Builderr, declared cost = actual metered usage.
- **Declared expected cost per official 100-company run: $0.00** (deterministic path;
  LLM path cost reported honestly when enabled).

## Timeline to Oct 18
- **By Oct 12 (this scaffold, done):** package skeleton, states, batch I/O, CLI,
  report formatter, tests, dry-run path.
- **Oct 13–14:** Brønnøysund client (bulk index + live REST + roles/accounts),
  classifier gate, evidence capture; real 100-company smoke test.
- **Oct 15–16:** external-footprint connectors (company-site crawler, entity-match
  hardening), refresh replay, HTML/UX report rendering.
- **Oct 17:** precision audit on 20 companies, idempotence tests, clean-machine
  install check, 1,000-company local run, cost metering verification.
- **Oct 18:** freeze, smoke-test report generation, email to submit@builderr.ai
  (repo URL + commit hash + run command + smoke report + models/APIs/licences +
  cost + agent name + contact). Revisions after Oct 18 not allowed; entries close
  Oct 21.

## Verified spec addendum (2026-10-10, official challenge page https://builderr.ai/challenges/signalpost)
- **Ranking:** qualified entries are averaged across scheduled daily batches while submitted code is active. Tiebreak order: (1) fewer wrong-company facts, (2) better weighted company recall, (3) lower declared third-party API cost. Implication: minimize cost-per-run (our $0 deterministic path is optimal) and never publish a fact with uncertain entity match (better `ambiguous`/`not_available` than a wrong-company match — wrong-company also blocks qualification until corrected).
- **Qualification:** ≥65/100 on an official run; component scores are not separate thresholds (ship if proxy ≥70 for margin).
- **Submission = email** to submit@builderr.ai with: repo URL, exact commit hash, one run command, 100-company smoke-test result/run report, models/APIs/licences, expected cost per official run, agent name, contact. No account needed to enter; payout rails (bank/PayPal) NOT stated on any public page — verify before celebration, not before building.
- **Prizes:** $2,500 total — main $2,000 ($1,200/$500/$300) + separate JBOX bonus $500 ($250/$150/$100, for agents built with JBOX — out of scope) + 4×$100 community awards (public vote; "never changes the technical ranking").
- **Scoring formula (recall/coverage):** for each information type, 70% = share of companies covered, 30% = share of individual facts found (weighted sum → 50-pt recall component).
- **Batch growth:** official batch may grow (e.g. 1,000 → 1,100); agent MUST read the batch file at runtime (never its own list) and emit one envelope per company, even past 100.
- **Evaluation contract:** linked on the page (【2†evaluation contract】) — fetch the full contract at build start (Oct 13) and encode every contract rule into `states.py`/`report.py` validation gates.
