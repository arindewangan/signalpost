"""ResearchAgent: per-company research pipeline.

Pipeline per input organisation number:
  1. Identity anchor — normalize the 9-digit org number. Invalid => FAILED
     identity claim; one envelope is still emitted (a missing row is not scorable).
  2. Official Brønnøysund layer — live entity endpoint (name, form, status,
     industry, address, website, employees), accounts, roles. HTTP errors mapped
     per the plan: 404 => not_available, 410 => drop cache (not_available with
     note), 429/timeout => blocked, exception => failed envelope.
  3. Entity-match gate — a claim becomes `available` only when its evidence names
     the exact legal entity (org number or exact legal name). Registry rows are
     keyed by org number, so the official layer passes by construction; the
     company-website layer must find the org number/exact name in the page text.
  4. External footprint (minimal, honest) — company-owned website only, from the
     registry record. Page references the org number/exact legal name => the
     footprint claim is available; fetched but no reference => ambiguous, never
     available. robots refusal => blocked. Never LinkedIn/Meta/Indeed.

Doctrine: never invent evidence. No evidence => a claim cannot be `available`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .batch import BatchInput, normalize_org_number
from .classifier import classify_field, evidence_refs_entity
from .evidence import make_evidence, sha256_text, utc_now_iso
from .sources import (
    BlockedError,
    BrregSource,
    GoneError,
    SourceError,
    WebsiteOutcome,
    WebsiteSource,
    _normalize_url,
)
from .states import CompanyState

AGENT_NAME = "signalpost"
SOURCE_CLASS_OFFICIAL = "official"
SOURCE_CLASS_COMPANY_OWNED = "company_owned"

# Claim fields emitted per company (official layer + footprint).
FIELD_LEGAL_NAME = "legal_name"
FIELD_ORG_FORM = "organisation_form"
FIELD_STATUS = "registration_status"
FIELD_INDUSTRY = "industry_code"
FIELD_ADDRESS = "business_address"
FIELD_WEBSITE = "website"
FIELD_EMAIL = "email"
FIELD_EMPLOYEES = "employee_count"
FIELD_PURPOSE = "stated_purpose"
FIELD_PHONE = "phone"
FIELD_FORMER_NAMES = "former_names"
FIELD_ACCOUNTS = "financials_latest"
FIELD_ROLES = "key_people"
FIELD_SUBUNITS = "sub_units"
FIELD_KONSERN = "konsern_structure"
FIELD_FOOTPRINT = "website_identity_confirmed"
FIELD_JOBS = "open_jobs"

# Every claim field the agent emits, in order. Used for the 404 and
# not_available-all paths so no field is ever silently dropped.
ALL_CLAIM_FIELDS = (
    FIELD_LEGAL_NAME, FIELD_ORG_FORM, FIELD_STATUS, FIELD_INDUSTRY,
    FIELD_ADDRESS, FIELD_WEBSITE, FIELD_EMAIL, FIELD_EMPLOYEES,
    FIELD_PURPOSE, FIELD_PHONE, FIELD_FORMER_NAMES,
    FIELD_ACCOUNTS, FIELD_ROLES, FIELD_SUBUNITS, FIELD_KONSERN,
    FIELD_FOOTPRINT, FIELD_JOBS,
)


class ResearchAgent:
    """Runs the per-company pipeline. One envelope per input, always."""

    def __init__(
        self,
        *,
        source: BrregSource | None = None,
        website_source: WebsiteSource | None = None,
        run_id: str = "manual",
        simulated: bool | None = None,
    ) -> None:
        self.source = source or BrregSource()
        self.website_source = website_source or WebsiteSource()
        self.run_id = run_id
        self.simulated = self.source.simulated if simulated is None else simulated
        self._evidence_seq = 0

    # ------------------------------------------------------------------
    # claim helpers
    # ------------------------------------------------------------------
    def _web_requests(self) -> int:
        thread_requests = getattr(self.website_source, "thread_requests", None)
        if callable(thread_requests):
            return thread_requests()
        return int(getattr(self.website_source, "requests_made", 0) or 0)

    def _next_evidence_id(self) -> str:
        self._evidence_seq += 1
        return f"ev-{self._evidence_seq:05d}"

    def _emit_evidence(
        self,
        evidence: list[dict],
        *,
        source_url: str,
        source_class: str,
        claim_span: str,
        content: str,
    ) -> str:
        evidence_id = self._next_evidence_id()
        evidence.append(
            make_evidence(
                evidence_id=evidence_id,
                source_url=source_url,
                source_class=source_class,
                claim_span=claim_span,
                content=content,
                simulated=self.simulated,
            )
        )
        return evidence_id

    def _claim(
        self,
        claims: list[dict],
        *,
        field: str,
        value: Any,
        state: CompanyState,
        confidence: float,
        evidence_ids: list[str] | None = None,
    ) -> None:
        claims.append(
            {
                "field": field,
                "value": value,
                "availability": state.value,
                "confidence": round(confidence, 3),
                "evidence_ids": evidence_ids or [],
            }
        )

    def _official_claim(
        self,
        claims: list[dict],
        evidence: list[dict],
        *,
        field: str,
        value: Any,
        entity: dict[str, Any],
        source_url: str,
    ) -> None:
        """Official-layer claim: evidence is the registry payload itself, which is
        keyed by org number — the entity-match gate passes by construction."""
        raw = json.dumps(entity, ensure_ascii=False, sort_keys=True)
        entity_url = source_url
        # The gate is honest here too: the registry row must name the entity.
        match_ok = evidence_refs_entity(
            entity.get("organisasjonsnummer", ""), entity.get("navn"), raw
        )
        state, conf = classify_field(
            value=value, source_checked=True, entity_match_ok=match_ok
        )
        evidence_ids: list[str] = []
        if state is CompanyState.AVAILABLE:
            evidence_ids = [
                self._emit_evidence(
                    evidence,
                    source_url=entity_url,
                    source_class=SOURCE_CLASS_OFFICIAL,
                    claim_span=f"registry record for org {entity.get('organisasjonsnummer')}",
                    content=raw,
                )
            ]
        self._claim(claims, field=field, value=value if state is CompanyState.AVAILABLE else None,
                    state=state, confidence=conf, evidence_ids=evidence_ids)

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def research(self, batch_input: BatchInput | str) -> dict:
        """Research one company. Returns the terminal envelope (always one)."""
        started_at = utc_now_iso()
        t0 = time.perf_counter()
        if isinstance(batch_input, str):
            org = normalize_org_number(batch_input) or ""
        else:
            org = batch_input.organisation_number

        claims: list[dict] = []
        evidence: list[dict] = []
        errors: list[str] = []

        # Per-envelope request accounting: thread-local deltas, so the shared
        # sources' counters are attributed to the company researched here.
        src_before = self.source.thread_requests()
        web_before = self._web_requests()

        terminal = "completed"
        try:
            if not org:
                terminal = "failed"
                state, conf = classify_field(value=None, source_checked=False)
                self._claim(
                    claims,
                    field="identity",
                    value=None,
                    state=state,
                    confidence=conf,
                )
                errors.append("invalid organisation number: not 9 digits")
            else:
                self._research_company(org, claims, evidence, errors)
        except BlockedError as exc:
            errors.append(f"blocked: {exc}")
            self._mark_remaining_blocked(
                claims, FIELD_ACCOUNTS, FIELD_ROLES, FIELD_SUBUNITS, FIELD_KONSERN
            )
        except GoneError as exc:
            errors.append(f"registry 410 (cached copy dropped): {exc}")
            self._not_available_all(claims)
        except Exception as exc:  # noqa: BLE001 - every input still gets an envelope
            terminal = "failed"
            errors.append(f"run error: {type(exc).__name__}: {exc}")
            if not claims:
                state, conf = classify_field(value=None, source_checked=False)
                self._claim(claims, field="identity", value=None, state=state, confidence=conf)

        runtime_ms = int((time.perf_counter() - t0) * 1000)
        envelope = {
            "organisation_number": org,
            "run": {
                "run_id": self.run_id,
                "started_at": started_at,
                "completed_at": utc_now_iso(),
                "terminal_status": terminal,
            },
            "claims": claims,
            "evidence": evidence,
            "changes": [],
            "errors": errors,
            "operations": {
                "requests": (self.source.thread_requests() - src_before)
                + (self._web_requests() - web_before),
                "runtime_ms": runtime_ms,
                "third_party_cost_usd": 0.0,
            },
        }
        if self.simulated:
            envelope["simulated"] = True
        return envelope

    # ------------------------------------------------------------------
    # pipeline stages
    # ------------------------------------------------------------------
    def _research_company(
        self, org: str, claims: list[dict], evidence: list[dict], errors: list[str]
    ) -> None:
        # Stage 1 — identity anchor: the official registry record.
        entity_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
        try:
            entity = self.source.fetch_entity(org)
        except GoneError:
            raise
        except BlockedError:
            raise
        except SourceError as exc:
            raise RuntimeError(str(exc)) from exc

        if entity is None:
            # Looked, nothing there: 404 from the registry.
            for field in ALL_CLAIM_FIELDS:
                state, conf = classify_field(value=None, source_checked=True)
                self._claim(claims, field=field, value=None, state=state, confidence=conf)
            errors.append("registry has no record for this organisation number (404)")
            return

        entity = dict(entity)
        entity.setdefault("organisasjonsnummer", org)
        legal_name = entity.get("navn")

        def _of(d: Any, key: str) -> str | None:
            if isinstance(d, dict):
                v = d.get(key)
                return str(v) if v else None
            return None

        address = entity.get("forretningsadresse") or {}
        addr = self._join_address(address)

        orgform = entity.get("organisasjonsform")
        orgform_label = None
        if isinstance(orgform, dict):
            orgform_label = orgform.get("beskrivelse") or orgform.get("kode")

        official_fields = [
            (FIELD_LEGAL_NAME, legal_name),
            (FIELD_ORG_FORM, orgform_label),
            (FIELD_STATUS, entity.get("status") or self._derive_status(entity)),
            (FIELD_INDUSTRY, _of(entity.get("naeringskode1"), "beskrivelse")),
            (FIELD_ADDRESS, addr),
            (FIELD_WEBSITE, entity.get("hjemmeside")),
            (FIELD_EMAIL, entity.get("epostadresse")),
            (FIELD_EMPLOYEES, entity.get("antallAnsatte")),
            (FIELD_PURPOSE, entity.get("vedtektsfestetFormaal")),
            (FIELD_PHONE, entity.get("telefon")),
            (FIELD_FORMER_NAMES, self._former_names(entity)),
        ]
        for field, value in official_fields:
            self._official_claim(claims, evidence, field=field, value=value,
                                 entity=entity, source_url=entity_url)

        # Stage 2 — financials (normalized annual accounts).
        self._research_accounts(org, claims, evidence, errors, entity)

        # Stage 3 — key people (roles endpoint; birth dates discarded in sources).
        self._research_roles(org, claims, evidence, errors, legal_name)

        # Stage 4 — sub-units (underenheter) of this entity.
        self._research_subunits(org, claims, evidence, errors)

        # Stage 5 — konsern (group) structure.
        self._research_konsern(org, claims, evidence, errors, entity)

        # Stage 6 — minimal external footprint: company-owned website only.
        self._research_website(org, claims, evidence, errors, legal_name, entity)

        # Stage 7 — open jobs from company-owned careers pages (entity-anchored).
        self._research_jobs(org, claims, evidence, errors, legal_name, entity)

    @staticmethod
    def _join_address(address: dict[str, Any]) -> str | None:
        """Join address parts; production returns `adresse` as a list of lines."""

        def _part(value: Any) -> str:
            if isinstance(value, list):
                return ", ".join(str(p) for p in value if p)
            return str(value) if value else ""

        joined = ", ".join(
            p for p in (
                _part(address.get("adresse")),
                _part(address.get("postnummer")),
                _part(address.get("poststed")),
            ) if p
        )
        return joined or None

    @staticmethod
    def _former_names(entity: dict[str, Any]) -> list[str] | None:
        names = [h.get("navn") for h in entity.get("historiskeNavn", []) if h.get("navn")]
        return names or None

    @staticmethod
    def _derive_status(entity: dict[str, Any]) -> str | None:
        if entity.get("konkurs"):
            return "bankrupt"
        if entity.get("underAvvikling"):
            return "under liquidation"
        if entity.get("slettedato"):
            return "deleted"
        return "active" if entity.get("navn") else None

    def _research_accounts(
        self, org: str, claims: list[dict], evidence: list[dict],
        errors: list[str], entity: dict[str, Any],
    ) -> None:
        url = f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
        try:
            accounts = self.source.fetch_accounts(org)
        except GoneError as exc:
            errors.append(f"accounts 410 (cached copy dropped): {exc}")
            accounts = None
        except BlockedError as exc:
            state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
            self._claim(claims, field=FIELD_ACCOUNTS, value=None, state=state, confidence=conf)
            errors.append(f"accounts blocked: {exc}")
            return
        except SourceError as exc:
            raise RuntimeError(str(exc)) from exc

        if accounts is None:
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=FIELD_ACCOUNTS, value=None, state=state, confidence=conf)
            return

        summary = self._summarize_accounts(accounts)
        raw = json.dumps(accounts, ensure_ascii=False, sort_keys=True)[:20000]
        state, conf = classify_field(value=summary, source_checked=True, entity_match_ok=True)
        evidence_ids: list[str] = []
        if state is CompanyState.AVAILABLE:
            evidence_ids = [
                self._emit_evidence(
                    evidence, source_url=url, source_class=SOURCE_CLASS_OFFICIAL,
                    claim_span=f"annual accounts for org {org}", content=raw,
                )
            ]
        self._claim(claims, field=FIELD_ACCOUNTS,
                    value=summary if state is CompanyState.AVAILABLE else None,
                    state=state, confidence=conf, evidence_ids=evidence_ids)

    @staticmethod
    def _summarize_accounts(accounts: list[dict[str, Any]] | dict[str, Any] | None) -> dict[str, Any] | None:
        """Keep only key, non-fabricated figures from the latest filing.

        The production endpoint returns a LIST of year-records; the latest is
        the one with the greatest regnskapsperiode.tilDato. Never zero-fill
        missing data: only figures actually present are included.
        """
        if isinstance(accounts, dict):
            records = [accounts]
        elif isinstance(accounts, list):
            records = [r for r in accounts if isinstance(r, dict)]
        else:
            return None
        if not records:
            return None

        def _til_dato(rec: dict[str, Any]) -> str:
            return str((rec.get("regnskapsperiode") or {}).get("tilDato") or "")

        latest = max(records, key=_til_dato)

        def _get(rec: dict[str, Any], *path: str) -> Any:
            node: Any = rec
            for key in path:
                if not isinstance(node, dict):
                    return None
                node = node.get(key)
            return node if isinstance(node, (int, float, str)) else None

        res = latest.get("resultatregnskapResultat") or {}
        summary = {
            "fiscal_year": _get(latest, "regnskapsperiode", "tilDato"),
            "currency": _get(latest, "valuta"),
            "revenue": _get(res, "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "operating_result": _get(res, "driftsresultat", "driftsresultat"),
            "net_result": _get(res, "aarsresultat"),
            "total_assets": _get(latest, "eiendeler", "sumEiendeler"),
            "total_equity_and_debt": _get(latest, "egenkapitalGjeld", "sumEgenkapitalGjeld"),
        }
        summary = {k: v for k, v in summary.items() if v is not None}
        return summary or None

    def _research_roles(
        self, org: str, claims: list[dict], evidence: list[dict],
        errors: list[str], legal_name: str | None,
    ) -> None:
        url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
        try:
            roles = self.source.fetch_roles(org)
        except BlockedError as exc:
            state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
            self._claim(claims, field=FIELD_ROLES, value=None, state=state, confidence=conf)
            errors.append(f"roles blocked: {exc}")
            return
        except (GoneError, SourceError) as exc:
            errors.append(f"roles fetch issue: {exc}")
            roles = None

        if roles is None:
            state, conf = classify_field(value=None, source_checked=False)
        else:
            state, conf = classify_field(value=roles or None, source_checked=True)
        evidence_ids: list[str] = []
        if state is CompanyState.AVAILABLE and roles:
            evidence_ids = [
                self._emit_evidence(
                    evidence, source_url=url, source_class=SOURCE_CLASS_OFFICIAL,
                    claim_span=f"registered roles for org {org}",
                    content=json.dumps(roles, ensure_ascii=False, sort_keys=True),
                )
            ]
        self._claim(claims, field=FIELD_ROLES,
                    value=roles if state is CompanyState.AVAILABLE else None,
                    state=state, confidence=conf, evidence_ids=evidence_ids)

    def _research_subunits(
        self, org: str, claims: list[dict], evidence: list[dict], errors: list[str],
    ) -> None:
        """Registered sub-units (underenheter). The query is keyed by the org
        number, so the entity-match gate passes by construction."""
        url = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100"
        try:
            subunits = self.source.fetch_subunits(org)
        except GoneError as exc:
            errors.append(f"sub-units 410 (cached copy dropped): {exc}")
            subunits = None
        except BlockedError as exc:
            state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
            self._claim(claims, field=FIELD_SUBUNITS, value=None, state=state, confidence=conf)
            errors.append(f"sub-units blocked: {exc}")
            return
        except SourceError as exc:
            raise RuntimeError(str(exc)) from exc

        if subunits is None:
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=FIELD_SUBUNITS, value=None, state=state, confidence=conf)
            errors.append("sub-units lookup returned no registry record (404)")
            return

        state, conf = classify_field(value=subunits or None, source_checked=True,
                                     entity_match_ok=True)
        evidence_ids: list[str] = []
        if state is CompanyState.AVAILABLE:
            evidence_ids = [
                self._emit_evidence(
                    evidence, source_url=url, source_class=SOURCE_CLASS_OFFICIAL,
                    claim_span=f"registered sub-units of org {org}",
                    content=json.dumps(subunits, ensure_ascii=False, sort_keys=True),
                )
            ]
        self._claim(claims, field=FIELD_SUBUNITS,
                    value=subunits if state is CompanyState.AVAILABLE else None,
                    state=state, confidence=conf, evidence_ids=evidence_ids)

    def _research_konsern(
        self, org: str, claims: list[dict], evidence: list[dict],
        errors: list[str], entity: dict[str, Any],
    ) -> None:
        """Group structure: hierarchical konsern data + the entity's erIKonsern
        flag. Query keyed by org number => gate passes by construction."""
        url = f"https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
        try:
            struktur = self.source.fetch_konsernstruktur(org)
        except GoneError as exc:
            errors.append(f"konsernstruktur 410 (cached copy dropped): {exc}")
            struktur = None
        except BlockedError as exc:
            state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
            self._claim(claims, field=FIELD_KONSERN, value=None, state=state, confidence=conf)
            errors.append(f"konsernstruktur blocked: {exc}")
            return
        except SourceError as exc:
            raise RuntimeError(str(exc)) from exc

        value = None
        if struktur is not None:
            value = {
                "in_konsern": bool(entity.get("erIKonsern")),
                "total_subsidiaries": struktur.get("total_subsidiaries", 0),
                "subsidiaries": struktur.get("subsidiaries", []),
            }
        state, conf = classify_field(value=value, source_checked=True, entity_match_ok=True)
        evidence_ids: list[str] = []
        if state is CompanyState.AVAILABLE:
            evidence_ids = [
                self._emit_evidence(
                    evidence, source_url=url, source_class=SOURCE_CLASS_OFFICIAL,
                    claim_span=f"konsern structure for org {org}",
                    content=json.dumps(struktur, ensure_ascii=False, sort_keys=True),
                )
            ]
        self._claim(claims, field=FIELD_KONSERN,
                    value=value if state is CompanyState.AVAILABLE else None,
                    state=state, confidence=conf, evidence_ids=evidence_ids)

    def _research_jobs(
        self, org: str, claims: list[dict], evidence: list[dict],
        errors: list[str], legal_name: str | None, entity: dict[str, Any],
    ) -> None:
        """Open positions from the company-owned careers pages. Published ONLY
        when a careers page names the exact entity; otherwise AMBIGUOUS."""
        homepage = entity.get("hjemmeside")
        if not homepage:
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=FIELD_JOBS, value=None, state=state, confidence=conf)
            return
        homepage = _normalize_url(homepage)
        if self.simulated:
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=FIELD_JOBS, value=None, state=state, confidence=conf)
            errors.append(f"jobs check skipped in simulated mode (registry lists {homepage})")
            return
        fetch_careers = getattr(self.website_source, "fetch_careers", None)
        if fetch_careers is None:
            state, conf = classify_field(value=None, source_checked=False)
            self._claim(claims, field=FIELD_JOBS, value=None, state=state, confidence=conf)
            errors.append("jobs check not performed: careers connector unavailable")
            return
        outcome, payload = fetch_careers(homepage, org, legal_name or "")
        if outcome == WebsiteOutcome.MATCHED:
            jobs = payload.get("jobs", [])
            state, conf = classify_field(value=jobs or None, source_checked=True,
                                         entity_match_ok=True)
            evidence_ids: list[str] = []
            if state is CompanyState.AVAILABLE:
                evidence_ids = [
                    self._emit_evidence(
                        evidence, source_url=payload.get("source_page", homepage),
                        source_class=SOURCE_CLASS_COMPANY_OWNED,
                        claim_span=f"open positions listed by org {org}",
                        content=payload.get("page_text", ""),
                    )
                ]
                value = {"count": len(jobs), "titles": jobs}
            else:
                value = None
            self._claim(claims, field=FIELD_JOBS, value=value,
                        state=state, confidence=conf, evidence_ids=evidence_ids)
            if state is CompanyState.NOT_AVAILABLE:
                errors.append(
                    f"careers section checked for org {org}: no open positions listed"
                )
        elif outcome == WebsiteOutcome.FETCHED_NO_REFERENCE:
            # Uncertain match => ambiguous, NEVER available.
            state, conf = classify_field(value=None, source_checked=True, entity_match_ok=False)
            self._claim(claims, field=FIELD_JOBS, value=None, state=state, confidence=conf)
            errors.append(f"careers page found but does not reference org {org}; marked ambiguous")
        elif outcome in (WebsiteOutcome.BLOCKED, WebsiteOutcome.BLOCKED_ROBOTS):
            state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
            self._claim(claims, field=FIELD_JOBS, value=None, state=state, confidence=conf)
            errors.append(f"jobs check blocked ({outcome}): {payload}")
        else:
            state, conf = classify_field(value=None, source_checked=False)
            self._claim(claims, field=FIELD_JOBS, value=None, state=state, confidence=conf)
            errors.append(f"jobs check failed: {payload}")

    def _research_website(
        self, org: str, claims: list[dict], evidence: list[dict],
        errors: list[str], legal_name: str | None, entity: dict[str, Any],
    ) -> None:
        homepage = entity.get("hjemmeside")
        if not homepage:
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=FIELD_FOOTPRINT, value=None, state=state, confidence=conf)
            return
        homepage = _normalize_url(homepage)
        if self.simulated:
            # Offline mode never pretends to have fetched a website.
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=FIELD_FOOTPRINT, value=None, state=state, confidence=conf)
            errors.append(f"website check skipped in simulated mode (registry lists {homepage})")
            return
        outcome, payload = self.website_source.fetch(homepage, org, legal_name or "")
        if outcome == WebsiteOutcome.MATCHED:
            evidence_id = self._emit_evidence(
                evidence, source_url=homepage, source_class=SOURCE_CLASS_COMPANY_OWNED,
                claim_span=f"company homepage references org {org}",
                content=payload[:20000],
            )
            state, conf = classify_field(value=homepage, source_checked=True, entity_match_ok=True)
            self._claim(claims, field=FIELD_FOOTPRINT, value=homepage,
                        state=state, confidence=conf, evidence_ids=[evidence_id])
        elif outcome == WebsiteOutcome.FETCHED_NO_REFERENCE:
            # Uncertain match => ambiguous, NEVER available.
            state, conf = classify_field(value=None, source_checked=True, entity_match_ok=False)
            self._claim(claims, field=FIELD_FOOTPRINT, value=None, state=state, confidence=conf)
            errors.append(f"website fetched but does not reference org {org}; marked ambiguous")
        elif outcome in (WebsiteOutcome.BLOCKED, WebsiteOutcome.BLOCKED_ROBOTS):
            state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
            self._claim(claims, field=FIELD_FOOTPRINT, value=None, state=state, confidence=conf)
            errors.append(f"website check blocked ({outcome}): {payload}")
        else:
            state, conf = classify_field(value=None, source_checked=False)
            self._claim(claims, field=FIELD_FOOTPRINT, value=None, state=state, confidence=conf)
            errors.append(f"website check failed: {payload}")

    # ------------------------------------------------------------------
    # error-path helpers
    # ------------------------------------------------------------------
    def _mark_remaining_blocked(self, claims: list[dict], *fields: str) -> None:
        claimed = {c["field"] for c in claims}
        for field in fields:
            if field not in claimed:
                state, conf = classify_field(value=None, source_checked=True, source_blocked=True)
                self._claim(claims, field=field, value=None, state=state, confidence=conf)

    def _not_available_all(self, claims: list[dict]) -> None:
        for field in ALL_CLAIM_FIELDS:
            state, conf = classify_field(value=None, source_checked=True)
            self._claim(claims, field=field, value=None, state=state, confidence=conf)

    # ------------------------------------------------------------------
    # synthesis (deterministic; no LLM by default)
    # ------------------------------------------------------------------
    def build_profile(self, envelope: dict) -> dict:
        """Human-readable profile from an envelope: what it does, what changed,
        what remains unknown — with the claims it rests on."""
        org = envelope["organisation_number"]
        claims = {c["field"]: c for c in envelope["claims"]}
        available = {k: v for k, v in claims.items() if v["availability"] == "available"}
        unknown = [k for k, v in claims.items() if v["availability"] in ("not_available", "ambiguous", "blocked")]

        parts = []
        name = available.get(FIELD_LEGAL_NAME, {}).get("value")
        form = available.get(FIELD_ORG_FORM, {}).get("value")
        industry = available.get(FIELD_INDUSTRY, {}).get("value")
        if name:
            parts.append(f"{name} (org {org})")
            if form:
                parts.append(f"is registered as {form}")
            if industry:
                parts.append(f"in {industry}")
        elif claims.get(FIELD_LEGAL_NAME, {}).get("availability") == "not_available":
            parts.append(f"No registry record found for org number {org}.")
        if available.get(FIELD_ACCOUNTS):
            parts.append("Latest annual accounts retrieved from the accounts register.")
        summary = " ".join(parts).strip() or "No verified information available."

        return {
            "organisation_number": org,
            "summary": summary,
            "available_fields": sorted(available.keys()),
            "unknown_fields": sorted(unknown),
            "changes": envelope.get("changes", []),
            "evidence_count": len(envelope.get("evidence", [])),
            "llm": "disabled",
        }

    @staticmethod
    def evidence_hash_ok(record: dict) -> bool:
        """Audit helper: the record has the required fields and a real sha256."""
        required = ("source_url", "source_class", "retrieved_at", "content_sha256", "claim_span")
        if not all(record.get(k) for k in required):
            return False
        digest = record.get("content_sha256", "")
        return bool(isinstance(digest, str) and len(digest) == 64 and sha256_text(digest) != digest)
