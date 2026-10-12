# Day-2 work log (Oct 12, 2026)

## Task 1 — live Brønnøysund exercise (production endpoints)
- Probed 5 real org numbers from the public universe file
  (923609016 EQUINOR ASA, 982463718 TELENOR ASA, 984851006 DNB BANK ASA,
  810034882 SANDNES ELEKTRISKE AS, 810059672 AASEN & FARSTAD AS).
- 25 live requests: entity + roles + accounts + sub-units + konsernstruktur.
  Entity-match gate passes on real registry JSON: 5/5.
- Full log: `out/live-exercise-task1.json`.
- New endpoints verified live and wired in:
  - `GET /enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100`
  - `GET /enhetsregisteret/api/konsernstruktur/{org}` (hierarchical; 404 when none)
- Transport flakiness observed (sandbox proxy resets): `_get` now retries
  transient transport errors with backoff (3 attempts). HTTP statuses still
  map 1:1 to claim states (404→not_available, 410→drop cache, 429/5xx→blocked).

## Bugs found by exercising against REAL data (all fixed + regression-tested)
1. **Accounts endpoint returns a LIST of year-records, not a dict.**
   Day-1 `_summarize_accounts` returned None for every company with filings.
   Fixed: pick latest by `regnskapsperiode.tilDato`, extract nested figures
   (`resultatregnskapResultat.driftsresultat.*`, `eiendeler.sumEiendeler`, …).
2. **Roles endpoint shape is `{"rollegrupper": [{"roller": [...]}]}`**,
   not `{"roller": [...]}` — Day-1 parsed the wrong key, so key_people was
   0/100 available on the first live run. Fixed; also skips `avregistrert`
   and `erDoed` holders. Person `navn` is `{fornavn, etternavn}`; `fodselsdato`
   still discarded at the source layer (verified: never in claim values).
3. **Production addresses are lists** (`beliggenhetsadresse.adresse`,
   `forretningsadresse.adresse`) — crashed `", ".join` in Day-1 code (95/100
   failed envelopes on the first live run). Fixed with list-tolerant join.
4. **Registry `hjemmeside` often lacks a scheme** (`www.realkon.no`) →
   `Invalid URL: No scheme supplied` → failed footprint/jobs claims. Fixed:
   `_normalize_url` defaults to https (normalization, not a new claim).
5. **Request accounting bug**: envelope `operations.requests` read the
   shared source's cumulative counter → report total 5563 for 100 companies.
   Fixed: thread-local per-envelope deltas.

## Task 2 — 100-company live smoke test (FINAL: run day2-live-smoke-100d)
- Batch: systematic sample (every 4111th) from the 411,160-company universe
  → `batch100.jsonl`. Run: `SIGNALPOST_LIVE=1 … -m signalpost.main … --workers 2`
- Result: exit 0, 100/100 terminal `completed`, 0 simulated flags,
  **515 honest requests** (thread-local per-envelope accounting),
  self-score **78.47/100** (qualifies_proxy: True vs the 70 margin).
- Claim mix: 968 available / 727 not_available / 2 blocked / 3 failed.
  Per-field: legal_name/form/status/industry 100/100; key_people 100/100
  (after the rollegrupper fix — was 0/100); accounts 98/100 (2 blocked on
  HTTP 500); sub-units 73/100; konsern 9/100; website identity 4 available;
  open jobs 1 available. Remaining 3 failed claims = sandbox proxy
  transport flakiness on company websites (retried, still unreachable from
  this environment), not code bugs.
- Outputs: `out/envelopes.jsonl`, `out/profiles.jsonl`, `out/run-report.json`,
  `out/report.html` (presentable, 100 companies, evidence links + source
  dates + sha256).

## Task 3 — footprint recall levers (lawful only)
- Sub-units endpoint + konsernstruktur (official layer, NLOD 2.0).
- Open jobs from company-owned careers pages: link discovery on the
  registry-listed homepage, robots-respecting, entity-anchored (org number
  or exact legal name in page text) — otherwise `ambiguous`, never available.
- Official-layer recall additions: stated_purpose, phone, former_names.

## Task 4 — HTML profile report
- `python -m signalpost.html_report --envelopes … --profiles … --report … \
    --output out/report.html` → self-contained, mobile-responsive page:
  per-company claims with state badges, evidence links, retrieved_at dates,
  sha256 fragments, material changes, filter box.

## Task 5 — precision audit (script: /tmp/precision_audit.py)
- Run on the FINAL 100d outputs. Slice 1 (first 20 envelopes): 205 available
  claims, 205 evidence records validated — every one present, valid 64-hex
  sha256, entity-anchored (org number in registry URL / claim span), zero
  simulated evidence in live mode; non-available claims carry no value.
  **0 failures, 0 warnings — PASS.**
- Slice 2 (12 footprint-rich envelopes: konsern/jobs/website claims):
  143 available claims, 0 failures — PASS.
- No first-token-style match bugs found: the exact-entity gate (org number
  verbatim or full legal name) held on all company_owned evidence.
