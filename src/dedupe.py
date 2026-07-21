"""SQLite dedupe store (§8/§9 of project brief).

Tracks every listing ever seen, keyed by (source, external_id) or a hash of
its canonical URL when no external_id is available. `emailed` is a separate
flag from "seen" — a listing is marked emailed only after a digest send
succeeds, so a failed send never silently burns a listing's only chance to
appear, and a listing that scored well but got cut from a full digest can
still appear another day (it just was never actually emailed).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    source TEXT,
    company TEXT,
    title TEXT,
    location TEXT,
    url TEXT,
    department TEXT,
    posted_at TEXT,
    first_seen_date TEXT,
    stage1_pass INTEGER,
    score INTEGER,
    level_match INTEGER,
    scope_0_to_1 INTEGER,
    location_match INTEGER,
    comp_meets_200k TEXT,
    management_fit TEXT,
    reasoning TEXT,
    red_flags TEXT,
    emailed INTEGER NOT NULL DEFAULT 0,
    emailed_date TEXT
);
"""


def job_key(job: dict) -> str:
    """Stable unique key for a listing: source:external_id, or a URL hash
    when no external_id is present (e.g. scraped sources)."""
    external_id = job.get("external_id")
    if external_id:
        return f"{job['source']}:{external_id}"
    url = job.get("url", "")
    return f"{job['source']}:{hashlib.sha256(url.encode()).hexdigest()[:16]}"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with closing(conn.cursor()) as cur:
        cur.executescript(SCHEMA)
    conn.commit()
    return conn


def filter_new_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> list[dict]:
    """Return only the listings not already present in the store."""
    with closing(conn.cursor()) as cur:
        existing = {row[0] for row in cur.execute("SELECT key FROM jobs")}
    new_jobs = []
    seen_this_batch = set()
    for job in jobs:
        key = job_key(job)
        if key in existing or key in seen_this_batch:
            continue
        seen_this_batch.add(key)
        job["_key"] = key
        new_jobs.append(job)
    return new_jobs


def insert_job(conn: sqlite3.Connection, job: dict) -> None:
    key = job.get("_key") or job_key(job)
    with closing(conn.cursor()) as cur:
        cur.execute(
            """
            INSERT OR IGNORE INTO jobs
                (key, source, company, title, location, url, department,
                 posted_at, first_seen_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                job.get("source"),
                job.get("company"),
                job.get("title"),
                job.get("location"),
                job.get("url"),
                job.get("department"),
                job.get("posted_at"),
                date.today().isoformat(),
            ),
        )
    conn.commit()


def update_stage1(conn: sqlite3.Connection, key: str, passed: bool) -> None:
    with closing(conn.cursor()) as cur:
        cur.execute("UPDATE jobs SET stage1_pass = ? WHERE key = ?", (int(passed), key))
    conn.commit()


def update_stage2(conn: sqlite3.Connection, key: str, result: dict) -> None:
    with closing(conn.cursor()) as cur:
        cur.execute(
            """
            UPDATE jobs SET
                score = ?, level_match = ?, scope_0_to_1 = ?, location_match = ?,
                comp_meets_200k = ?, management_fit = ?, reasoning = ?, red_flags = ?
            WHERE key = ?
            """,
            (
                result.get("score"),
                int(bool(result.get("level_match"))),
                int(bool(result.get("scope_0_to_1"))),
                int(bool(result.get("location_match"))),
                str(result.get("comp_meets_200k")),
                result.get("management_fit"),
                result.get("reasoning"),
                json.dumps(result.get("red_flags", [])),
                key,
            ),
        )
    conn.commit()


def get_unemailed_matches(conn: sqlite3.Connection, threshold: int) -> list[dict]:
    """Listings that cleared the score threshold and have never been emailed,
    sorted by score descending."""
    with closing(conn.cursor()) as cur:
        rows = cur.execute(
            """
            SELECT * FROM jobs
            WHERE score >= ? AND emailed = 0
            ORDER BY score DESC, first_seen_date ASC
            """,
            (threshold,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_emailed(conn: sqlite3.Connection, keys: list[str]) -> None:
    """Mark listings as emailed. Call only after a digest send succeeds."""
    if not keys:
        return
    today = date.today().isoformat()
    with closing(conn.cursor()) as cur:
        cur.executemany(
            "UPDATE jobs SET emailed = 1, emailed_date = ? WHERE key = ?",
            [(today, key) for key in keys],
        )
    conn.commit()
