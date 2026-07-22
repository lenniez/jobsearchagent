"""Build and send the daily HTML digest email via Resend (§9).

Caps each digest at DIGEST_MAX_ROLES. Listings cut for being outside the cap
are NOT marked emailed — they remain eligible for a future digest as long as
they keep scoring above threshold and haven't been emailed before. The
caller is responsible for calling dedupe.mark_emailed() only on the listings
actually included, and only after send_digest() reports success.
"""
from __future__ import annotations

import html
import logging
from datetime import date

import resend

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROLES = 50


def select_for_digest(matches: list[dict], max_roles: int = DEFAULT_MAX_ROLES) -> list[dict]:
    """Matches are expected pre-sorted by score descending (dedupe.get_unemailed_matches)."""
    return matches[:max_roles]


def build_html(jobs: list[dict]) -> str:
    rows = []
    for job in jobs:
        title = html.escape(job.get("title") or "")
        company = html.escape(job.get("company") or "")
        location = html.escape(job.get("location") or "")
        url = html.escape(job.get("url") or "", quote=True)
        score = job.get("score")
        reasoning = html.escape(job.get("reasoning") or "")
        rows.append(
            f"""
            <tr>
              <td style="padding:12px 8px;border-bottom:1px solid #e5e5e5;">
                <div style="font-weight:600;font-size:15px;">
                  <a href="{url}" style="color:#111;text-decoration:none;">{title}</a>
                </div>
                <div style="color:#555;font-size:13px;margin-top:2px;">
                  {company} &middot; {location}
                </div>
                <div style="color:#777;font-size:13px;margin-top:6px;">{reasoning}</div>
              </td>
              <td style="padding:12px 8px;border-bottom:1px solid #e5e5e5;text-align:right;
                         vertical-align:top;font-weight:700;font-size:16px;color:#222;">
                {score}/10
              </td>
            </tr>
            """
        )

    return f"""
    <html>
      <body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;">
        <h2 style="margin-bottom:4px;">Job Digest — {date.today().isoformat()}</h2>
        <p style="color:#666;font-size:13px;margin-top:0;">{len(jobs)} new match{'es' if len(jobs) != 1 else ''} today.</p>
        <table style="width:100%;border-collapse:collapse;">
          {''.join(rows)}
        </table>
      </body>
    </html>
    """


def send_digest(
    jobs: list[dict],
    api_key: str,
    from_addr: str,
    to_addr: str,
    max_roles: int = DEFAULT_MAX_ROLES,
) -> tuple[bool, list[dict]]:
    """Send the digest email. Returns (success, jobs_included).

    Sends nothing (and returns success=True with an empty list) if there are
    no matches — we never send an empty digest.
    """
    selected = select_for_digest(jobs, max_roles)
    if not selected:
        logger.info("no matches to digest today; skipping send")
        return True, []

    resend.api_key = api_key
    subject = f"Job Digest: {len(selected)} match{'es' if len(selected) != 1 else ''} — {date.today().isoformat()}"

    try:
        resend.Emails.send(
            {
                "from": from_addr,
                "to": to_addr,
                "subject": subject,
                "html": build_html(selected),
            }
        )
    except Exception as exc:  # noqa: BLE001 - a failed send must not mark anything emailed
        logger.error("digest send failed: %s", exc)
        return False, []

    logger.info("digest sent: %d listings", len(selected))
    return True, selected
