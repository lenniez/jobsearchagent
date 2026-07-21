"""Built In NYC scraper (§4b of project brief).

Built In NYC has no public API, so this scrapes the public /jobs listing
pages with Playwright. Selectors below were reverse-engineered against the
live site's `[data-id="job-card"]` markup; if the site redeploys with a
different structure, `_parse_card` fails soft (logs a warning, skips that
card) rather than crashing the run.

Guardrails (all required, not optional — see §4b):
  - robots.txt is fetched and checked (via fetchers.robots, since stdlib
    urllib.robotparser doesn't handle the `*`/`$` wildcards this site's
    robots.txt relies on) before every page load.
  - robots.txt here only Allows unfiltered `/jobs?page=1|2|3` — no
    `?search=`, `?f=`, or location-filtered URLs. Job-relevance filtering
    therefore happens client-side, not via query params.
  - Rate-limited to a few requests/minute with jitter (SCRAPER_DELAY_SECONDS).
  - Routed through SCRAPER_PROXY_URL if set; proxy-less works too.
  - Descriptive, non-spoofed User-Agent.
  - Never logs in / never touches anything behind auth.
  - CAPTCHA or sustained block -> log a warning and stop the run, no retries.
"""
from __future__ import annotations

import logging
import os
import random
import time
from urllib.parse import urljoin

import requests

from .robots import RobotsRules, path_for

logger = logging.getLogger(__name__)

BASE_URL = "https://www.builtinnyc.com"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
USER_AGENT = (
    "job-digest-bot/1.0 (+personal job-search digest; "
    "contact: lenniezhu@gmail.com; not affiliated with Built In)"
)

# robots.txt only Allows the first 3 pages of the unfiltered /jobs listing.
LISTING_PAGES = [1, 2, 3]

SENIOR_TITLE_MARKERS = ["staff", "principal", "group product", "director of product"]
PM_MARKERS = ["product manager", "product management"]
HARD_FAIL_TITLE_MARKERS = [
    "associate product manager",
    "apm",
    "product manager i,",
    "product manager ii",
    "product owner",
    "intern",
]

CAPTCHA_MARKERS = [
    "captcha",
    "are you a human",
    "unusual traffic",
    "access denied",
    "request blocked",
    "please verify you are a human",
]


def _sleep_with_jitter() -> None:
    # `or "3"` (not `.get(key, "3")`) because GitHub Actions injects unset
    # optional secrets as an empty-string env var, not an absent one.
    base = float(os.environ.get("SCRAPER_DELAY_SECONDS") or "3")
    time.sleep(base + random.uniform(0, base))


def _load_robots() -> RobotsRules | None:
    try:
        resp = requests.get(ROBOTS_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        return RobotsRules.parse(resp.text, USER_AGENT)
    except requests.RequestException as exc:
        logger.warning("builtin_scraper: failed to fetch robots.txt (%s); skipping this source", exc)
        return None


def _looks_blocked(page_text: str) -> bool:
    lowered = page_text.lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS)


def _matches_target_role(title: str) -> bool:
    lowered = title.lower()
    if any(marker in lowered for marker in HARD_FAIL_TITLE_MARKERS):
        return False
    if not any(marker in lowered for marker in PM_MARKERS):
        return False
    return any(marker in lowered for marker in SENIOR_TITLE_MARKERS) or "senior" in lowered


def _launch_browser(playwright):
    proxy_url = os.environ.get("SCRAPER_PROXY_URL", "").strip()
    kwargs = {"headless": True}
    if proxy_url:
        kwargs["proxy"] = {"server": proxy_url}
    return playwright.chromium.launch(**kwargs)


def _parse_card(card) -> dict | None:
    try:
        title_el = card.query_selector('a[data-id="job-card-title"]')
        if not title_el:
            return None
        title = title_el.inner_text().strip()
        href = title_el.get_attribute("href")

        company_el = card.query_selector('a[data-id="company-title"]')
        company = company_el.inner_text().strip() if company_el else ""

        location_el = card.query_selector('i.fa-location-dot')
        location = ""
        if location_el:
            container = location_el.evaluate_handle("el => el.closest('.d-flex.align-items-start.gap-sm')")
            if container:
                text = container.as_element().inner_text().strip()
                location = text

        description_el = card.query_selector(".fs-sm.fw-regular.mb-md.text-gray-04")
        description = description_el.inner_text().strip() if description_el else ""

        if not href:
            return None

        job_id = href.rstrip("/").rsplit("/", 1)[-1]
        return {
            "source": "builtin",
            "company": company,
            "external_id": job_id or None,
            "title": title,
            "location": location,
            "url": urljoin(BASE_URL, href),
            "department": "Product",
            "posted_at": None,
            "description": description,
        }
    except Exception as exc:  # noqa: BLE001 - one bad card must not kill the run
        logger.warning("builtin_scraper: failed to parse a job card: %s", exc)
        return None


def fetch_jobs(query: str = "product manager") -> list[dict]:
    """Scrape senior PM listings from Built In NYC's public job board.

    Never raises — returns whatever was collected (possibly empty) on any
    robots.txt failure, CAPTCHA/block detection, missing Playwright install,
    or page-structure change. This source is secondary; it must not break
    the daily pipeline.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("builtin_scraper: playwright not installed, skipping")
        return []

    rules = _load_robots()
    if rules is None:
        return []

    jobs: list[dict] = []

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        try:
            for page_num in LISTING_PAGES:
                listing_url = f"{BASE_URL}/jobs?page={page_num}"
                if not rules.can_fetch(path_for(listing_url)):
                    logger.info("builtin_scraper: robots.txt disallows %s, skipping", listing_url)
                    continue

                _sleep_with_jitter()
                try:
                    page.goto(listing_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_selector('[data-id="job-card"]', timeout=10000)
                except Exception as exc:
                    logger.warning("builtin_scraper: failed to load %s: %s", listing_url, exc)
                    continue

                body_text = page.inner_text("body")
                if _looks_blocked(body_text):
                    logger.warning(
                        "builtin_scraper: possible CAPTCHA/block on %s, aborting run (no retry)",
                        listing_url,
                    )
                    return jobs

                cards = page.query_selector_all('[data-id="job-card"]')
                if not cards:
                    logger.info("builtin_scraper: no job cards found on %s (selectors may be stale)", listing_url)
                    continue

                for card in cards:
                    job = _parse_card(card)
                    if job and _matches_target_role(job["title"]):
                        jobs.append(job)
        finally:
            context.close()
            browser.close()

    logger.info("builtin_scraper: collected %d candidate listings", len(jobs))
    return jobs
