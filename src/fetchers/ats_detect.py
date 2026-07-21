"""Detect which ATS (Greenhouse / Lever / Ashby) a company uses, and cache
the resolved (company -> ats_type, slug) mapping in config/ats_map.yaml so
detection only has to run once per company.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import yaml

from . import ashby, greenhouse, lever

logger = logging.getLogger(__name__)

ATS_PROBES = [
    ("greenhouse", greenhouse.probe),
    ("lever", lever.probe),
    ("ashby", ashby.probe),
]

FETCHERS = {
    "greenhouse": greenhouse.fetch_jobs,
    "lever": lever.fetch_jobs,
    "ashby": ashby.fetch_jobs,
}


def normalize_name(company: str) -> str:
    return company.strip().lower()


def slug_candidates(company: str) -> list[str]:
    """Generate plausible board slugs from a company name, most-likely first."""
    name = company.strip()
    # Drop common corporate suffixes and punctuation that never appear in slugs.
    name = re.sub(r"\b(inc|llc|co|corp|corporation|the)\b", "", name, flags=re.IGNORECASE)
    name = name.replace("&", "and")
    name = re.sub(r"[^a-zA-Z0-9\s\-]", "", name)
    name = re.sub(r"\s+", " ", name).strip()

    words = name.split(" ")
    hyphenated = "-".join(w.lower() for w in words if w)
    concatenated = "".join(w.lower() for w in words if w)

    candidates = [hyphenated, concatenated]
    if len(words) > 1:
        # Some boards use just the first word (e.g. multi-word brand -> single token).
        candidates.append(words[0].lower())
    # De-dupe while preserving order.
    seen = set()
    unique = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def load_ats_map(path: Path) -> dict:
    if not path.exists():
        return {"resolved": {}, "unresolved": []}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("resolved", {})
    data.setdefault("unresolved", [])
    return data


def save_ats_map(path: Path, ats_map: dict) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(ats_map, f, sort_keys=True, default_flow_style=False)


def probe_company(company: str) -> dict | None:
    """Try each slug candidate against each ATS. Returns the first hit."""
    for slug in slug_candidates(company):
        for ats_type, probe_fn in ATS_PROBES:
            try:
                if probe_fn(slug):
                    logger.info("resolved %s -> %s/%s", company, ats_type, slug)
                    return {"ats_type": ats_type, "slug": slug, "source": "auto"}
            except Exception as exc:  # noqa: BLE001 - never let one company break the run
                logger.warning("probe failed for %s (%s/%s): %s", company, ats_type, slug, exc)
    return None


def detect_ats(company: str, ats_map: dict) -> dict | None:
    """Resolve a single company's ATS, using the cache if already resolved."""
    key = normalize_name(company)
    if key in ats_map["resolved"]:
        return ats_map["resolved"][key]

    result = probe_company(company)
    if result:
        result["resolved_at"] = date.today().isoformat()
        result["display_name"] = company
        ats_map["resolved"][key] = result
        if key in ats_map["unresolved"]:
            ats_map["unresolved"].remove(key)
        return result

    logger.info("no ATS found for %s", company)
    if key not in ats_map["unresolved"]:
        ats_map["unresolved"].append(key)
    return None


def detect_all(companies: list[str], ats_map_path: Path) -> dict:
    """Resolve ATS for every company not already cached, saving as it goes."""
    ats_map = load_ats_map(ats_map_path)
    for company in companies:
        detect_ats(company, ats_map)
    save_ats_map(ats_map_path, ats_map)
    return ats_map


def fetch_all_jobs(companies: list[str], ats_map_path: Path) -> list[dict]:
    """Detect ATS for all companies (using cache) and fetch jobs for resolved ones.

    Companies that never resolve to a known ATS are logged and skipped, not
    treated as errors.
    """
    ats_map = load_ats_map(ats_map_path)
    dirty = False
    all_jobs = []

    for company in companies:
        key = normalize_name(company)
        if key not in ats_map["resolved"] and key not in ats_map["unresolved"]:
            detect_ats(company, ats_map)
            dirty = True

        entry = ats_map["resolved"].get(key)
        if not entry:
            logger.info("skipping %s: no resolved ATS", company)
            continue

        fetch_fn = FETCHERS.get(entry["ats_type"])
        if not fetch_fn:
            logger.warning("skipping %s: unknown ats_type %s", company, entry["ats_type"])
            continue

        jobs = fetch_fn(company, entry["slug"])
        logger.info("%s: fetched %d jobs via %s", company, len(jobs), entry["ats_type"])
        all_jobs.extend(jobs)

    if dirty:
        save_ats_map(ats_map_path, ats_map)

    return all_jobs
