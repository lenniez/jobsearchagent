"""Lever job board API fetcher (no auth required)."""
import logging

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.lever.co/v0/postings/{slug}"
TIMEOUT = 15


def probe(slug: str) -> bool:
    """Return True if this slug resolves to a live Lever board."""
    try:
        resp = requests.get(
            BASE_URL.format(slug=slug), params={"mode": "json"}, timeout=TIMEOUT
        )
        return resp.status_code == 200 and isinstance(resp.json(), list)
    except (requests.RequestException, ValueError):
        return False


def fetch_jobs(company: str, slug: str) -> list[dict]:
    """Fetch and normalize all open postings for a Lever board slug."""
    try:
        resp = requests.get(
            BASE_URL.format(slug=slug), params={"mode": "json"}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        postings = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("lever: failed to fetch jobs for %s (%s): %s", company, slug, exc)
        return []

    jobs = []
    for job in postings:
        categories = job.get("categories") or {}
        jobs.append(
            {
                "source": "lever",
                "company": company,
                "external_id": str(job.get("id", "")),
                "title": job.get("text", ""),
                "location": categories.get("location", ""),
                "url": job.get("hostedUrl", ""),
                "department": categories.get("team"),
                "posted_at": job.get("createdAt"),
                "description": job.get("descriptionPlain") or job.get("description", ""),
            }
        )
    return jobs
