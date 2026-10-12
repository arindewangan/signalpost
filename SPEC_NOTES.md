# Signalpost Challenge — Spec Notes

Sources read (Oct 10, 2026):
- `https://builderr.ai/challenges/signalpost` — official challenge page (authoritative).
- `https://github.com/mmansii/builderr-signalpost` — community reference agent repo
  (README + OUTPUT_CONTRACT.md + docs/competition-control-loop.md + docs/norway-sources.md).
  This is a third-party baseline, NOT official; where it differs from the challenge page,
  the challenge page wins (see Ambiguities).

## Task definition
- Build a **company research agent**. Given a Norwegian organisation number, it collects
  public facts: company details, people, locations, financial results, jobs, public
  activity — wherever each is available.
- Every fact must be checked to belong to the right company and linked to its source
  and date. Anything not found is marked as not found (never invented, never zeroed).
- Profiles must be **kept current**: re-check sources, report material changes, preserve
  earlier evidence, and avoid duplicate records on refresh (refresh idempotence).
- The agent works from the **company number**, including companies it has never seen
  before — no pre-baked company list of its own.

## Starting universe
- 411,160 eligible Norwegian companies with 2025 annual-account records (Brønnøysund
  registry). Used for local testing (any scale incl. 1000+); local precomputed profiles
  are never submitted and never scored.

## Per-company output states (one result per input company)
- `available` — found it
- `not_available` — looked, nothing there
- `blocked` — the source refused the request
- `not_applicable` — the question does not apply to this company
- `ambiguous` — could not be sure it is the right company (use instead of guessing)
- `failed` — the run broke on this company

Rules: return a result for every supplied company; a missing row = run not scored.
"I found nothing" is a valid answer; a missing row is not. Never replace missing
values with zero. A source that was checked and is empty (zero jobs / zero locations)
is different from a source never checked.

## Official run / data contract
- Builderr supplies the same official company batch to every eligible entry at run
  time (chosen after the cutoff). The shared set can grow (e.g. 1,000 → 1,100);
  when it grows, every active entry runs on the same current set.
- Local 100-company smoke test is practice only, not the competition score.
- Each official run has a **fixed time and resource budget** — exact numbers are NOT
  published on the challenge page.
- Agent reads the batch from the file handed to it at run time (JSONL of organisation
  numbers per the reference implementation). An agent that researches its own company
  list cannot be scored.
- Minimal envelope per input (per OUTPUT_CONTRACT.md, reference repo):
  `organisation_number`, `run` {run_id, started_at, completed_at, terminal_status},
  `claims` [{field, value, availability, confidence, evidence_ids}],
  `evidence` [{id, source_url, source_class, retrieved_at, content_sha256, claim_span}],
  `changes` [], `errors` [],
  `operations` {requests, runtime_ms, third_party_cost_usd}.
- Submission contract (reference repo, matches page): pinned deps + reproducible setup,
  previous-snapshot input + material-change output (refresh replay), machine-readable
  run report (runtime, request count, third-party cost), declared models/APIs/licences
  and source-rights assumptions.
- Install check: clone repo at pinned commit into a new folder, empty virtualenv, run
  ONLY the declared install step, then the run command — it must work.
- Exactly **one documented, paste-able command** that accepts the batch file. No
  multi-step instructions or notebooks.
- Timeout or dropped results ⇒ run not scored (no score, no qualification credit).
  Builderr infrastructure failures are rerun and not assigned to the builder.

## Scoring breakdown (qualification bar: 65/100 overall)
- **Recall & coverage — 50 pts.** Compared against a combined checked collection built
  from all submissions + Builderr crawlers, de-duplicated, wrong-company matches
  removed. Per information type: 70% = share of companies covered, 30% = share of
  checked facts found. New verified findings update the collection and everyone's
  scores; finalized after all eligible final submissions.
- **Precision & evidence — 30 pts.** Each fact must belong to the right company with
  a valid source and date. Wrong/unsupported facts lose points. A **material
  wrong-company match blocks qualification** (score stays visible; run cannot become
  official until corrected). Key doctrine: it is better to miss information than to
  publish it under the wrong company — when the match is uncertain, return
  `ambiguous`/`not_available`.
- **Synthesis — 12 pts.** Profile explains what the company does, what changed, and
  what remains unknown, with sources for its conclusions.
- **UX — 8 pts.** A user can find, compare and verify company info on desktop and
  mobile.
- The four components are NOT separate thresholds — only the 65/100 overall bar.
- Ranking: average of every scheduled daily batch while the submitted code is active.
  Code failure/missed batch ⇒ that batch scores zero (after a clean evaluator rerun).
  Tiebreaks in exact order: (1) fewer wrong-company facts, (2) better weighted company
  recall, (3) lower declared third-party API cost.

## Revisions / versions
- First submission = version 1. Up to **four revised commit hashes** before **Oct 18**
  (5 versions total). Each revision is frozen before its next official run and applies
  only to later batches. Entries close **Oct 21**.

## Smoke-test report (required in submission)
- A 100-company smoke-test result or run report showing one complete result per input.
- Reference runner emits: envelopes JSONL + profiles JSONL + report JSON (run id,
  runtime, request counts, third-party cost) — i.e. `out/smoke-envelopes.jsonl`,
  `out/smoke-profiles.jsonl`, `out/smoke-report.json`.

## Submission checklist (email to submit@builderr.ai)
1. Repository URL + exact commit hash.
2. One run command (accepts a JSONL batch of organisation numbers).
3. 100-company smoke-test result / report.
4. Models, APIs and licences used.
5. Expected cost per official run.
6. Agent name + contact details.
7. (Implied by contract) Source-rights assumptions; previous-snapshot input +
   material-change output evidence.

## Budgets & resources
- Time/resource budget per official run: fixed but unpublished (see Ambiguities).
- LLM/API: a model MAY be used. There is a small external API budget per run; a
  model key for scoring can be requested from Builderr. **Cannot** use a credential
  tied to a personal account on another service (unreproducible runs).
- Cost is a tiebreaker, so declare honest third-party cost per 100-company run.
- Prizes: $2,500 total — Main challenge $2,000 ($1,200/$500/$300); separate JBOX
  bonus $500 ($250/$150/$100) for agents built with JBOX; Community awards $100 × 4
  (Sep 6, Sep 20, Oct 4, Oct 18; public voting never changes the technical ranking).
- Winner gets an opportunity to partner with Håvard Liltved Dalen (CPO Fronted,
  co-founder JBOX) to launch Signalpost in Norway. Qualified entries enter the
  hosted Builderr gallery; Builderr covers hosting during the competition.

## Source rules (legal/safety)
- Open-source code does NOT grant permission to scrape a platform. Follow each
  source's terms, robots policy, rate limits, licence.
- LinkedIn, Meta, Indeed: useful for identity/discovery, but direct automated
  collection may be restricted ⇒ use permitted APIs, licensed providers,
  company-owned outbound links, or return `blocked`/`not_available`.
- Brønnøysund endpoints (reference repo norway-sources.md): bulk CSV download,
  live entity API, roles, group structure, sub-units, normalized accounts, filing
  years (rate-limited ~30 req/min), annual-account PDFs. A 410 response ⇒ remove any
  cached copy. Person data only in company-centric role context; birth dates are
  discarded, not stored/displayed. 404 on accounts ⇒ no returned record, not zero.
  Many sole proprietorships do not file; AS/ASA generally do.

## Ambiguities / contradictions to resolve
1. **Time/resource budget STILL unpublished (2026-10-11 live recon).** The challenge page, brief, and the official evaluation contract (https://builderr.ai/docs/signalpost-evaluation-harness.md — found via the "Full evaluation contract" link; Scoring version 2, effective 26 Aug 2026; random daily evaluation began 24 Aug 2026) all state only "a fixed time and resource budget" — NO seconds/minutes, NO RAM/CPU, NO request-rate/concurrency limits anywhere on the site. The brief mentions a small external API budget per run and that Builderr will supply a model key for scoring if needed. Needed for architecture (concurrency, LLM per-company limits). ⇒ Ask via Builderr Discord (https://discord.gg/ytBsTqkkes) / inquiries@builderr.ai before Oct 18.
2. **"External-first score of at least 80/100"** appears in the reference repo's
   competition-control-loop.md — that is the reference author's own target, NOT
   official. Official bar is 65/100 overall on an official run. Do not confuse.
3. ~~The evaluation contract (linked from the challenge page as the exact scoring terms and run format) was not accessible via page text.~~ RESOLVED 2026-10-11 (live browser read): the contract is at https://builderr.ai/docs/signalpost-evaluation-harness.md. STILL unresolved inside it: the exact runtime input-file format (e.g. whether it is JSONL of org numbers) is NOT explicitly specified; deadline timezone/hours for the Oct 21 close and Oct 18 revision freeze are NOT published anywhere (page banner "Ends Oct 21"; brief "Round 1 runs from 23 August through 21 October 2026"; revisions "by 18 October" — no time/timezone).
4. **Batch growth + version freezing interaction**: revisions apply "only to later
   batches" while the shared set "can grow over time" — fine, but ranking averages
   "every scheduled daily batch while your code is active," so early submissions
   accumulate batches; confirm how a v1 submission date affects the averaging window.
5. **"Same checked company collection"**: new verified findings update the
   collection and everyone's scores — meaning rivals' evidence can raise the bar
   mid-competition. Local self-scoring can only approximate the hidden collection.
6. **UX scoring** is based on the output profiles being easy to find/compare/verify —
   the rendered form of profiles matters (reference repo includes a sample site);
   our submission needs a presentable output format.
7. NEW (2026-10-11): batch-size DISCREPANCY — the public board review note says entries were recalculated against "the same locked 1,500-company set", while the contract says the official batch is 1,000 companies (may grow to 1,100). Flag to support when asking the budget questions. Board status 2026-10-11: OPEN, 37 submissions / 28 assessed / **0 qualified** — weak field; daily evaluation running.
8. NEW (2026-10-11): challenge-page resources available for the Oct 18 build — starter brief (/starter-briefs/signalpost.md), runnable starter kit (signalpost-starter-kit.tar.gz), full 411,160-company universe file (.jsonl.gz with published SHA-256), agent playbook, learning harness, source policy, permitted sources, sample site at builderr.ai/signalpost. Fabrication guardrail in contract: fabricated financial values or a material wrong-company publication prevent a run from becoming official.
