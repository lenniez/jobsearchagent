"""Ashby job board API fetcher (no auth required)."""
import logging

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
TIMEOUT = 15


def probe(slug: str) -> bool:
    """Return True if this slug resolves to a live Ashby job board."""
    try:
        resp = requests.get(BASE_URL.format(slug=slug), timeout=TIMEOUT)
        return resp.status_code == 200 and "jobs" in resp.json()
    except (requests.RequestException, ValueError):
        return False


def fetch_jobs(company: str, slug: str) -> list[dict]:
    """Fetch and normalize all open postings for an Ashby board slug."""
    try:
        resp = requests.get(BASE_URL.format(slug=slug), timeout=TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("ashby: failed to fetch jobs for %s (%s): %s", company, slug, exc)
        return []

    jobs = []
    for job in payload.get("jobs", []):
        location = job.get("location") or job.get("locationName") or ""
        jobs.append(
            {
                "source": "ashby",
                "company": company,
                "external_id": str(job.get("id", "")),
                "title": job.get("title", ""),
                "location": location,
                "url": job.get("jobUrl") or job.get("applyUrl", ""),
                "department": job.get("department") or job.get("team"),
                "posted_at": job.get("publishedAt"),
                "description": job.get("descriptionPlain") or "",
            }
        )
    return jobs
