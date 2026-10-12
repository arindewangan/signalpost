"""Source layer: Brønnøysund registry client.

Real endpoints (NLOD 2.0, public):
- bulk CSV:  https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv
- live:      https://data.brreg.no/enhetsregisteret/api/enheter/{org}
- roles:     https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller
- accounts:  https://data.brreg.no/regnskapsregisteret/regnskap/{org}

The `BrregSource` class below is wired for live HTTP but defaults to OFFLINE
(simulated fixtures) in this scaffold — set SIGNALPOST_LIVE=1 to enable live
requests. The simulated mode is HONESTLY LABELED: envelopes carry
"source_class": "simulated" and "simulated": true.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://data.brreg.no"
BULK_CSV_URL = f"{BASE}/enhetsregisteret/api/enheter/lastned/csv"

# Respect Brønnøysund's ~30 req/min guidance: ~2 req/s per host is the plan's cap.
BRREG_MIN_INTERVAL_S = 0.5

# Internal sentinel for "HTML fetched fine" inside WebsiteSource._fetch_html.
_HTML_OK = "ok"


def _normalize_url(url: str) -> str:
    """Registry website values often lack a scheme ('www.example.no'); default
    to https. This is normalization of the company's own listed address, not
    a new claim."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url.lstrip("/")
    return url

# Href fragments that hint at a company-owned careers/jobs section.
_CAREER_HINTS = (
    "karriere", "career", "ledige-stillinger", "ledig-stilling",
    "stillinger", "stilling", "/jobs", "/jobb", "vacancies",
)

# Tiny built-in fixture for the offline dry run. Simulated data, clearly labeled.
_FIXTURE: dict[str, dict[str, Any]] = {
    "917755736": {
        "navn": "SPAREBANK 1 SMN",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        "hjemmeside": "https://www.smn.no",
        "epostadresse": "post@smn.no",
        "naeringskode1": {"kode": "64.190", "beskrivelse": "Annen kredittgivning"},
    },
    "920100000": {
        "navn": "EKSEMPELBEDRIFT AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "hjemmeside": None,
        "epostadresse": None,
        "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"},
    },
}


class SourceError(Exception):
    """A source-level failure that is *not* a claim about the company."""


class GoneError(SourceError):
    """HTTP 410 — the registry says the record was removed; drop any cache."""


class BlockedError(SourceError):
    """HTTP 429 / timeout / 5xx — the source refused; the claim is BLOCKED."""


class RateLimiter:
    """Thread-safe minimum-interval limiter per host (~2 req/s to data.brreg.no)."""

    def __init__(self, min_interval: float = BRREG_MIN_INTERVAL_S) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_ok = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_ok - now
            if delay > 0:
                self._next_ok = now + delay + self.min_interval
            else:
                self._next_ok = now + self.min_interval
        if delay > 0:
            time.sleep(delay)


class BrregSource:
    def __init__(
        self,
        *,
        live: bool | None = None,
        timeout: float = 15.0,
        fixture_data: dict[str, dict[str, Any]] | None = None,
        min_interval: float = BRREG_MIN_INTERVAL_S,
    ) -> None:
        self.live = bool(int(os.environ.get("SIGNALPOST_LIVE", "0"))) if live is None else live
        self.timeout = timeout
        self.fixture_data = fixture_data if fixture_data is not None else _FIXTURE
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "signalpost/0.1.0 (company-research-agent)"
        self.requests_made = 0
        self._thread_counts = threading.local()
        self.simulated = not self.live
        self.rate_limiter = RateLimiter(min_interval) if live else None

    def _count_request(self) -> None:
        self.requests_made += 1
        self._thread_counts.n = getattr(self._thread_counts, "n", 0) + 1

    def thread_requests(self) -> int:
        """Requests made on the calling thread — for honest per-envelope accounting."""
        return getattr(self._thread_counts, "n", 0)

    def _get(self, url: str) -> requests.Response:
        if self.rate_limiter is not None:
            self.rate_limiter.wait()
        self._count_request()
        # Transient transport failures (proxy hiccups, resets) are retried with
        # backoff — a flaky hop is not a claim about the company. HTTP error
        # statuses are NOT retried; they map to claim states via _classify_response.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))
        raise last_exc  # type: ignore[misc]

    def _classify_response(self, resp: requests.Response, org: str, what: str) -> bool:
        """Classify a non-200 response.

        Returns True when the response is 404 ('looked, nothing there' — the
        caller returns None). Raises GoneError for 410, BlockedError for
        429/5xx, SourceError for anything else unexpected.
        """
        if resp.status_code == 200:
            return False
        if resp.status_code == 404:
            return True
        if resp.status_code == 410:
            raise GoneError(f"{what} for {org}: registry reports record removed (410)")
        if resp.status_code == 429 or 500 <= resp.status_code <= 599:
            raise BlockedError(f"{what} for {org}: source refused (HTTP {resp.status_code})")
        resp.raise_for_status()
        return False

    def fetch_entity(self, org: str) -> dict[str, Any] | None:
        """Return the registry entity dict, or None when the registry has no record.

        None here means 'looked, nothing there' (404) — never zero/empty data.
        HTTP 410 raises GoneError (drop any cached copy). 429/timeout/5xx raise
        BlockedError -> caller maps to BLOCKED. Other errors -> FAILED envelope.
        """
        if not self.live:
            return dict(self.fixture_data.get(org, {})) or None
        url = f"{BASE}/enhetsregisteret/api/enheter/{org}"
        try:
            resp = self._get(url)
        except requests.Timeout as exc:
            raise BlockedError(f"transport timeout for {org}: {exc}") from exc
        except requests.RequestException as exc:
            raise SourceError(f"transport error for {org}: {exc}") from exc
        if self._classify_response(resp, org, "entity fetch"):
            return None
        return resp.json()

    def fetch_accounts(self, org: str) -> list[dict[str, Any]] | None:
        """Annual accounts, newest filing last; None when no filing exists.

        The production endpoint returns a LIST of year-records (not a dict).
        None here means 'looked, nothing there' (404) — never zero/empty data.
        """
        if not self.live:
            return None  # fixture has no accounts -> not_available, honestly labeled
        url = f"{BASE}/regnskapsregisteret/regnskap/{org}"
        try:
            resp = self._get(url)
        except requests.Timeout as exc:
            raise BlockedError(f"transport timeout (accounts) for {org}: {exc}") from exc
        except requests.RequestException as exc:
            raise SourceError(f"transport error (accounts) for {org}: {exc}") from exc
        if self._classify_response(resp, org, "accounts fetch"):
            return None
        data = resp.json()
        return data if isinstance(data, list) else [data]

    def fetch_subunits(self, org: str) -> list[dict[str, Any]] | None:
        """Registered sub-units (underenheter) of this entity.

        Returns a list of {organisasjonsnummer, navn, adresse}; [] when the
        registry lists none; None when the registry has no record (404).
        """
        if not self.live:
            return []
        url = f"{BASE}/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=100"
        try:
            resp = self._get(url)
        except requests.Timeout as exc:
            raise BlockedError(f"transport timeout (sub-units) for {org}: {exc}") from exc
        except requests.RequestException as exc:
            raise SourceError(f"transport error (sub-units) for {org}: {exc}") from exc
        if self._classify_response(resp, org, "sub-units fetch"):
            return None
        subs: list[dict[str, Any]] = []
        for item in resp.json().get("_embedded", {}).get("underenheter", []):
            addr = item.get("beliggenhetsadresse") or {}

            def _addr_str(value: Any) -> str:
                if isinstance(value, list):
                    return ", ".join(str(p) for p in value if p)
                return str(value) if value else ""

            subs.append({
                "organisasjonsnummer": item.get("organisasjonsnummer"),
                "navn": item.get("navn"),
                "adresse": ", ".join(
                    p for p in (
                        _addr_str(addr.get("adresse")),
                        _addr_str(addr.get("postnummer")),
                        _addr_str(addr.get("poststed")),
                    ) if p
                ) or None,
            })
        return subs

    def fetch_konsernstruktur(self, org: str) -> dict[str, Any] | None:
        """Hierarchical group structure for this entity, or None (404) when the
        registry publishes none. Flattens the recursive children into a
        summary: total count + capped sample of subsidiaries."""
        if not self.live:
            return None
        url = f"{BASE}/enhetsregisteret/api/konsernstruktur/{org}"
        try:
            resp = self._get(url)
        except requests.Timeout as exc:
            raise BlockedError(f"transport timeout (konsernstruktur) for {org}: {exc}") from exc
        except requests.RequestException as exc:
            raise SourceError(f"transport error (konsernstruktur) for {org}: {exc}") from exc
        if self._classify_response(resp, org, "konsernstruktur fetch"):
            return None
        flat: list[dict[str, Any]] = []

        def _walk(children: Any) -> None:
            for child in children or []:
                if not isinstance(child, dict):
                    continue
                flat.append({
                    "organisasjonsnummer": child.get("organisasjonsnummer"),
                    "navn": child.get("navn"),
                    "knytningsform": (child.get("knytningsform") or {}).get("beskrivelse"),
                    "nivaa": child.get("nivaa"),
                })
                _walk(child.get("children"))

        _walk(resp.json().get("children"))
        return {"total_subsidiaries": len(flat), "subsidiaries": flat[:50]}

    def fetch_roles(self, org: str) -> list[dict[str, Any]]:
        """Company-centric role holders. Person birth dates are DISCARDED here.

        Production shape: {"rollegrupper": [{"roller": [{type, person: {navn:
        {fornavn, etternavn}, fodselsdato, erDoed}, avregistrert}, ...]}]}.
        Deregistered and deceased role holders are skipped.
        """
        if not self.live:
            return []
        url = f"{BASE}/enhetsregisteret/api/enheter/{org}/roller"
        try:
            resp = self._get(url)
        except requests.Timeout as exc:
            raise BlockedError(f"transport timeout (roles) for {org}: {exc}") from exc
        except requests.RequestException as exc:
            raise SourceError(f"transport error (roles) for {org}: {exc}") from exc
        if self._classify_response(resp, org, "roles fetch"):
            return []
        roles: list[dict[str, Any]] = []
        payload = resp.json()
        groups = payload.get("rollegrupper") or []
        entries = [r for g in groups for r in (g.get("roller") or [])]
        if not entries and isinstance(payload.get("roller"), list):
            entries = payload["roller"]  # tolerate the flat shape if it appears
        for role in entries:
            if not isinstance(role, dict):
                continue
            if role.get("avregistrert"):
                continue  # deregistered role — stale, not published
            person = role.get("person") or {}
            if person.get("erDoed"):
                continue  # deceased — stale, not published
            navn = person.get("navn")
            if isinstance(navn, dict):
                name = " ".join(
                    p for p in (navn.get("fornavn"), navn.get("mellomnavn"), navn.get("etternavn"))
                    if p
                ) or None
            else:
                name = navn
            roles.append({
                "type": (role.get("type") or {}).get("beskrivelse"),
                "name": name,
                # fodselsdato (birth date) is deliberately dropped — never stored.
            })
        return roles

    def download_bulk_csv(self, dest: str) -> str:
        """Download the bulk registry CSV to dest; returns dest."""
        with self.session.get(BULK_CSV_URL, timeout=120, stream=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        return dest


class WebsiteOutcome(str):
    MATCHED = "matched"                # page references the exact org number/name
    FETCHED_NO_REFERENCE = "no_reference"  # page fetched, no entity reference
    BLOCKED_ROBOTS = "blocked_robots"  # robots.txt disallows the crawl
    BLOCKED = "blocked"                # server refused / rate limited / timed out
    FAILED = "failed"                  # transport or parse error


class WebsiteSource:
    """Minimal, honest external-footprint layer: company-owned websites only.

    Rules enforced here (never bent):
    - Only the website listed in the Brønnøysund record for THIS org number.
    - robots.txt is respected: a blanket Disallow for our user-agent => blocked.
    - The page only counts as evidence when its text contains the exact
      organisation number or the exact legal name. Anything else => no_reference,
      which the agent maps to AMBIGUOUS, never AVAILABLE.
    - No LinkedIn / Meta / Indeed / job boards are ever fetched (Day-1 scope).
    """

    def __init__(self, *, timeout: float = 12.0, max_bytes: int = 1 << 20) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "signalpost/0.1.0 (company-research-agent)"
        self.requests_made = 0
        self._thread_counts = threading.local()

    def _count_request(self) -> None:
        self.requests_made += 1
        self._thread_counts.n = getattr(self._thread_counts, "n", 0) + 1

    def thread_requests(self) -> int:
        """Requests made on the calling thread — for honest per-envelope accounting."""
        return getattr(self._thread_counts, "n", 0)

    def _robots_disallows(self, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            self._count_request()
        except requests.RequestException:
            return False  # unreachable robots.txt is not a refusal
        if resp.status_code != 200:
            return False
        in_our_block = False
        saw_disallow_all = False
        for line in resp.text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, val = (p.strip() for p in line.split(":", 1))
            if key.lower() == "user-agent":
                in_our_block = val in ("*", "signalpost")
            elif in_our_block and key.lower() == "disallow":
                if val in ("", "/"):
                    saw_disallow_all = saw_disallow_all or val == "/"
        return saw_disallow_all

    def fetch(self, url: str, org: str, legal_name: str) -> tuple[str, str]:
        """Fetch the company homepage; return (outcome, page-text-or-note).

        The page text is returned ONLY when it references the exact org number
        or the exact legal name; otherwise the outcome is no_reference and the
        text is dropped (nothing publishable).
        """
        url = _normalize_url(url)
        outcome, payload = self._fetch_html(url)
        if outcome != _HTML_OK:
            return outcome, payload
        text = payload
        hay = text.upper()
        org_hit = org in text  # 9-digit org number, verbatim
        name_hit = bool(legal_name) and legal_name.strip().upper() in hay
        if org_hit or name_hit:
            return WebsiteOutcome.MATCHED, text
        return WebsiteOutcome.FETCHED_NO_REFERENCE, "page does not reference the organisation"

    # ------------------------------------------------------------------
    # careers-page discovery (Day-2 recall lever): open positions are only
    # collected from the company-owned website, and only published when the
    # page is entity-anchored (org number or exact legal name in the text).
    # Anything else => ambiguous, never available.
    # ------------------------------------------------------------------
    def find_careers_links(self, html: str, base_url: str) -> list[str]:
        """Same-host links whose href hints at a careers/jobs section."""
        links: list[str] = []
        for match in re.finditer(r'href\s*=\s*["\']([^"\'#]+)["\']', html, re.IGNORECASE):
            href = match.group(1).strip()
            if any(hint in href.lower() for hint in _CAREER_HINTS):
                full = urljoin(base_url, href)
                if urlparse(full).netloc == urlparse(base_url).netloc and full not in links:
                    links.append(full)
        return links[:2]

    @staticmethod
    def extract_job_titles(html: str, cap: int = 25) -> list[str]:
        """Best-effort job titles from a static careers page: <title>, <h1>, <h2>."""
        titles: list[str] = []
        for tag in ("h1", "h2", "title"):
            for match in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", html,
                                     re.IGNORECASE | re.DOTALL):
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()
                if text and len(text) < 200 and text not in titles:
                    titles.append(text)
        return titles[:cap]

    def fetch_careers(self, homepage_url: str, org: str, legal_name: str) -> tuple[str, Any]:
        """Discover open positions on the company-owned careers pages.

        Returns (outcome, payload). On MATCHED the payload is a dict
        {jobs, source_page, page_text} (jobs may be [] => 'looked, none
        listed'). The jobs list is published ONLY when a careers page names
        the exact entity; otherwise the outcome is FETCHED_NO_REFERENCE and
        the agent maps it to AMBIGUOUS.
        """
        homepage_url = _normalize_url(homepage_url)
        outcome, payload = self._fetch_html(homepage_url)
        if outcome != _HTML_OK:
            return outcome, payload
        links = self.find_careers_links(payload, homepage_url)
        if not links:
            return WebsiteOutcome.MATCHED, {
                "jobs": [], "source_page": homepage_url, "page_text": "",
                "note": "no careers section found on the company homepage",
            }
        texts: list[str] = []
        first_page = ""
        for link in links:
            page_outcome, page_payload = self._fetch_html(link)
            if page_outcome != _HTML_OK:
                continue
            if not first_page:
                first_page = link
            texts.append(page_payload)
        if not texts:
            return WebsiteOutcome.FAILED, "careers pages could not be fetched"
        combined = "\n".join(texts)
        hay = combined.upper()
        anchored = (bool(org) and org in combined) or (
            bool(legal_name) and legal_name.strip().upper() in hay
        )
        if not anchored:
            return (WebsiteOutcome.FETCHED_NO_REFERENCE,
                    "careers page does not reference the organisation")
        jobs: list[str] = []
        for text in texts:
            for title in self.extract_job_titles(text):
                if title not in jobs:
                    jobs.append(title)
        return WebsiteOutcome.MATCHED, {
            "jobs": jobs[:25], "source_page": first_page,
            "page_text": texts[0][:20000],
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _fetch_html(self, url: str) -> tuple[str, str]:
        """Robots-respecting HTML fetch. Returns (outcome, html-or-note).

        Transient transport failures are retried once — a flaky hop is not a
        claim about the company. HTTP error statuses are not retried.
        """
        if self._robots_disallows(url):
            return WebsiteOutcome.BLOCKED_ROBOTS, "robots.txt disallows this crawl"
        resp = None
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True)
                self._count_request()
                last_exc = None
                break
            except requests.Timeout as exc:
                last_exc = exc
                break  # a timeout is reported as BLOCKED, not retried
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.0)
        if last_exc is not None:
            if isinstance(last_exc, requests.Timeout):
                return WebsiteOutcome.BLOCKED, "website request timed out"
            return WebsiteOutcome.FAILED, f"website fetch failed: {last_exc}"
        assert resp is not None
        if resp.status_code == 429 or 500 <= resp.status_code <= 599:
            return WebsiteOutcome.BLOCKED, f"website refused (HTTP {resp.status_code})"
        if resp.status_code != 200:
            return WebsiteOutcome.FAILED, f"website returned HTTP {resp.status_code}"
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and "text" not in content_type:
            return WebsiteOutcome.FAILED, f"non-text content ({content_type})"
        try:
            raw = resp.raw.read(self.max_bytes + 1, decode_content=True)
        except Exception as exc:  # noqa: BLE001 - map any read failure to failed
            return WebsiteOutcome.FAILED, f"failed reading website body: {exc}"
        return _HTML_OK, raw.decode("utf-8", errors="replace")
