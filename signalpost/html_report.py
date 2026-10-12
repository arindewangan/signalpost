"""Presentable HTML profile report (UX scoring component).

Renders per-company profiles with claims, evidence links and source dates
from the machine-readable run outputs. Single self-contained file, no
external assets.

Usage:
    python -m signalpost.html_report --envelopes out/envelopes.jsonl \
        --profiles out/profiles.jsonl --report out/run-report.json \
        --output out/report.html
"""

from __future__ import annotations

import argparse
import html
import json

STATE_COLORS = {
    "available": "#15803d",
    "not_available": "#64748b",
    "blocked": "#b45309",
    "not_applicable": "#7c3aed",
    "ambiguous": "#b91c1c",
    "failed": "#111827",
}

FIELD_LABELS = {
    "legal_name": "Legal name",
    "organisation_form": "Legal form",
    "registration_status": "Status",
    "industry_code": "Industry",
    "business_address": "Address",
    "website": "Website",
    "email": "Email",
    "employee_count": "Employees",
    "stated_purpose": "Stated purpose",
    "phone": "Phone",
    "former_names": "Former names",
    "financials_latest": "Latest accounts",
    "key_people": "Key people",
    "sub_units": "Sub-units",
    "konsern_structure": "Group structure",
    "website_identity_confirmed": "Website identity",
    "open_jobs": "Open jobs",
    "identity": "Identity",
}


def _load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _fmt_value(value) -> str:
    if value is None:
        return '<span class="na">—</span>'
    if isinstance(value, (dict, list)):
        pretty = json.dumps(value, ensure_ascii=False, indent=1)
        if len(pretty) > 900:
            pretty = pretty[:900] + " …"
        return f"<details><summary>view</summary><pre>{_esc(pretty)}</pre></details>"
    text = str(value)
    if len(text) > 220:
        text = text[:220] + " …"
    return _esc(text)


def _badge(state: str) -> str:
    color = STATE_COLORS.get(state, "#111827")
    label = state.replace("_", " ")
    return (f'<span class="badge" style="background:{color}1a;color:{color};'
            f'border:1px solid {color}55">{_esc(label)}</span>')


def _render_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return '<p class="muted">No evidence records.</p>'
    items = []
    for rec in evidence:
        url = rec.get("source_url", "")
        short = url if len(url) <= 72 else url[:72] + "…"
        digest = str(rec.get("content_sha256", ""))
        items.append(
            "<li class=\"ev\">"
            f"<a href=\"{_esc(url)}\" target=\"_blank\" rel=\"noopener\">"
            f"{_esc(short)}</a>"
            f"<span class=\"src-class\">{_esc(rec.get('source_class', ''))}</span>"
            f"<div class=\"ev-meta\">retrieved <time>{_esc(rec.get('retrieved_at', ''))}</time>"
            f" · sha256 <code>{_esc(digest[:16])}…</code>"
            f" · <em>{_esc(rec.get('claim_span', ''))}</em></div>"
            "</li>"
        )
    return "<ul class=\"evidence\">" + "".join(items) + "</ul>"


def _render_company(envelope: dict, profile: dict) -> str:
    org = _esc(envelope.get("organisation_number", "?"))
    claims = envelope.get("claims", [])
    evidence = {r["id"]: r for r in envelope.get("evidence", [])}
    changes = envelope.get("changes", [])
    errors = envelope.get("errors", [])
    simulated = envelope.get("simulated", False)

    rows = []
    for claim in claims:
        field = claim.get("field", "")
        label = FIELD_LABELS.get(field, field)
        ev_links = []
        for eid in claim.get("evidence_ids", []):
            rec = evidence.get(eid)
            if rec:
                url = rec.get("source_url", "#")
                ev_links.append(
                    f"<a href=\"{_esc(url)}\" target=\"_blank\" rel=\"noopener\" "
                    f"title=\"{_esc(rec.get('retrieved_at', ''))}\">[{_esc(eid)}]</a>"
                )
        rows.append(
            "<tr>"
            f"<td class=\"field\">{_esc(label)}</td>"
            f"<td>{_badge(claim.get('availability', ''))}</td>"
            f"<td class=\"value\">{_fmt_value(claim.get('value'))}</td>"
            f"<td class=\"confl\">{_esc(claim.get('confidence', ''))}</td>"
            f"<td class=\"evrefs\">{' '.join(ev_links) or '<span class=na>—</span>'}</td>"
            "</tr>"
        )

    changes_html = ""
    if changes:
        items = "".join(
            f"<li><b>{_esc(c.get('field', ''))}</b>: "
            f"{_esc(str(c.get('old_availability')))} → "
            f"{_esc(str(c.get('new_availability')))}</li>"
            for c in changes
        )
        changes_html = f"<h4>Material changes</h4><ul class=\"changes\">{items}</ul>"

    errors_html = ""
    if errors:
        items = "".join(f"<li>{_esc(e)}</li>" for e in errors)
        errors_html = f"<h4>Notes</h4><ul class=\"errors\">{items}</ul>"

    sim_tag = ' <span class="sim">simulated</span>' if simulated else ""
    status = envelope.get("run", {}).get("terminal_status", "?")
    avail = sum(1 for c in claims if c.get("availability") == "available")
    summary = _esc(profile.get("summary", "")) if profile else ""
    unknown = ", ".join(_esc(f) for f in (profile.get("unknown_fields") or [])) if profile else ""

    return (
        f"<section class=\"company\" id=\"c-{org}\" data-org=\"{org}\">"
        f"<h3><a href=\"#c-{org}\">#{org}</a>{sim_tag}"
        f" <span class=\"tstatus\">{_esc(status)}</span>"
        f" <span class=\"avail-n\">{avail}/{len(claims)} available</span></h3>"
        f"<p class=\"summary\">{summary}</p>"
        + (f"<p class=\"muted\">Unknown: {unknown}</p>" if unknown else "")
        + f"<table class=\"claims\"><thead><tr><th>Field</th><th>State</th>"
        f"<th>Value</th><th>Conf.</th><th>Evidence</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"<details class=\"evdetails\"><summary>Evidence ({len(evidence)})</summary>"
        f"{_render_evidence(list(evidence.values()))}</details>"
        + changes_html + errors_html +
        "</section>"
    )


def render(envelopes: list[dict], profiles: list[dict], report: dict) -> str:
    profiles_by_org = {p.get("organisation_number"): p for p in profiles}
    companies = "".join(
        _render_company(e, profiles_by_org.get(e.get("organisation_number"), {}))
        for e in envelopes
    )
    self_score = report.get("self_score", {})
    state_counts = report.get("claim_state_counts", {})
    state_chips = " ".join(
        f"<span class=\"chip\">{_badge(s)} × {n}</span>" for s, n in sorted(state_counts.items())
    )
    sim_banner = ""
    if report.get("simulated"):
        sim_banner = ("<div class=\"banner\">SIMULATED RUN — no live sources were "
                      "contacted; evidence is fixture-labeled.</div>")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signalpost — run {html.escape(str(report.get('run_id', '')))} profiles</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
       background: #f8fafc; color: #0f172a; line-height: 1.45; }}
header {{ background: #0f172a; color: #e2e8f0; padding: 1.4rem 1rem; }}
header h1 {{ margin: 0 0 .4rem; font-size: 1.35rem; }}
header .meta {{ font-size: .85rem; color: #94a3b8; }}
.wrap {{ max-width: 1060px; margin: 0 auto; padding: 1rem; }}
.banner {{ background: #fef3c7; border: 1px solid #f59e0b; color: #92400e;
           padding: .6rem .9rem; border-radius: .5rem; margin-bottom: 1rem;
           font-weight: 600; }}
.stats {{ display: flex; flex-wrap: wrap; gap: .5rem; margin: .8rem 0; }}
.chip {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 999px;
         padding: .25rem .6rem; font-size: .8rem; }}
.badge {{ border-radius: 999px; padding: .1rem .55rem; font-size: .75rem;
          font-weight: 600; white-space: nowrap; }}
#filter {{ width: 100%; padding: .6rem .8rem; font-size: 1rem; margin: .6rem 0 1rem;
           border: 1px solid #cbd5e1; border-radius: .5rem; }}
.company {{ background: #fff; border: 1px solid #e2e8f0; border-radius: .75rem;
            padding: 1rem 1.1rem; margin-bottom: 1rem; }}
.company h3 {{ margin: 0 0 .3rem; font-size: 1.05rem; }}
.company h3 a {{ color: inherit; text-decoration: none; }}
.tstatus {{ font-size: .75rem; color: #475569; font-weight: 600; }}
.avail-n {{ font-size: .75rem; color: #475569; }}
.sim {{ background: #fef3c7; color: #92400e; font-size: .7rem; padding: .1rem .4rem;
        border-radius: .3rem; }}
.summary {{ margin: .4rem 0 .8rem; }}
.muted {{ color: #64748b; font-size: .85rem; }}
table.claims {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
table.claims th {{ text-align: left; color: #475569; font-weight: 600;
                   border-bottom: 2px solid #e2e8f0; padding: .4rem .5rem; }}
table.claims td {{ border-bottom: 1px solid #f1f5f9; padding: .4rem .5rem;
                   vertical-align: top; }}
td.field {{ font-weight: 600; white-space: nowrap; }}
td.value {{ max-width: 26rem; overflow-wrap: anywhere; }}
td.confl, td.evrefs {{ white-space: nowrap; }}
.na {{ color: #94a3b8; }}
pre {{ background: #f1f5f9; padding: .5rem; border-radius: .4rem; overflow: auto;
      font-size: .78rem; }}
details.evdetails {{ margin-top: .7rem; }}
ul.evidence {{ list-style: none; padding: 0; margin: .5rem 0 0; }}
li.ev {{ border-top: 1px solid #f1f5f9; padding: .45rem 0; font-size: .82rem;
         overflow-wrap: anywhere; }}
.src-class {{ background: #eef2ff; color: #4338ca; font-size: .72rem;
              border-radius: .3rem; padding: .05rem .4rem; margin-left: .5rem; }}
.ev-meta {{ color: #64748b; font-size: .78rem; }}
ul.changes, ul.errors {{ font-size: .85rem; }}
@media (max-width: 640px) {{
  table.claims, table.claims tbody, table.claims tr, table.claims td {{
    display: block; width: 100%; }}
  table.claims thead {{ display: none; }}
  table.claims td {{ border: 0; padding: .15rem 0; }}
  td.field {{ margin-top: .5rem; }}
}}
</style>
</head>
<body>
<header><div class="wrap">
<h1>Signalpost profiles — run {html.escape(str(report.get('run_id', '')))}</h1>
<div class="meta">{report.get('companies', 0)} companies · self-score
{self_score.get('total', '?')}/100 ·
{report.get('requests_total', 0)} requests ·
{round(report.get('runtime_ms_total', 0) / 1000, 1)}s total ·
third-party cost ${report.get('third_party_cost_usd', 0)}</div>
</div></header>
<div class="wrap">
{sim_banner}
<div class="stats">{state_chips}</div>
<input id="filter" type="search" placeholder="Filter by org number or company name…">
<div id="companies">{companies}</div>
</div>
<script>
const input = document.getElementById('filter');
input.addEventListener('input', () => {{
  const q = input.value.trim().toLowerCase();
  document.querySelectorAll('.company').forEach(el => {{
    el.style.display = !q || el.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}});
</script>
</body>
</html>"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render the HTML profile report.")
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    envelopes = _load_jsonl(args.envelopes)
    profiles = _load_jsonl(args.profiles)
    with open(args.report, encoding="utf-8") as fh:
        report = json.load(fh)
    page = render(envelopes, profiles, report)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {args.output} ({len(envelopes)} companies)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
