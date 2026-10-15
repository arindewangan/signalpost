# Day-3/4 work log (Oct 15, 2026)

Slot: Oct 15–16 build advancement + Oct 17 prep. All work in
`~/workspace/hackathon-pipeline/builds/builderr-signalpost/`.
Commit: none (no git repo in project dir; files edited in place).

## Offline work

### 1. Pytest suite — GREEN, 87/87 (was 55/55)
- Baseline before changes: 55/55 passed with the project venv
  (system python3 has no pytest; `.venv/bin/python` used throughout).
- Added `tests/test_contract.py` (18 tests) + `tests/test_footprint.py`
  (13 tests) + 1 refresh regression test → final: **87/87 passed**.
- No fake passes: all tests assert real behavior against canned fixtures /
  real agent runs; the one initial failure (spaced-org regex vs a longer
  digit run) was a REAL gate-tightening fix, not a test relaxation
  (see §3).

### 2. Refresh-replay idempotence (live, §7) — verdict: CONDITIONAL PASS
- Ran the same 100-company batch twice with a snapshot between
  (`--snapshot out/day3/envelopes-a.jsonl` on the second run, run day3-100b).
- Second run: 98/100 envelopes had empty `changes`; **zero duplicate
  evidence records**; `validation_violations_total` 0; self-score 78.5.
- The 2 non-empty `changes` are live-source non-determinism, NOT replay bugs:
  1. `934930150` sub_units available→available: the Brønnøysund API returned
     the SAME 2 sub-units in a DIFFERENT ORDER between runs. Root-caused and
     FIXED: `_values_equal` in `report.py` is now order-insensitive for lists
     (canonical sort by JSON) — the contract explicitly measures false-change
     rate. Regression test added
     (`test_list_reordering_is_not_a_material_change`); suite 87/87.
  2. `975877620` (atvogfritid.no): sandbox transport flakiness — reachable in
     run A (identity available), ProxyError in run B (identity failed). The
     replay honestly recorded what each run observed; not a logic bug.
- Offline unit tests prove the replay logic itself idempotent
  (`test_same_snapshot_zero_changes`, `test_evidence_merge_no_duplicates`).

### 7b. Precision audit on run A — PASS
- Fresh audit (`/tmp/precision_audit_day3.py`, mirrors Day-2): 100 envelopes,
  970 available claims — **0 failures, 0 warnings**. Every available claim
  resolves to evidence with valid 64-hex sha256 + source URL + retrieval
  date; every record entity-anchored (org number in URL/claim span); zero
  simulated evidence in live mode; non-available claims carry no value.

### 3. Clean-machine install check — PASS (real commands below)
Fresh venv in /tmp, install from requirements.txt only, documented
`--dry-run` command:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m signalpost.main --organisations <batch.jsonl> \
  --output out/envelopes.jsonl --profiles-output out/profiles.jsonl \
  --report out/run-report.json --run-id <id> [--expected-count N] \
  [--snapshot prev.jsonl] [--dry-run]
```
Result: INSTALL OK; 3/3 envelopes (completed/completed/failed), exit 0,
contract violations 0, full suite 87/87 in the clean venv (re-verified on
the final Day-3 code). These commands match README.md exactly (README
updated: test count 87/87, dry-run proxy ~58/100). NOTE: pip install needed
proxy retries in this sandbox (transient ProxyErrors, retried to success) —
not a code issue.

### 4. Cost metering — PASS (deterministic $0 path)
- No LLM code is wired anywhere in `signalpost/` (only the honest
  `"llm": "disabled"` label in `build_profile`); no LLM env vars set in the
  environment (`SIGNALPOST_LLM_MODEL`/`SIGNALPOST_LLM_API_KEY` absent).
- Verified on the dry run: `report.third_party_cost_usd == 0.00`;
  every profile carries `llm: "disabled"`.
- Live runs likewise report `third_party_cost_usd: 0.0` (see §7); only
  Brønnøysund (NLOD 2.0, free) + company-owned websites are fetched.

### 5. Evaluation-contract encoding — DONE (was partially missing)
- Fetched the current contract text (https://builderr.ai/docs/signalpost-evaluation-harness.md,
  scoring version 2, effective 26 Aug 2026 — same version SPEC_NOTES
  captured Oct 11). Verified the rules it states vs the code.
- Gaps found and fixed (BUILD_PLAN said "encode every contract rule into
  states.py/report.py validation gates" — this had not been done):
  - `states.validate_claim()`: every claim must carry a valid availability
    state, confidence in [0,1], and an evidence_ids list.
  - `report.validate_envelope()`: envelope-level gate — run block terminal,
    claims non-empty, evidence records complete (source_url, source_class,
    retrieved_at, valid 64-hex sha256, claim_span), evidence ids unique,
    every `available` claim resolves ≥1 evidence id (fabrication guardrail),
    non-available claims carry NO value (never silently zero-filled),
    `changes`/`errors` lists, `operations` complete.
  - `report.validate_run()`: one envelope per input, input order preserved,
    no missing rows, no duplicate orgs (empty orgs from invalid inputs
    exempt — each still gets its failed envelope, honestly labeled).
  - `main.py`: runs validation on every envelope, stores
    `envelope["validation_violations"]`, prints run-level violations to
    stderr, adds `report["validation_violations_total"]` (0 on all runs
    below). Exit-code contract unchanged (0 = one terminal envelope each).
- The clean-machine check itself PROVED the gate's value: it caught a real
  edge case (validator initially flagged the failed envelope for the
  invalid `"bad"` input; fixed to compare against the input org instead of
  demanding a non-empty org number).
- 18 new tests in `tests/test_contract.py` cover all gates.

### 6. Footprint-recall hardening (LAWFUL only, exact-entity gate kept)
Day-2 baselines (same batch100): website_identity 4/100 available,
open_jobs 1/100 available. Changes in `sources.py` + `classifier.py`:
- **Spaced org-number matching** (`classifier.evidence_refs_entity`):
  Norwegian sites print "Org.nr 923 609 016" — now matches as the same
  exact entity; digit-boundaried so a longer digit run containing the org's
  digits does NOT match (tightened vs Day-2's plain substring check).
- **Identity-page discovery** (`WebsiteSource.fetch`): when the homepage
  carries no entity reference, up to 2 same-host kontakt/om-oss/about links
  discovered FROM THE HOMEPAGE's own anchors are checked — still
  company-owned, robots-respecting, gated on the exact entity. No guessed
  paths are ever fetched.
- **Careers-link discovery from link TEXT** (`find_careers_links`): matches
  href OR visible anchor text (e.g. `<a href="/om-oss">Karriere</a>`);
  same-host only.
- **Job-title extraction** (`extract_job_titles`): also harvests visible
  anchor texts (postings are usually links), filtering generic nav words
  (les mer, kontakt, personvern, …); cap 25.
- **robots.txt cached per host** (was re-fetched per page).
- The entity gate is UNCHANGED in strictness: uncertain match → ambiguous,
  never available. 13 new tests in `tests/test_footprint.py`.

## Live runs (SIGNALPOST_LIVE=1, --workers 2)

### 7. 100-company re-run (batch100.jsonl, hardened code) — run day3-100a
- Command: `SIGNALPOST_LIVE=1 .venv/bin/python -m signalpost.main
  --organisations batch100.jsonl --output out/day3/envelopes-a.jsonl
  --profiles-output out/day3/profiles-a.jsonl --report out/day3/report-a.json
  --run-id day3-100a --expected-count 100 --workers 2`
- Result: exit 0, 100/100 terminal `completed`, **515 honest requests**,
  **self-score 78.53/100** (qualifies_proxy: True; Day-2 was 78.47 — no
  regression), `validation_violations_total` **0**.
- Claim mix: **970 available / 725 not_available / 2 blocked / 3 failed**
  (Day-2: 968/727/2/3). Per-field: legal_name/form/status/industry 100/100;
  key_people 100/100; accounts 98/100 (2 blocked on HTTP 500);
  sub-units 73/100; konsern 9/100; website_identity **5/100** (Day-2: 4/100);
  open_jobs 1/100.
- Footprint-recall verdict: the hardened crawler is implemented + unit-tested
  (13 tests), but this batch is STRUCTURALLY capped — only 6/100 companies
  have a registry-listed homepage, and 1 of the 6 is unreachable from this
  sandbox (transport → failed, same as Day-2). The 5th available
  (975877620/atvogfritid.no) is transport luck vs the pilot, not discovery;
  no live company needed the kontakt-page fallback (homepages matched
  directly). Lawful expansion beyond registry-listed homepages (web search
  for company sites) was deliberately NOT done — misattribution risk
  violates the exact-entity doctrine.
- Known imperfection (documented, not a precision regression): the one
  open_jobs value (sletteboe.no/ledige-stillinger, an e-commerce site)
  lists 25 titles dominated by product-category nav anchors. The page IS
  entity-anchored (org number present), so no wrong-company fact — but the
  count is noisy. Mitigations shipped: HTML-entity decoding, posting-like
  href preference, generic-nav-word filtering. Structural fix (posting vs
  nav classification) deferred — out of scope for the freeze.
- A pilot run before the final code (same batch) exposed the nav-noise
  issue; it was fixed and the batch re-run, so the numbers above are on
  the final Day-3 code.

### 8. Idempotence live replay — run day3-100b (snapshot of run A) — DONE
- Command: same as run A plus `--snapshot out/day3/envelopes-a.jsonl`,
  outputs `out/day3/envelopes-b.jsonl` / `report-b.json`, run-id day3-100b.
- Result: exit 0, 100/100 completed, self-score 78.5, 0 contract violations.
- Verdict details in §2 (CONDITIONAL PASS): 98/100 empty changes, 0
  duplicate evidence records; the 2 non-empty changes are live-source
  non-determinism (1 order-artifact → fixed in code, 1 transport flake).

### 9. 1,000-company live run (batch1000.jsonl, systematic every-411th of the
411,160-company universe; universe sha256-verified against the contract's
published hash `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`)
- First launch (~16:01) was lost when the background exec session was reaped
  (~5 min in, no output written); re-launched detached via nohup (PID 1799,
  16:09 IST) writing to out/day3/envelopes-1000.jsonl + report-1000.json.
  Expected ~85–90 min (Day-2 rate: ~8.5 min/100 companies at --workers 2).
- IN FLIGHT at time of writing — numbers below to be filled on completion.

## Freeze readiness (Oct 18)
- Code: 87/87 tests green; contract validation gates encoded + live-verified
  (0 violations on 200 live envelopes); order-insensitive refresh comparison;
  clean-machine install check passed on final code.
- Runs: Day-3 100-company live re-run self-score 78.53/100 (≥70 proxy, no
  regression vs Day-2 78.47); precision audit PASS (970 claims, 0 failures);
  idempotence replay logic proven (2/100 live changes both explained:
  1 order-artifact → fixed, 1 transport flakiness).
- 1,000-company run: IN FLIGHT (results pending — runtime, request count,
  claim mix, self-score to be recorded in §9 on completion).
- Remaining before freeze:
  - Record the 1,000-company numbers + self-score in §9.
  - Refresh `SUBMISSION_EMAIL_DRAFT.md`: new self-score (78.53), contract
    validation statement (0 violations), final commit hash (parent agent
    owns the repo/commit — DO NOT commit from here).
  - `out/day3/report.html` regenerated from run A (done, 100 companies).
  - Open (human-side, not build blockers): Builderr payout rails still
    unstated publicly; official time/resource budget still unpublished
    (documented in SPEC_NOTES; flagged for Discord / inquiries@builderr.ai).
