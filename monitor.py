#!/usr/bin/env python3
"""Watch the big AI/compute companies and turn each new announcement into an
insight: what it signals about their next move, and what it means for the AI
business landscape. Writes data/insights.json (for the dashboard) and emails a
digest.

Run:  python monitor.py            # fetch, analyze, write, email
      python monitor.py --dry-run  # analyze + write, print digest instead of emailing
      python monitor.py --selftest # offline checks, no network/API

Every source is a feed (RSS/Atom): blogs, SEC EDGAR, GitHub releases all reduce
to the same shape, so one fetcher covers every signal type.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
INSIGHTS_FILE = DATA / "insights.json"

# Cap analysis per run so a backlog (or a misbehaving feed) can't run up a bill.
# ponytail: fixed cap; make it an env var if runs ever legitimately exceed it.
MAX_NEW_PER_RUN = 25
MODEL = "claude-opus-4-8"
# EDGAR requires a descriptive User-Agent or it 403s.
USER_AGENT = "watchdog-monitor/1.0"


# ---- feeds --------------------------------------------------------------

def load_sources() -> list[dict]:
    import yaml
    return yaml.safe_load((ROOT / "sources.yaml").read_text())["companies"]


def normalize_ads(rows: list[dict], company: dict, seen: set[str]) -> list[dict]:
    """Map raw scraper rows -> item dicts, deduped by ad id. Tolerant of field
    naming (scrapers differ) so this is verifiable offline without a live call."""
    items = []
    for r in rows:
        ad_id = str(r.get("ad_archive_id") or r.get("adArchiveID") or r.get("id") or "")
        uid = f"ad:{ad_id}"
        if not ad_id or uid in seen:
            continue
        seen.add(uid)
        body = (r.get("ad_creative_body") or r.get("body") or r.get("adText")
                or r.get("creative_text") or r.get("text")
                or r.get("snapshot", {}).get("body", {}).get("text", "") or "")
        title = (body.strip().split("\n", 1)[0] or "(ad, no text)")[:120]
        if company.get("platform") == "google":
            fallback_url = f"https://adstransparency.google.com/advertiser/{company.get('advertiser_id', '')}?region={company.get('region', 'US')}"
        else:
            fallback_url = f"https://www.facebook.com/ads/library/?id={ad_id}"
        items.append({
            "uid": uid,
            "company": company["name"],
            "ticker": company.get("ticker"),
            "type": company.get("type", "industry"),
            "kind": "ad_library",
            "title": title,
            "url": r.get("snapshot_url") or r.get("url") or fallback_url,
            "published": (r.get("ad_delivery_start_time") or r.get("startDate")
                          or r.get("start_date") or ""),
            "raw": str(body)[:4000],
        })
    return items


def fetch_ads(company: dict, seen: set[str]) -> list[dict]:
    """Pull active ads for a page via a third-party scraper (Apify by default).
    Skips cleanly if APIFY_TOKEN is unset. VERIFY the actor input/output field
    names against your chosen actor — scrapers differ; normalize_ads is lenient
    but the actor slug and input shape below are the pieces to confirm."""
    import json as _json
    import urllib.request

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("  APIFY_TOKEN unset — skipping ad-library source.", file=sys.stderr)
        return []
    platform = company.get("platform", "meta")
    if platform == "google":
        actor = os.environ.get("APIFY_ACTOR_GOOGLE", "scraping_solutions~google-ads-transparency-scraper")
        lib_url = f"https://adstransparency.google.com/advertiser/{company['advertiser_id']}?region={company.get('region', 'US')}"
    else:  # meta
        actor = os.environ.get("APIFY_ACTOR", "curious_coder~facebook-ads-library-scraper")
        lib_url = (
            "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country={company.get('country', 'US')}&search_type=page"
            f"&view_all_page_id={company['page_id']}"
        )
    payload = {"urls": [{"url": lib_url}], "count": 50}  # confirm keys for your actor
    req = urllib.request.Request(
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}",
        data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        rows = _json.loads(resp.read())
    return normalize_ads(rows, company, seen)


def fetch_new_items(companies: list[dict], seen: set[str]) -> list[dict]:
    """Return new items (feed entries or ads) as flat dicts. Skips anything
    already seen and any source that fails (one bad source can't kill the run)."""
    import feedparser

    items: list[dict] = []
    for company in companies:
        if company.get("kind") == "ad_library":
            try:
                items += fetch_ads(company, seen)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {company['name']} ad fetch error: {e}", file=sys.stderr)
            continue
        for url in company["feeds"]:
            try:
                parsed = feedparser.parse(url, agent=USER_AGENT)
            except Exception as e:  # noqa: BLE001 - one bad feed must not abort
                print(f"  ! {company['name']} feed error: {url}\n    {e}", file=sys.stderr)
                continue
            for entry in parsed.entries:
                uid = entry.get("id") or entry.get("link")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                items.append({
                    "uid": uid,
                    "company": company["name"],
                    "ticker": company.get("ticker"),
                    "type": company.get("type", "industry"),
                    "title": entry.get("title", "(untitled)"),
                    "url": entry.get("link", ""),
                    "published": entry.get("published", entry.get("updated", "")),
                    "raw": (entry.get("summary", "") or "")[:4000],
                })
    return items


# ---- analysis -----------------------------------------------------------

def build_analyzer():
    """Returns analyze(item) -> dict. Imported lazily so --selftest needs no key."""
    import anthropic
    from pydantic import BaseModel, Field

    class Insight(BaseModel):
        summary: str = Field(description="One or two sentences: what happened.")
        signal: str = Field(description="What this signals about the subject's likely NEXT move or strategy.")
        business_impact: str = Field(description="Why it matters to you — who wins, who's under pressure, what shifts, and what you should do about it.")
        category: str = Field(description="A short label for the item, e.g. Model, Product, Pricing, Partnership, Earnings, Offer, Campaign, Positioning, Content, Other.")
        importance: int = Field(description="1 (routine) to 5 (major).", ge=1, le=5)

    # Framing per source type. Both fill the same schema.
    LENS = {
        "industry": (
            "You are a sharp AI-industry analyst. Extract the insight a business leader "
            "would care about — not a summary, but what it signals about this company's "
            "future moves and how it shifts the AI landscape."
        ),
        "competitor": (
            "You are a competitive-intelligence analyst and this company is a competitor "
            "you are tracking. Read this post and extract what it reveals about their "
            "strategy, offers, or positioning — and the threat or opportunity it creates "
            "for us (put that in business_impact, including what we should do about it)."
        ),
        "ad": (
            "You are a performance-marketing analyst. This is the creative text of an ad a "
            "competitor is actively running. Extract the OFFER and the HOOK/angle (summary), "
            "what their ad strategy signals — audience, positioning, what they're testing "
            "(signal) — and how we should respond: counter-offer, angle to steal or avoid, "
            "gap to exploit (business_impact). Use category for the ad's angle "
            "(e.g. Offer, Lead-magnet, Social-proof, Urgency, Authority)."
        ),
    }

    client = anthropic.Anthropic()

    def analyze(item: dict) -> dict:
        lens_key = "ad" if item.get("kind") == "ad_library" else item["type"]
        lens = LENS.get(lens_key, LENS["industry"])
        prompt = (
            f"{lens}\n\n"
            f"Company: {item['company']}\n"
            f"Title: {item['title']}\n"
            f"Published: {item['published']}\n"
            f"Content:\n{item['raw']}"
        )
        resp = client.messages.parse(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
            output_format=Insight,
        )
        out = resp.parsed_output.model_dump()
        out.update({
            "company": item["company"],
            "ticker": item["ticker"],
            "title": item["title"],
            "url": item["url"],
            "published": item["published"],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        })
        return out

    return analyze


# ---- persistence + digest ----------------------------------------------

def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def render_digest(new: list[dict]) -> str:
    rows = []
    for i in sorted(new, key=lambda x: -x["importance"]):
        stars = "★" * i["importance"] + "☆" * (5 - i["importance"])
        rows.append(
            f'<div style="margin:0 0 26px;padding-bottom:22px;border-bottom:1px solid #e7e2d8">'
            f'<div style="font:600 12px/1 ui-monospace,monospace;letter-spacing:.08em;color:#9a7b4f">'
            f'{i["company"].upper()} · {i["category"].upper()} · {stars}</div>'
            f'<a href="{i["url"]}" style="display:block;margin:8px 0 10px;font:600 19px/1.3 Georgia,serif;color:#1a1a1a;text-decoration:none">{i["title"]}</a>'
            f'<p style="margin:0 0 8px;font:15px/1.55 Georgia,serif;color:#333">{i["summary"]}</p>'
            f'<p style="margin:0 0 6px;font:14px/1.5 Georgia,serif;color:#555"><b>Signal:</b> {i["signal"]}</p>'
            f'<p style="margin:0;font:14px/1.5 Georgia,serif;color:#555"><b>Business impact:</b> {i["business_impact"]}</p>'
            f'</div>'
        )
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    return (
        f'<div style="max-width:640px;margin:0 auto;padding:32px;background:#f4f1ea">'
        f'<h1 style="font:700 28px/1.1 Georgia,serif;color:#1a1a1a;margin:0 0 4px">Watchdog</h1>'
        f'<div style="font:600 12px/1 ui-monospace,monospace;letter-spacing:.1em;color:#9a7b4f;margin:0 0 28px">'
        f'{today} · {len(new)} SIGNALS</div>'
        + "".join(rows) + "</div>"
    )


def send_email(html: str, count: int) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("SMTP_HOST unset — skipping email (use --dry-run to preview).", file=sys.stderr)
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = f"Watchdog — {count} AI-market signals · {datetime.now(timezone.utc):%b %d}"
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", 465))) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print(f"Emailed {count} signals to {msg['To']}.")


# ---- main ---------------------------------------------------------------

def run(dry_run: bool) -> None:
    companies = load_sources()
    seen = set(read_json(SEEN_FILE, []))
    items = fetch_new_items(companies, seen)
    print(f"Found {len(items)} new items.")
    if not items:
        write_json(SEEN_FILE, sorted(seen))
        return

    dropped = max(0, len(items) - MAX_NEW_PER_RUN)
    items = sorted(items, key=lambda x: x["published"], reverse=True)[:MAX_NEW_PER_RUN]
    if dropped:
        # Older overflow is marked seen (baseline) below, NOT re-analyzed later.
        # This is deliberate: the first run establishes a baseline instead of
        # analyzing every historical post. Raise MAX_NEW_PER_RUN to keep more.
        print(f"  (analyzing newest {MAX_NEW_PER_RUN}; {dropped} older items baselined as seen)")

    analyze = build_analyzer()
    fresh = []
    for it in items:
        try:
            fresh.append(analyze(it))
            print(f"  ✓ {it['company']}: {it['title'][:70]}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! analyze failed ({it['title'][:50]}): {e}", file=sys.stderr)
            seen.discard(it["uid"])  # retry next run rather than lose it

    insights = fresh + read_json(INSIGHTS_FILE, [])
    write_json(INSIGHTS_FILE, insights[:500])  # keep newest 500
    write_json(SEEN_FILE, sorted(seen))

    if fresh:
        html = render_digest(fresh)
        if dry_run:
            print("\n--- DIGEST (dry run) ---\n" + html)
        else:
            send_email(html, len(fresh))


def selftest() -> None:
    # Dedup: a uid already in `seen` must not reappear.
    companies = [{"name": "X", "ticker": None, "feeds": []}]
    seen = {"already"}
    assert fetch_new_items(companies, seen) == []
    # Ad normalization: maps varied scraper fields, dedupes by ad id, derives title.
    comp = {"name": "X Ads", "type": "competitor"}
    rows = [
        {"ad_archive_id": "111", "ad_creative_body": "Free webinar\nBook now"},
        {"id": "111", "body": "dupe"},                       # same id -> dropped
        {"adArchiveID": "222", "adText": "50% off cohort"},  # alt field names
    ]
    s: set[str] = set()
    ads = normalize_ads(rows, comp, s)
    assert len(ads) == 2, ads
    assert ads[0]["title"] == "Free webinar" and ads[0]["kind"] == "ad_library"
    assert normalize_ads(rows, comp, s) == []  # all seen now
    # Digest renders the title, company, and impact for a sample insight.
    html = render_digest([{
        "company": "NVIDIA", "category": "Model", "importance": 5,
        "title": "New GPU", "url": "http://x", "summary": "s",
        "signal": "sig", "business_impact": "impact",
    }])
    assert "NVIDIA" in html and "New GPU" in html and "impact" in html
    print("selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        selftest()
    else:
        run(a.dry_run)
