"""Footprint-recall hardening (Day-3, lawful sources only).

The exact-entity gate is unchanged (org number verbatim-or-formatted, or the
full legal name; uncertain match => ambiguous, never available). These tests
cover the recall levers that keep the gate honest:
- careers-link discovery from link TEXT as well as href;
- identity-page discovery (kontakt/om-oss pages on the company-owned host)
  as a fallback when the homepage itself carries no entity reference;
- robots.txt verdicts cached per host;
- job-title extraction from anchor texts with generic nav words filtered.
"""

from signalpost.sources import WebsiteOutcome, WebsiteSource, _HTML_OK


class StubWebsite(WebsiteSource):
    """WebsiteSource with canned page fetches keyed by URL."""

    def __init__(self, pages):
        super().__init__()
        self.pages = pages
        self.robots_calls = 0

    def _fetch_html(self, url):
        # robots check happens inside _fetch_html in the real class; emulate it.
        self._robots_disallows(url)
        return self.pages.get(url, (WebsiteOutcome.FAILED, "not stubbed"))

    def _check_robots(self, url):
        self.robots_calls += 1
        return False


def _page(html):
    return (_HTML_OK, html)


# --------------------------------------------------------------------------
# link discovery
# --------------------------------------------------------------------------

def test_find_careers_links_matches_link_text():
    ws = WebsiteSource()
    html = ('<a href="/om-oss">Karriere</a>'
            '<a href="/produkter">Produkter</a>'
            '<a href="https://other.example/karriere">x</a>')
    links = ws.find_careers_links(html, "https://example.com")
    assert links == ["https://example.com/om-oss"]


def test_find_careers_links_still_matches_href():
    ws = WebsiteSource()
    html = ('<a href="/karriere">x</a>'
            '<a href="https://other.example/jobs">x</a>'
            '<a href="/om-oss">Om oss</a>')
    assert ws.find_careers_links(html, "https://example.com") == [
        "https://example.com/karriere"
    ]


def test_find_identity_links_discovers_kontakt_and_about():
    ws = WebsiteSource()
    html = ('<a href="/kontakt-oss">Kontakt oss</a>'
            '<a href="/om-selskapet">About the company</a>'
            '<a href="/produkter">Produkter</a>')
    links = ws.find_identity_links(html, "https://example.com")
    assert links == ["https://example.com/kontakt-oss",
                     "https://example.com/om-selskapet"]


def test_link_discovery_never_leaves_the_host():
    ws = WebsiteSource()
    html = '<a href="https://evil.example/kontakt">Kontakt</a>'
    assert ws.find_identity_links(html, "https://example.com") == []


# --------------------------------------------------------------------------
# fetch with identity-page discovery
# --------------------------------------------------------------------------

def test_fetch_matches_homepage_directly():
    ws = StubWebsite({
        "https://example.com": _page("<html>TESTBEDRIFT AS org 123456789</html>"),
    })
    outcome, text = ws.fetch("https://example.com", "123456789", "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.MATCHED
    assert "123456789" in text


def test_fetch_discovers_org_number_on_kontakt_page():
    ws = StubWebsite({
        "https://example.com": _page(
            '<html><a href="/kontakt">Kontakt oss</a><p>Welcome</p></html>'),
        "https://example.com/kontakt": _page(
            "<html><p>Kontakt: post@example.com. Org.nr 123 456 789</p></html>"),
    })
    outcome, text = ws.fetch("example.com", "123456789", "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.MATCHED
    assert "123 456 789" in text


def test_fetch_no_reference_when_identity_pages_lack_entity():
    ws = StubWebsite({
        "https://example.com": _page(
            '<html><a href="/kontakt">Kontakt oss</a><p>Welcome</p></html>'),
        "https://example.com/kontakt": _page("<html><p>Hello world</p></html>"),
    })
    outcome, _ = ws.fetch("https://example.com", "123456789", "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.FETCHED_NO_REFERENCE


def test_fetch_does_not_publish_wrong_company_page():
    ws = StubWebsite({
        "https://example.com": _page(
            '<html><a href="/om-oss">Om oss</a></html>'),
        "https://example.com/om-oss": _page(
            "<html><p>Annen Bedrift AS, org 999999999</p></html>"),
    })
    outcome, _ = ws.fetch("https://example.com", "123456789", "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.FETCHED_NO_REFERENCE


# --------------------------------------------------------------------------
# robots cache
# --------------------------------------------------------------------------

def test_robots_verdict_cached_per_host():
    ws = StubWebsite({})
    ws._robots_disallows("https://example.com/a")
    ws._robots_disallows("https://example.com/b")
    ws._robots_disallows("https://other.example/a")
    assert ws.robots_calls == 2  # one check per host, not per URL


# --------------------------------------------------------------------------
# careers + job titles
# --------------------------------------------------------------------------

def test_fetch_careers_discovers_link_by_text_and_anchors_entity():
    ws = StubWebsite({
        "https://example.com": _page(
            '<html><a href="/om-oss">Karriere</a></html>'),
        "https://example.com/om-oss": _page(
            "<html><title>Karriere</title><h1>Ledige stillinger</h1>"
            '<a href="/stilling/1">Senior utvikler</a>'
            '<a href="/stilling/2">Designer</a>'
            '<a href="/kontakt">Kontakt</a>'
            "<p>TESTBEDRIFT AS, org.nr 123 456 789</p></html>"),
    })
    outcome, payload = ws.fetch_careers("https://example.com", "123456789",
                                        "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.MATCHED
    assert "Senior utvikler" in payload["jobs"]
    assert "Designer" in payload["jobs"]
    assert "Kontakt" not in payload["jobs"]  # generic nav word filtered


def test_fetch_careers_ambiguous_without_entity_reference():
    ws = StubWebsite({
        "https://example.com": _page('<html><a href="/karriere">Karriere</a></html>'),
        "https://example.com/karriere": _page(
            "<html><h1>Jobs</h1><p>Some other company</p></html>"),
    })
    outcome, _ = ws.fetch_careers("https://example.com", "123456789",
                                  "TESTBEDRIFT AS")
    assert outcome == WebsiteOutcome.FETCHED_NO_REFERENCE


def test_extract_job_titles_harvests_anchor_texts():
    html = ('<html><h1>Ledige stillinger</h1>'
            '<a href="/s/1">Butikkmedarbeider Oslo</a>'
            '<a href="/s/2">Les mer</a>'
            '<a href="/s/3">https://example.com/s/3</a></html>')
    titles = WebsiteSource.extract_job_titles(html)
    assert "Butikkmedarbeider Oslo" in titles
    assert "Les mer" not in titles
    assert not any(t.startswith("https://") for t in titles)
