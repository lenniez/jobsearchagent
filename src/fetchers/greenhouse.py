"""Greenhouse job board API fetcher (no auth required)."""
import logging
import re

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
TIMEOUT = 15


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def probe(slug: str) -> bool:
    """Return True if this slug resolves to a live Greenhouse board."""
    try:
        resp = requests.get(BASE_URL.format(slug=slug), timeout=TIMEOUT)
        return resp.status_code == 200 and "jobs" in resp.json()
    except (requests.RequestException, ValueError):
        return False


def fetch_jobs(company: str, slug: str) -> list[dict]:
    """Fetch and normalize all open postings for a Greenhouse board slug."""
    try:
        resp = requests.get(
            BASE_URL.format(slug=slug), params={"content": "true"}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("greenhouse: failed to fetch jobs for %s (%s): %s", company, slug, exc)
        return []

    jobs = []
    for job in payload.get("jobs", []):
        location = (job.get("location") or {}).get("name", "")
        departments = job.get("departments") or []
        department = departments[0].get("name") if departments else None
        jobs.append(
            {
                "source": "greenhouse",
                "company": company,
                "external_id": str(job.get("id")),
                "title": job.get("title", ""),
                "location": location,
                "url": job.get("absolute_url", ""),
                "department": department,
                "posted_at": job.get("updated_at"),
                "description": _strip_html(job.get("content", "")),
            }
        )
    return jobs
