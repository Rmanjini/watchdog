# Watchdog

An autonomous monitor for the companies driving the AI build-out — **Anthropic,
OpenAI, NVIDIA, AMD, Google, AWS**. Every day it reads their announcements,
filings, and shipping activity, and for each new item asks an LLM two questions:

- **Signal** — what does this tell us about the company's *next* move?
- **Business impact** — how does it shift the AI landscape: who wins, who's under pressure?

Output is an emailed morning digest **and** a dashboard.

## How it works

```
sources.yaml ──▶ monitor.py ──▶ LLM (signal + impact) ──▶ data/insights.json ──▶ index.html
   (feeds)        (fetch, dedupe,                              (dashboard reads this)
                   analyze, email)
```

Every source is just a feed (RSS / Atom): company blogs, **SEC EDGAR** filings,
and **GitHub release** feeds all reduce to the same shape, so one fetcher covers
blogs, earnings, and pricing/product news. New items are deduped against
`data/seen.json`.

## Run it locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python monitor.py --dry-run   # fetch + analyze, print the digest (no email sent)
python monitor.py --selftest  # offline sanity checks, no network / no API

# view the dashboard (needs http, not file://)
python -m http.server 8000    # then open http://localhost:8000
```

`python monitor.py` (no flag) also emails the digest — see below.

## Run it on autopilot (GitHub Actions + Pages)

`.github/workflows/monitor.yml` runs daily, commits new signals, and publishes
the dashboard to GitHub Pages. Set these repo **Secrets** (Settings → Secrets and
variables → Actions):

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | required — the analysis |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` | email transport (e.g. an app password) |
| `EMAIL_FROM` `EMAIL_TO` | digest sender / recipient |

Then enable **Settings → Pages → Source: GitHub Actions**. Omit the SMTP secrets
to skip email and just publish the dashboard.

## Tuning

- **Companies / feeds:** edit `sources.yaml`. If a feed goes quiet, swap its URL —
  no code change. Add tickers to pull SEC filings.
- **Competitor watch:** add `type: competitor` to a source and it's analyzed for
  threat/opportunity instead of industry signals (same dashboard). Facebook has no
  native RSS — generate a feed URL at [rss.app](https://rss.app) / fetchrss.com and
  paste it into that source's `feeds:`.
- **Cost guard:** `MAX_NEW_PER_RUN` in `monitor.py` caps items analyzed per run.
- **Model:** the `MODEL` constant in `monitor.py`.

## Ad-library monitoring (competitor ads)

Track the ads a page is *actively running* (new ad launches = new signals),
analyzed through a performance-marketing lens (offer, hook, how to counter).

The Meta Ad Library has **no RSS and no free official API** for commercial ads,
so this needs a third-party scraper. An `ad_library` source has no `feeds:` —
it has a `page_id` and `country` (see the `Social Eagle Ads` example in
`sources.yaml`, whose `view_all_page_id` came straight from the Ad Library URL).

Activate it by setting a scraper token; without it the source is skipped:

| Env | Purpose |
|---|---|
| `APIFY_TOKEN` | your [Apify](https://apify.com) API token (required to activate) |
| `APIFY_ACTOR` | optional — the actor slug (default `curious_coder~facebook-ads-library-scraper`) |

> ⚠️ **Verify field names for your actor.** Scrapers return different JSON
> shapes. `normalize_ads()` in `monitor.py` is deliberately lenient (it tries
> several field names), but confirm the actor's input keys and output fields
> against one real run — that's the one thing that can't be tested without your
> key. Swap in ScrapeCreators or another provider by editing `fetch_ads()`.

Paid + ToS gray-area, like any Ad Library scraper. If it errors, that source is
skipped, not fatal.

## Notes

Feeds are best-effort and change over time; a dead feed is skipped, not fatal.
SEC EDGAR requires the descriptive User-Agent already set in `monitor.py`.
