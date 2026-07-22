"""One-off utility: force previously-seen, never-emailed listings to be
re-evaluated on the next pipeline run -- e.g. after editing
config/criteria.yaml.

Deletes rows from seen_jobs.db where emailed=0. filter_new_jobs() only
checks whether a listing's key exists in the DB at all, so once its row is
gone the next run treats it as new again and reruns the full prefilter ->
stage1 -> stage2 pipeline under whatever criteria.yaml says now.

Already-emailed listings are never touched: the "no repeats, ever" rule
(§9 of the project brief) is about what's been sent, not what score a
listing would get today, so rescoring must not put them back in play.

Usage: python src/rescore.py
"""
from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path

from dedupe import connect

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = connect(ROOT / "data" / "seen_jobs.db")
    with closing(conn.cursor()) as cur:
        before = cur.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        emailed = cur.execute("SELECT COUNT(*) FROM jobs WHERE emailed = 1").fetchone()[0]
        cur.execute("DELETE FROM jobs WHERE emailed = 0")
        deleted = cur.rowcount
    conn.commit()
    print(f"{before} rows before, {deleted} deleted (never emailed), {emailed} kept (already emailed, untouched)")
    print("run the daily pipeline again to re-evaluate everything under the current criteria.yaml")
