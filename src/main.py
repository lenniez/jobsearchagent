"""Daily digest pipeline entrypoint (§3 of project brief).

fetch -> dedupe -> stage1 filter -> stage2 score -> digest email -> (caller
commits data/seen_jobs.db and config/ats_map.yaml back to the repo; see
.github/workflows/daily-digest.yml).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import anthropic
import yaml

from dedupe import (
    connect,
    filter_new_jobs,
    get_unemailed_matches,
    insert_job,
    mark_emailed,
    update_stage1,
    update_stage2,
)
from digest import send_digest
from fetchers import builtin_scraper
from fetchers.ats_detect import fetch_all_jobs
from filter_claude import load_criteria, stage1_screen, stage2_score_all, title_prefilter

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

# Stage 1/2 Claude calls are sequential, so an unbounded backlog of PM-title
# candidates (e.g. a company-wide reorg reposting hundreds of roles at once)
# could still mean many sequential API calls in one workflow run even after
# the local title prefilter. Capping per-run keeps cost and runtime
# predictable; candidates beyond the cap are simply left unseen, so they're
# picked up and processed on a later run instead of this one.
DEFAULT_MAX_NEW_JOBS_PER_RUN = 300

logger = logging.getLogger("main")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def load_companies() -> list[str]:
    with open(CONFIG_DIR / "companies_seed.yaml") as f:
        data = yaml.safe_load(f)
    return [c["name"] for c in data["companies"]]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    anthropic_key = require_env("ANTHROPIC_API_KEY")
    resend_key = require_env("RESEND_API_KEY")
    resend_from = require_env("RESEND_FROM")
    resend_to = require_env("RESEND_TO")
    threshold = int(os.environ.get("SCORE_THRESHOLD") or "7")
    max_roles = int(os.environ.get("DIGEST_MAX_ROLES") or "50")
    max_new_per_run = int(os.environ.get("MAX_NEW_JOBS_PER_RUN") or DEFAULT_MAX_NEW_JOBS_PER_RUN)

    companies = load_companies()
    logger.info("loaded %d companies", len(companies))

    jobs = fetch_all_jobs(companies, CONFIG_DIR / "ats_map.yaml")
    logger.info("fetched %d jobs from Greenhouse/Lever/Ashby", len(jobs))

    builtin_jobs = builtin_scraper.fetch_jobs()
    logger.info("fetched %d jobs from Built In NYC", len(builtin_jobs))
    jobs.extend(builtin_jobs)

    conn = connect(DATA_DIR / "seen_jobs.db")
    new_jobs = filter_new_jobs(conn, jobs)
    logger.info("%d new (deduped) listings to evaluate", len(new_jobs))

    criteria = load_criteria(CONFIG_DIR / "criteria.yaml")

    # Local, zero-cost pass: most fetched listings aren't product roles at
    # all, so drop those before spending any Claude call on them. Insert
    # them as immediately-resolved (stage1_pass=False) so they're deduped
    # for good, not re-fetched and re-checked every day.
    pm_candidates = title_prefilter(criteria, new_jobs)
    pm_candidate_keys = {job["_key"] for job in pm_candidates}
    non_pm_jobs = [job for job in new_jobs if job["_key"] not in pm_candidate_keys]
    logger.info(
        "%d/%d new listings mention product management (no API cost to filter the rest)",
        len(pm_candidates),
        len(new_jobs),
    )
    for job in non_pm_jobs:
        insert_job(conn, job)
        update_stage1(conn, job["_key"], False)

    if len(pm_candidates) > max_new_per_run:
        logger.info(
            "%d PM-title candidates found, processing %d this run; the rest "
            "stay unseen and will be picked up on a future run",
            len(pm_candidates),
            max_new_per_run,
        )
        pm_candidates = pm_candidates[:max_new_per_run]
    for job in pm_candidates:
        insert_job(conn, job)

    if not pm_candidates:
        logger.info("nothing new to score today; exiting")
        return

    client = anthropic.Anthropic(api_key=anthropic_key)

    survivors = stage1_screen(client, criteria, pm_candidates)
    logger.info("%d/%d survived stage 1", len(survivors), len(pm_candidates))
    survivor_keys = {job["_key"] for job in survivors}
    for job in pm_candidates:
        update_stage1(conn, job["_key"], job["_key"] in survivor_keys)

    for job, result in stage2_score_all(client, criteria, survivors):
        if result:
            update_stage2(conn, job["_key"], result)

    matches = get_unemailed_matches(conn, threshold)
    logger.info("%d unemailed matches >= threshold %d", len(matches), threshold)

    success, sent_jobs = send_digest(matches, resend_key, resend_from, resend_to, max_roles)
    if not success:
        logger.error("digest send failed; nothing marked emailed")
    elif sent_jobs:
        mark_emailed(conn, [m["key"] for m in sent_jobs])
        logger.info("digest sent and %d listings marked emailed", len(sent_jobs))
    else:
        logger.info("no matches today; no digest sent")


if __name__ == "__main__":
    main()
