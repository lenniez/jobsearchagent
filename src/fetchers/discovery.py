"""Company discovery job (§6 of project brief). Run every ~3 days via
.github/workflows/discovery.yml to grow config/companies_seed.yaml beyond
its initial bootstrap list.

Sources:
  - YC's public company directory, which is Algolia-backed client-side
    (verified live: app id 45BWZJ1SGC, index YCCompany_production, using
    the same public restricted search key the ycombinator.com/companies
    page embeds in its HTML — this is a read-only frontend search key, not
    a secret). Queried directly over HTTPS, no scraping needed.
  - Built In NYC's public company directory (/companies). robots.txt only
    permits the single unpaged request (`*?page=` is disallowed with no
    Allow carve-out for /companies, unlike /jobs), so this contributes one
    page's worth of companies per run, not the full directory.

New companies are tagged category="discovered". Only those are eligible
for pruning — hand-curated seed entries (§5) are never auto-dropped.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests
import yaml

from .ats_detect import detect_ats, load_ats_map, normalize_name, save_ats_map
from .robots import RobotsRules, path_for

logger = logging.getLogger(__name__)

YC_ALGOLIA_URL = "https://45BWZJ1SGC-dsn.algolia.net/1/indexes/YCCompany_production/query"
# Public, restricted (read-only, ycdc_public-tagged) frontend search key
# embedded in https://www.ycombinator.com/companies's page source.
YC_ALGOLIA_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJiMWM4ZTU0ZDlhYTZjOTJiMjlhMWFuYWx5"
    "dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlfTGF1"
    "bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE"
)
YC_TIMEOUT = 20

BUILTIN_BASE_URL = "https://www.builtinnyc.com"
BUILTIN_COMPANIES_URL = f"{BUILTIN_BASE_URL}/companies"
USER_AGENT = (
    "job-digest-bot/1.0 (+personal job-search digest; "
    "contact: lenniezhu@gmail.com; not affiliated with Built In)"
)

EXCLUDED_INDUSTRIES = {
    "fintech",
    "payments",
    "consumer finance",
    "banking and exchange",
    "credit and lending",
    "insurance",
    "asset management",
    "security",
}

MAX_TRACKED_COMPANIES = 500
MAX_UNRESOLVED_CYCLES = 3


def _yc_query(query: str, facet_filters: list | None = None, hits_per_page: int = 1000) -> list[dict]:
    payload = {
        "query": query,
        "hitsPerPage": hits_per_page,
        "attributesToRetrieve": ["name", "industries", "status", "team_size", "top_company"],
    }
    if facet_filters:
        payload["facetFilters"] = facet_filters
    try:
        resp = requests.post(
            YC_ALGOLIA_URL,
            headers={
                "X-Algolia-Application-Id": "45BWZJ1SGC",
                "X-Algolia-API-Key": YC_ALGOLIA_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=YC_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("hits", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("discovery: YC query failed (query=%r, facets=%r): %s", query, facet_filters, exc)
        return []


def _passes_industry_filter(industries: list[str]) -> bool:
    lowered = {i.lower() for i in (industries or [])}
    return not (lowered & EXCLUDED_INDUSTRIES)


def _looks_established(hit: dict) -> bool:
    """Rough proxy for 'Series B or later': YC's dataset has no funding-round
    field, so use team size / top_company flag as a stand-in. This is
    intentionally permissive — the real stage filter runs later in
    filter_claude.py's stage 2 scoring against config/criteria.yaml."""
    if hit.get("status") not in ("Active", "Public"):
        return False
    if hit.get("top_company"):
        return True
    return (hit.get("team_size") or 0) >= 50


def fetch_yc_companies() -> list[str]:
    """Pull B2B and AI-tagged companies from YC's public directory."""
    hits = _yc_query("", facet_filters=[["industries:B2B"]])
    hits += _yc_query("artificial intelligence")

    names = []
    seen = set()
    for hit in hits:
        name = (hit.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        if not _passes_industry_filter(hit.get("industries", [])):
            continue
        if not _looks_established(hit):
            continue
        seen.add(name.lower())
        names.append(name)

    logger.info("discovery: YC directory contributed %d candidate companies", len(names))
    return names


def fetch_builtin_companies() -> list[str]:
    """Pull companies from Built In NYC's public directory (single page —
    robots.txt disallows paginating /companies)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("discovery: playwright not installed, skipping Built In companies")
        return []

    try:
        robots_resp = requests.get(f"{BUILTIN_BASE_URL}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=15)
        robots_resp.raise_for_status()
        rules = RobotsRules.parse(robots_resp.text, USER_AGENT)
    except requests.RequestException as exc:
        logger.warning("discovery: failed to fetch Built In robots.txt (%s), skipping", exc)
        return []

    if not rules.can_fetch(path_for(BUILTIN_COMPANIES_URL)):
        logger.info("discovery: robots.txt disallows %s, skipping", BUILTIN_COMPANIES_URL)
        return []

    names = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(BUILTIN_COMPANIES_URL, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_selector(".company-card-horizontal", timeout=10000)

            for card in page.query_selector_all(".company-card-horizontal"):
                name_el = card.query_selector("h2")
                industries_el = card.query_selector(".font-barlow.fw-medium.text-gray-04")
                if not name_el:
                    continue
                name = name_el.inner_text().strip()
                industries_text = industries_el.inner_text() if industries_el else ""
                industries = [t.strip() for t in re.split(r"[•·]", industries_text) if t.strip()]
                if _passes_industry_filter(industries):
                    names.append(name)

            context.close()
            browser.close()
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort, never fatal
        logger.warning("discovery: Built In companies scrape failed: %s", exc)
        return names

    logger.info("discovery: Built In directory contributed %d candidate companies", len(names))
    return names


def load_seed_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save_seed_config(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)


def merge_candidates(seed_data: dict, candidate_names: list[str]) -> int:
    """Add new candidate companies not already present. Returns count added."""
    existing = {normalize_name(c["name"]) for c in seed_data["companies"]}
    added = 0
    for name in candidate_names:
        key = normalize_name(name)
        if key in existing:
            continue
        existing.add(key)
        seed_data["companies"].append(
            {"name": name, "category": "discovered", "unresolved_streak": 0}
        )
        added += 1
    return added


def reconcile_ats_and_prune(seed_data: dict, ats_map: dict) -> tuple[int, int]:
    """Run ATS detection on unresolved companies, track unresolved streaks for
    discovered companies, and drop ones that never resolve. Returns
    (newly_resolved_count, dropped_count)."""
    newly_resolved = 0
    kept = []
    for company in seed_data["companies"]:
        key = normalize_name(company["name"])
        if key not in ats_map["resolved"] and key not in ats_map["unresolved"]:
            before = key in ats_map["resolved"]
            detect_ats(company["name"], ats_map)
            if not before and key in ats_map["resolved"]:
                newly_resolved += 1

        is_discovered = company.get("category") == "discovered"
        is_resolved = key in ats_map["resolved"]

        if is_discovered and not is_resolved:
            company["unresolved_streak"] = company.get("unresolved_streak", 0) + 1
            if company["unresolved_streak"] >= MAX_UNRESOLVED_CYCLES:
                logger.info(
                    "discovery: dropping %s after %d unresolved cycles",
                    company["name"],
                    company["unresolved_streak"],
                )
                continue
        elif is_discovered and is_resolved:
            company["unresolved_streak"] = 0

        kept.append(company)

    dropped = len(seed_data["companies"]) - len(kept)
    seed_data["companies"] = kept
    return newly_resolved, dropped


def enforce_cap(seed_data: dict, ats_map: dict, max_companies: int = MAX_TRACKED_COMPANIES) -> int:
    """Cap the tracked list, prioritizing seed/curated entries and any
    discovered company with a resolved ATS. Returns count dropped."""
    companies = seed_data["companies"]
    if len(companies) <= max_companies:
        return 0

    def priority(company: dict) -> int:
        if company.get("category") != "discovered":
            return 0  # hand-curated: always keep
        if normalize_name(company["name"]) in ats_map["resolved"]:
            return 1  # discovered + resolved: keep next
        return 2  # discovered + unresolved: drop first

    companies.sort(key=priority)
    dropped = len(companies) - max_companies
    seed_data["companies"] = companies[:max_companies]
    return dropped


def run_discovery(companies_seed_path: Path, ats_map_path: Path) -> None:
    seed_data = load_seed_config(companies_seed_path)
    ats_map = load_ats_map(ats_map_path)

    candidates = fetch_yc_companies() + fetch_builtin_companies()
    added = merge_candidates(seed_data, candidates)
    logger.info("discovery: added %d new companies (total now %d)", added, len(seed_data["companies"]))

    newly_resolved, dropped_unresolved = reconcile_ats_and_prune(seed_data, ats_map)
    dropped_cap = enforce_cap(seed_data, ats_map)

    save_seed_config(companies_seed_path, seed_data)
    save_ats_map(ats_map_path, ats_map)

    logger.info(
        "discovery: done. +%d added, %d newly resolved, %d dropped (unresolved), %d dropped (cap), %d total tracked",
        added,
        newly_resolved,
        dropped_unresolved,
        dropped_cap,
        len(seed_data["companies"]),
    )
