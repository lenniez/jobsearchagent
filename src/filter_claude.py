"""Two-stage Claude filtering pipeline (§7 of project brief).

A zero-cost local title prefilter runs before either stage: most fetched
listings (engineering, sales, support, etc.) aren't product roles at all, so
there's no reason to spend a Haiku call on them. Only listings that at least
mention "product manager"/"product management" in the title reach stage 1.

Stage 1 (Haiku, batched): cheap bulk pass — does title/level look like
Staff/Group PM (or equivalent), does the company/industry look like B2B
enterprise or AI? Drop clear non-matches.

Stage 2 (Sonnet, per listing): score survivors against the full rubric in
config/criteria.yaml, returning structured JSON.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import yaml

logger = logging.getLogger(__name__)

STAGE1_MODEL = "claude-haiku-4-5-20251001"
STAGE2_MODEL = "claude-sonnet-5"
STAGE1_BATCH_SIZE = 20

# Both stages issue independent, stateless API calls, so they parallelize
# safely with a thread pool -- well within rate limits at this volume (Start
# tier alone allows 1,000 req/min for both Haiku and Sonnet).
MAX_WORKERS = 8

PM_TITLE_MARKERS = ["product manager", "product management"]

STAGE1_TOOL = {
    "name": "screen_listings",
    "description": "Report pass/fail for each listing in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "pass": {"type": "boolean"},
                    },
                    "required": ["index", "pass"],
                },
            }
        },
        "required": ["results"],
    },
}

STAGE2_TOOL = {
    "name": "score_job",
    "description": "Score a job listing against the candidate's rubric.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 1, "maximum": 10},
            "level_match": {"type": "boolean"},
            "scope_0_to_1": {"type": "boolean"},
            "location_match": {"type": "boolean"},
            "management_fit": {
                "type": "string",
                "enum": ["ic-only", "hybrid", "management-only", "unclear"],
            },
            "reasoning": {"type": "string"},
            "red_flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "score",
            "level_match",
            "scope_0_to_1",
            "location_match",
            "management_fit",
            "reasoning",
            "red_flags",
        ],
    },
}


def load_criteria(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _extract_tool_input(message, tool_name: str) -> dict | None:
    for block in message.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return None


def _stage1_prompt(criteria: dict, batch: list[dict]) -> str:
    hard_fail_titles = ", ".join(criteria["role"]["hard_fail_titles"])
    target_titles = ", ".join(criteria["role"]["target_titles"])
    listings_text = "\n".join(
        f"{i}. title=\"{job.get('title', '')}\" | company=\"{job.get('company', '')}\" "
        f"| department=\"{job.get('department') or ''}\" | location=\"{job.get('location', '')}\""
        for i, job in enumerate(batch)
    )
    return f"""You are doing a fast, cheap first-pass screen of job listings for a candidate.

The candidate wants: {target_titles} (or a clear seniority equivalent like "Principal PM").
Hard-fail titles (never pass these): {hard_fail_titles}.
Domain must plausibly involve a B2B enterprise offering and/or AI functionality — this
applies to the specific product/role, not necessarily the whole company. A consumer-first
company's B2B-specific team (e.g. a PM role on Notion's team/enterprise offering) still
counts. Only reject domains that are clearly irrelevant (e.g. a pure consumer product with
no B2B angle and no AI functionality, or an unrelated industry).

This is a coarse filter — when genuinely uncertain, let it pass through to the more careful
second-stage review rather than guessing. Only fail listings that clearly violate the title or
domain check.

Listings:
{listings_text}

Call screen_listings with a pass/fail for every index above."""


def _stage2_prompt(criteria: dict, job: dict) -> str:
    criteria_yaml = yaml.safe_dump(criteria, sort_keys=False)
    description = (job.get("description") or "")[:6000]
    return f"""You are scoring one job listing against a candidate's job-search rubric.

RUBRIC:
{criteria_yaml}

LISTING:
title: {job.get('title', '')}
company: {job.get('company', '')}
department: {job.get('department') or ''}
location: {job.get('location', '')}
url: {job.get('url', '')}
description:
{description}

Score this listing 1-10 against the full rubric. Be honest and specific — most listings
will not be a 9 or 10. Call score_job with your structured assessment."""


def title_prefilter(criteria: dict, jobs: list[dict]) -> list[dict]:
    """Local, zero-cost pass: drop anything whose title doesn't even mention
    product management, or that matches a hard-fail title (e.g. "Associate
    Product Manager"). Most fetched listings are non-product roles entirely,
    so this cuts stage 1 volume (and cost) dramatically before any API call.
    Stage 1 still does the nuanced level/domain judgment on what survives."""
    hard_fail_titles = [t.lower() for t in criteria["role"]["hard_fail_titles"]]
    survivors = []
    for job in jobs:
        title = (job.get("title") or "").lower()
        if not any(marker in title for marker in PM_TITLE_MARKERS):
            continue
        if any(hard_fail in title for hard_fail in hard_fail_titles):
            continue
        survivors.append(job)
    return survivors


def _stage1_screen_batch(client: anthropic.Anthropic, criteria: dict, batch: list[dict]) -> list[dict]:
    try:
        message = client.messages.create(
            model=STAGE1_MODEL,
            max_tokens=2048,
            tools=[STAGE1_TOOL],
            tool_choice={"type": "tool", "name": "screen_listings"},
            messages=[{"role": "user", "content": _stage1_prompt(criteria, batch)}],
        )
        result = _extract_tool_input(message, "screen_listings")
    except anthropic.APIError as exc:
        logger.warning("stage1: API error on a batch: %s", exc)
        # Fail open: let the batch through to stage 2 rather than silently dropping it.
        return batch

    if not result:
        logger.warning("stage1: no tool result for a batch, failing open")
        return batch

    passed_indices = {r["index"] for r in result["results"] if r.get("pass")}
    return [job for i, job in enumerate(batch) if i in passed_indices]


def stage1_screen(client: anthropic.Anthropic, criteria: dict, jobs: list[dict]) -> list[dict]:
    """Batch-screen listings with Haiku, batches run concurrently. Returns
    the subset that passes."""
    batches = [jobs[start : start + STAGE1_BATCH_SIZE] for start in range(0, len(jobs), STAGE1_BATCH_SIZE)]
    survivors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_stage1_screen_batch, client, criteria, batch) for batch in batches]
        for future in as_completed(futures):
            survivors.extend(future.result())
    return survivors


def stage2_score(client: anthropic.Anthropic, criteria: dict, job: dict) -> dict | None:
    """Score one survivor with Sonnet against the full rubric."""
    try:
        message = client.messages.create(
            model=STAGE2_MODEL,
            max_tokens=1024,
            tools=[STAGE2_TOOL],
            tool_choice={"type": "tool", "name": "score_job"},
            messages=[{"role": "user", "content": _stage2_prompt(criteria, job)}],
        )
    except anthropic.APIError as exc:
        logger.warning("stage2: API error scoring %s @ %s: %s", job.get("title"), job.get("company"), exc)
        return None

    result = _extract_tool_input(message, "score_job")
    if not result:
        logger.warning("stage2: no tool result scoring %s @ %s", job.get("title"), job.get("company"))
        return None
    return result


def stage2_score_all(client: anthropic.Anthropic, criteria: dict, jobs: list[dict]) -> list[tuple[dict, dict | None]]:
    """Score all survivors concurrently. Each call is independent and
    stateless, so this is safe to parallelize. Returns (job, result) pairs;
    result is None for any job that failed to score. Caller is responsible
    for any shared state (e.g. DB writes) -- this function only makes API
    calls, it doesn't touch the database."""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_job = {executor.submit(stage2_score, client, criteria, job): job for job in jobs}
        return [(future_to_job[future], future.result()) for future in as_completed(future_to_job)]
