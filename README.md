# jobsearchagent

Agent that reviews job listings of top target companies and creates a regular
email digest.

Automated daily pipeline: fetches new job postings from ~260 tracked companies
(Greenhouse/Lever/Ashby + Built In NYC), scores them against a personal job
rubric using Claude, and emails a digest of the strong matches. Runs entirely
on GitHub Actions' free tier plus Claude API usage — no server, no database
beyond a SQLite file committed to the repo.

## How it works

```
daily-digest.yml (cron, ~9am ET)
  1. Fetch jobs from all tracked companies' ATS boards (Greenhouse/Lever/Ashby)
     + Built In NYC (scraped, guardrailed)
  2. Dedupe against data/seen_jobs.db
  3. Stage 1: Claude Haiku, batched — drop obvious non-matches (wrong level,
     wrong domain)
  4. Stage 2: Claude Sonnet — score survivors against config/criteria.yaml,
     structured JSON output
  5. Email the matches scoring >= threshold (default 7/10) via Resend,
     capped at 50/day, sorted by score
  6. Commit the updated dedupe DB + ATS map back to the repo

discovery.yml (cron, every 3 days)
  1. Pull candidate companies from YC's public directory + Built In's
     company directory
  2. Merge into config/companies_seed.yaml, dedupe by name
  3. Resolve ATS (Greenhouse/Lever/Ashby) for new companies
  4. Cap the tracked list at ~500, drop companies that never resolve to a
     known ATS after 3 discovery cycles
  5. Commit the updated list back to the repo
```

A listing is only ever emailed once, even if it's cut from a full digest for
being outside the top 50 that day — see `emailed` tracking in
[src/dedupe.py](src/dedupe.py).

## Setup

1. Push this repo to GitHub.
2. Add repo secrets (Settings → Secrets and variables → Actions):
   - `ANTHROPIC_API_KEY`
   - `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_TO`
   - `SCRAPER_PROXY_URL` (optional — datacenter proxy for the Built In
     scraper only; leave unset to run proxy-less)
   - `SCRAPER_DELAY_SECONDS` (optional, default 3)
3. The workflows are already scheduled (`daily-digest.yml`,
   `discovery.yml`). Trigger a manual run from the Actions tab
   (`workflow_dispatch`) to test before waiting for the cron.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # only needed for the Built In scraper

cp .env.example .env   # fill in real values, then `export $(cat .env | xargs)`
.venv/bin/python3 src/main.py           # run the daily pipeline once
.venv/bin/python3 src/run_discovery.py  # run company discovery once
```

`config/ats_map.yaml` and `data/seen_jobs.db` are stateful and gitignored
from your local edits colliding with CI — the workflows commit their own
updates. Don't hand-edit `data/seen_jobs.db`.

## Tuning the rubric

Edit [config/criteria.yaml](config/criteria.yaml) — it's loaded directly
into both Claude filtering stages ([src/filter_claude.py](src/filter_claude.py)),
so changes take effect on the next run without touching code.

`SCORE_THRESHOLD` (env var, default 7) and `DIGEST_MAX_ROLES` (default 50)
control what makes it into the email.

## Adding companies manually

Append to [config/companies_seed.yaml](config/companies_seed.yaml):

```yaml
- { name: SomeCompany, category: manual }
```

If auto-detection can't find its ATS, add a manual override directly to
`config/ats_map.yaml` under `resolved:`:

```yaml
resolved:
  somecompany:
    ats_type: greenhouse   # or lever / ashby
    slug: some-company-slug
    source: manual
```

## Budget

Target: $5-15/month (realistic — GitHub Actions, Resend, and SQLite are
free at this volume; the only real costs are Claude API usage and an
optional proxy for the Built In scraper). If Claude spend trends much above
$15/month, stage 1 (Haiku) probably isn't cutting enough volume before
stage 2 (Sonnet) — tighten the stage 1 prompt in
[src/filter_claude.py](src/filter_claude.py) before raising the budget.

## Repo structure

```
config/
  companies_seed.yaml   # tracked company list — grown by discovery.py over time
  ats_map.yaml           # cache of resolved (company -> ATS type, slug)
  criteria.yaml           # the job-search rubric, as structured config
data/
  seen_jobs.db            # dedupe store (SQLite), committed back to the repo each run
src/
  fetchers/
    greenhouse.py, lever.py, ashby.py   # ATS API clients
    ats_detect.py                        # slug guessing + probing + caching
    builtin_scraper.py                   # Playwright scraper w/ guardrails
    robots.py                            # wildcard-aware robots.txt matcher
    discovery.py                         # company-list expansion (YC + Built In)
  dedupe.py         # SQLite dedupe + emailed-tracking
  filter_claude.py  # two-stage Claude scoring
  digest.py          # HTML digest + Resend send
  main.py             # daily pipeline entrypoint
  run_discovery.py    # discovery entrypoint
.github/workflows/
  daily-digest.yml
  discovery.yml
```
