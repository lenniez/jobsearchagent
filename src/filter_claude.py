"""Two-stage Claude filtering pipeline (§7 of project brief).

Stage 1 (Haiku, batched): cheap bulk pass — does title/level look like
Staff/Group PM (or equivalent), does the company/industry look like B2B
enterprise or AI? Drop clear non-matches.

Stage 2 (Sonnet, per listing): score survivors against the full rubric in
config/criteria.yaml, returning structured JSON.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic
import yaml

logger = logging.getLogger(__name__)

STAGE1_MODEL = "claude-haiku-4-5-20251001"
STAGE2_MODEL = "claude-sonnet-5"
STAGE1_BATCH_SIZE = 20

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
            "comp_meets_200k": {
                "anyOf": [{"type": "boolean"}, {"const": "unclear"}]
            },
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
            "comp_meets_200k",
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
Domain must plausibly be B2B enterprise software or AI/ML products — reject obviously
irrelevant domains (e.g. pure consumer-only apps with no B2B angle, unrelated industries).

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
will not be a 9 or 10. Flag ambiguous compensation as "unclear" rather than guessing.
Call score_job with your structured assessment."""


def stage1_screen(client: anthropic.Anthropic, criteria: dict, jobs: list[dict]) -> list[dict]:
    """Batch-screen listings with Haiku. Returns the subset that passes."""
    survivors = []
    for start in range(0, len(jobs), STAGE1_BATCH_SIZE):
        batch = jobs[start : start + STAGE1_BATCH_SIZE]
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
            logger.warning("stage1: API error on batch starting at %d: %s", start, exc)
            # Fail open: let the batch through to stage 2 rather than silently dropping it.
            survivors.extend(batch)
            continue

        if not result:
            logger.warning("stage1: no tool result for batch starting at %d, failing open", start)
            survivors.extend(batch)
            continue

        passed_indices = {r["index"] for r in result["results"] if r.get("pass")}
        for i, job in enumerate(batch):
            if i in passed_indices:
                survivors.append(job)
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
