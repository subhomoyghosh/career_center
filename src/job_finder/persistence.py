"""
Save and read jobs in the database.
Each job: id, company, title, link, score, theme, rationale, status, user_feedback, user_weight, first_seen, posted_at.
The id is a hash of the link so we don't add the same job twice.

first_seen: ISO 8601 UTC timestamp of when WE first persisted this row. Always populated.
posted_at: ISO 8601 UTC timestamp of when the employer posted the job (best-effort,
           extracted by the agent from sources like Lever's createdAt). May be NULL.
"""
import hashlib
import json
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from job_finder.paths import get_db_path

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    company TEXT,
    title TEXT,
    link TEXT,
    score INTEGER,
    theme TEXT,
    rationale TEXT,
    status TEXT DEFAULT 'New',
    user_feedback TEXT,
    user_weight INTEGER DEFAULT 50,
    first_seen TEXT,
    posted_at TEXT,
    description TEXT,
    failed_validation_count INTEGER DEFAULT 0,
    last_validated_at TEXT
)
"""

# Job lifecycle status vocabulary. The `status` column is the single source of truth
# for application lifecycle — disentangled from `user_feedback` (genre preference) and
# `user_weight` (specific-row interest). A user who applied to a great job and got
# rejected should NOT have that row dragged into the synthesizer's bad-set; the
# genre signal stays positive even though the specific row is closed.
JOB_STATUS_NEW = "New"                  # default: system surfaced, no user action
JOB_STATUS_APPLIED = "Applied"          # user applied; awaiting outcome
JOB_STATUS_IN_PROGRESS = "InProgress"   # interviewing or otherwise active
JOB_STATUS_CLOSED = "Closed"            # applied + outcome (rejected/ghosted/withdrawn); GENRE-POSITIVE
JOB_STATUS_WON = "Won"                  # got offer; strongest positive
JOB_STATUS_NOT_FOR_ME = "NotForMe"      # user explicitly rejected (without applying); GENRE-NEGATIVE
JOB_STATUS_QUARANTINE = "quarantine"    # pruner-set; not a user status

# Status sets used by the synthesizer + efficacy script for set membership.
# Reason for keeping POSITIVE_GENRE distinct from NEGATIVE_GENRE: a row may be
# in POSITIVE_GENRE (user applied) while also having low weight (don't re-surface
# this specific row). The user's applied-intent dominates the genre signal.
POSITIVE_GENRE_STATUSES = frozenset({
    JOB_STATUS_APPLIED, JOB_STATUS_IN_PROGRESS, JOB_STATUS_CLOSED, JOB_STATUS_WON,
})
NEGATIVE_GENRE_STATUSES = frozenset({JOB_STATUS_NOT_FOR_ME})
USER_TERMINAL_STATUSES = POSITIVE_GENRE_STATUSES | NEGATIVE_GENRE_STATUSES  # user is "done" with this row

VALID_USER_STATUSES = frozenset({
    JOB_STATUS_NEW, JOB_STATUS_APPLIED, JOB_STATUS_IN_PROGRESS,
    JOB_STATUS_CLOSED, JOB_STATUS_WON, JOB_STATUS_NOT_FOR_ME,
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_posted_at(value: Any) -> Optional[str]:
    """
    Best-effort normalize a posted-at value into an ISO 8601 UTC string.

    Accepts:
    - None / "" → None
    - Epoch ms (Lever's createdAt) or seconds, as int/float or all-digit string
    - ISO-like string → passed through trimmed (we don't validate, just store)
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            if s.lstrip("-").isdigit():
                ts = float(s)
            else:
                return s
        else:
            return None
        # Heuristic: > 1e12 implies milliseconds (year 2001+).
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None

_PLACEHOLDER_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "localhost",
}


def _is_placeholder_link(link: str) -> bool:
    if not isinstance(link, str):
        return False
    try:
        host = (urlparse(link).hostname or "").lower()
    except ValueError:
        return False
    return host in _PLACEHOLDER_HOSTS


def _job_id(link: str) -> str:
    return hashlib.md5(link.encode()).hexdigest()


def _normalize_link(link: Any) -> Optional[str]:
    """
    Normalize agent-provided links into a fully qualified http(s) URL.

    We intentionally keep this conservative:
    - If already http(s), pass through.
    - If missing scheme but looks like a host/path, prefix https://.
    """
    if not isinstance(link, str):
        return None
    raw = link.strip()
    if not raw:
        return None
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        return raw
    # Handle protocol-relative URLs.
    if low.startswith("//"):
        return "https:" + raw
    # Heuristic: if it looks like a host/path, prefix https://.
    # Examples: "jobs.lever.co/..." or "boards.greenhouse.io/..."
    if "." in low and "/" in raw and " " not in raw and "\n" not in raw and "\t" not in raw:
        return "https://" + raw
    return None


def _coalesce_first(j: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        if k in j and j.get(k) not in (None, ""):
            return j.get(k)
    return None


def _adapt_job_fields(j: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Adapt common agent schema variations into the strict persistence schema.

    Required output keys for persistence:
    - company, title, link, score, theme, rationale
    """
    company = _coalesce_first(j, ("company", "org", "employer"))
    title = _coalesce_first(j, ("title", "job_title", "role"))
    link = _coalesce_first(j, ("link", "url", "listing_url"))
    score = _coalesce_first(j, ("score", "match_score", "moat_score"))
    theme = _coalesce_first(j, ("theme", "domain"))
    rationale = _coalesce_first(
        j,
        (
            "rationale",
            "rationale_preview",  # used in judge contexts
            "rationaleText",
            "reasoning",
        ),
    )

    posted_at_raw = _coalesce_first(
        j,
        ("posted_at", "createdAt", "created_at", "posted_date", "date_posted", "postedAt"),
    )

    description = _coalesce_first(j, ("description", "body", "description_text", "descriptionPlain"))

    payload = {
        "company": company,
        "title": title,
        "link": _normalize_link(link),
        "score": score,
        "theme": theme,
        "rationale": rationale,
        "posted_at": _normalize_posted_at(posted_at_raw),
        "description": description,
    }

    # Ensure all required fields are present and non-empty.
    # posted_at and description are optional and intentionally excluded from this check.
    if any(payload.get(k) in (None, "") for k in ("company", "title", "link", "score", "theme", "rationale")):
        return None

    return payload


def ensure_feedback_columns(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(jobs)")
    cols = [row[1] for row in cur.fetchall()]
    if "user_feedback" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN user_feedback TEXT")
    if "user_weight" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN user_weight INTEGER DEFAULT 50")
    conn.commit()


def ensure_recency_columns(conn: sqlite3.Connection) -> None:
    """Add first_seen and posted_at columns to existing databases (idempotent)."""
    cur = conn.execute("PRAGMA table_info(jobs)")
    cols = [row[1] for row in cur.fetchall()]
    if "first_seen" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN first_seen TEXT")
    if "posted_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN posted_at TEXT")
    conn.commit()


def ensure_pruning_columns(conn: sqlite3.Connection) -> None:
    """Add two-strike validation tracking columns (idempotent)."""
    cur = conn.execute("PRAGMA table_info(jobs)")
    cols = [row[1] for row in cur.fetchall()]
    if "failed_validation_count" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN failed_validation_count INTEGER DEFAULT 0")
    if "last_validated_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN last_validated_at TEXT")
    conn.commit()


def ensure_description_column(conn: sqlite3.Connection) -> None:
    """Add the description column for storing full job body text (idempotent)."""
    cur = conn.execute("PRAGMA table_info(jobs)")
    cols = [row[1] for row in cur.fetchall()]
    if "description" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN description TEXT")
    conn.commit()


def _legacy_posting_columns() -> Tuple[str, ...]:
    return ("country", "state", "city", "date_posted", "work_mode")


def migrate_jobs_table_drop_legacy_posting_columns(conn: sqlite3.Connection) -> None:
    """
    Remove country, state, city, date_posted, work_mode (unreliable / removed from product).
    Uses DROP COLUMN when supported (SQLite 3.35+); otherwise rebuilds the table.
    """
    cur = conn.execute("PRAGMA table_info(jobs)")
    col_names = [r[1] for r in cur.fetchall()]
    legacy = [c for c in _legacy_posting_columns() if c in col_names]
    if not legacy:
        return
    try:
        for c in legacy:
            conn.execute(f"ALTER TABLE jobs DROP COLUMN {c}")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
        _rebuild_jobs_table_without_legacy(conn)


def _rebuild_jobs_table_without_legacy(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy_mig")
    conn.executescript(JOBS_SCHEMA)
    legacy_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(jobs_legacy_mig)").fetchall()
    }
    sentinel = _now_iso()

    # For each "new" column that may or may not exist on the legacy table,
    # pick a SELECT expression that yields a sensible non-NULL value when the
    # column does exist, and a fallback literal otherwise. Bound params are
    # passed positionally so the order here matters.
    def _col_or_default(name: str, default_sql: str) -> str:
        return f"COALESCE({name}, {default_sql})" if name in legacy_cols else default_sql

    user_weight_expr = _col_or_default("user_weight", "50")
    first_seen_expr = _col_or_default("first_seen", "?")
    posted_at_expr = "posted_at" if "posted_at" in legacy_cols else "NULL"
    description_expr = "description" if "description" in legacy_cols else "NULL"
    failed_count_expr = _col_or_default("failed_validation_count", "0")
    last_validated_expr = "last_validated_at" if "last_validated_at" in legacy_cols else "NULL"

    sql = f"""
        INSERT INTO jobs (
            id, company, title, link, score, theme, rationale, status, user_feedback, user_weight,
            first_seen, posted_at, description, failed_validation_count, last_validated_at
        )
        SELECT id, company, title, link, score, theme, rationale,
               COALESCE(status, 'New'), user_feedback, {user_weight_expr},
               {first_seen_expr}, {posted_at_expr}, {description_expr},
               {failed_count_expr}, {last_validated_expr}
        FROM jobs_legacy_mig
    """
    # first_seen_expr contains "?" iff we need to bind the sentinel timestamp.
    params: Tuple[Any, ...] = (sentinel,) if "?" in first_seen_expr else ()
    conn.execute(sql, params)
    conn.execute("DROP TABLE jobs_legacy_mig")
    conn.commit()


def create_jobs_table(db_path: Optional[str] = None) -> None:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.execute(JOBS_SCHEMA)
    ensure_feedback_columns(conn)
    migrate_jobs_table_drop_legacy_posting_columns(conn)
    ensure_recency_columns(conn)
    ensure_description_column(conn)
    ensure_pruning_columns(conn)
    conn.close()


def persist_jobs(jobs: List[dict], db_path: Optional[str] = None) -> None:
    """
    Insert or replace jobs. Required keys: company, title, link, score, theme, rationale.
    Preserves user_feedback and user_weight on upsert.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, timeout=30.0)
    ensure_feedback_columns(conn)
    migrate_jobs_table_drop_legacy_posting_columns(conn)
    ensure_recency_columns(conn)
    ensure_description_column(conn)
    ensure_pruning_columns(conn)
    # Take manual control of transactions so we can wrap each per-job
    # read-modify-write in an explicit BEGIN IMMEDIATE. This blocks concurrent
    # writers (e.g. the Streamlit UI editing user_feedback) at the SELECT step
    # instead of letting them race through and clobber each other on REPLACE.
    conn.isolation_level = None
    # Fetch-time exclusion backstop. The /fetchjobs agent SHOULD have applied
    # this upstream, but persistence enforces it unconditionally so silent
    # agent regressions never reach the DB / scoring snapshots.
    _dropped: List[Any] = []
    _exclusion_backstop_failed = 0
    try:
        from job_finder.config import load_config
        from job_finder.exclusions import apply_exclusions
        _cfg = load_config()
        if jobs:
            jobs, _dropped = apply_exclusions(jobs, _cfg)
            if _dropped:
                print(
                    f"persist_jobs debug exclusions: dropped={len(_dropped)} "
                    f"sample={_dropped[:3]}"
                )
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as _e:
        # Narrow catch: only recoverable config-load issues are swallowed.
        # Any other exception propagates so the user sees the real failure.
        _exclusion_backstop_failed = 1
        print(f"persist_jobs debug exclusions: skipped ({_e!r})")
    counters = {
        "received": len(jobs),
        "persisted": 0,
        "skipped_non_dict": 0,
        "skipped_missing_or_adaptation_failed": 0,
        "skipped_type_normalization": 0,
        "skipped_placeholder_link": 0,
        "dropped_by_exclusion": len(_dropped),
        "exclusion_backstop_failed": _exclusion_backstop_failed,
    }
    # Debug: make it obvious whether /fetchjobs is providing any jobs to persistence.
    # Cursor agent calls this in-process; stdout should appear in the chat run.
    if jobs is None:
        print("persist_jobs debug: received=None")
    else:
        sample = []
        for j in jobs[:3]:
            if isinstance(j, dict):
                sample.append(
                    {
                        "company": j.get("company") or j.get("org") or j.get("employer"),
                        "title": j.get("title") or j.get("job_title") or j.get("role"),
                        "link": j.get("link") or j.get("url") or j.get("listing_url"),
                    }
                )
        print(
            "persist_jobs debug: "
            + f"received={len(jobs)} sample={sample}"
        )

    for j in jobs:
        if not isinstance(j, dict):
            counters["skipped_non_dict"] += 1
            continue

        adapted = _adapt_job_fields(j)
        if adapted is None:
            counters["skipped_missing_or_adaptation_failed"] += 1
            if counters["skipped_missing_or_adaptation_failed"] <= 5:
                # Print why a job dict couldn't be adapted into the strict schema.
                company = _coalesce_first(j, ("company", "org", "employer", "company_name"))
                title = _coalesce_first(j, ("title", "job_title", "role", "jobTitle"))
                link_raw = _coalesce_first(j, ("link", "url", "listing_url", "listingUrl"))
                link = _normalize_link(link_raw)
                score = _coalesce_first(j, ("score", "match_score", "moat_score"))
                theme = _coalesce_first(j, ("theme", "domain", "themeName"))
                rationale = _coalesce_first(
                    j,
                    (
                        "rationale",
                        "rationale_preview",
                        "rationaleText",
                        "reasoning",
                    ),
                )
                required = {
                    "company": company,
                    "title": title,
                    "link": link,
                    "score": score,
                    "theme": theme,
                    "rationale": rationale,
                }
                missing = [k for k, v in required.items() if v in (None, "")]
                print(
                    "persist_jobs debug skipped_adaptation: "
                    + f"missing={missing} keys={list(j.keys())[:20]}"
                )
            continue

        # Basic type normalization so downstream comparisons (UI) behave consistently.
        try:
            new_description_raw = adapted.get("description")
            payload: Dict[str, Any] = {
                "company": str(adapted["company"]),
                "title": str(adapted["title"]),
                "link": str(adapted["link"]),
                "score": int(adapted["score"]),
                "theme": str(adapted["theme"]),
                "rationale": str(adapted["rationale"]),
                "description": str(new_description_raw) if new_description_raw not in (None, "") else None,
            }
        except (TypeError, ValueError) as e:
            counters["skipped_type_normalization"] += 1
            if counters["skipped_type_normalization"] <= 5:
                print(
                    "persist_jobs debug skipped_type_normalization: "
                    + f"score_raw={adapted.get('score')} error={type(e).__name__}"
                )
            continue

        # Hard safety gate: never persist placeholder/test domains.
        if _is_placeholder_link(payload["link"]):
            counters["skipped_placeholder_link"] += 1
            continue
        company_l = payload["company"].strip().lower()
        if "test" in company_l and _is_placeholder_link(payload["link"]):
            counters["skipped_placeholder_link"] += 1
            continue

        job_id = _job_id(payload["link"])
        new_posted_at = adapted.get("posted_at")
        new_description = payload.get("description")
        # Hold a RESERVED lock across SELECT + INSERT OR REPLACE so the
        # Streamlit UI can't edit user_feedback between our read and write.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT user_feedback, user_weight, status, first_seen, posted_at, "
                "description, failed_validation_count, last_validated_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None:
                (
                    feedback,
                    weight,
                    status,
                    existing_first_seen,
                    existing_posted_at,
                    existing_description,
                    existing_failed_count,
                    existing_last_validated_at,
                ) = row
                payload = {
                    **payload,
                    "user_feedback": feedback,
                    "user_weight": weight if weight is not None else 50,
                    "status": status or "New",
                    "first_seen": existing_first_seen or _now_iso(),
                    # Prefer the freshly extracted value; fall back to existing.
                    # This lets a later Lever-API hit upgrade a row first persisted via aggregator.
                    "posted_at": new_posted_at if new_posted_at else existing_posted_at,
                    "description": new_description if new_description else existing_description,
                    "failed_validation_count": existing_failed_count if existing_failed_count is not None else 0,
                    "last_validated_at": existing_last_validated_at,
                }
            else:
                payload = {
                    **payload,
                    "user_feedback": j.get("user_feedback"),
                    "user_weight": j.get("user_weight", 50),
                    "status": "New",
                    "first_seen": _now_iso(),
                    "posted_at": new_posted_at,
                    "description": new_description,
                    "failed_validation_count": 0,
                    "last_validated_at": None,
                }
            conn.execute(
                """INSERT OR REPLACE INTO jobs (
                    id, company, title, link, score, theme, rationale, status, user_feedback, user_weight,
                    first_seen, posted_at, description, failed_validation_count, last_validated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    payload["company"],
                    payload["title"],
                    payload["link"],
                    payload["score"],
                    payload["theme"],
                    payload["rationale"],
                    payload.get("status", "New"),
                    payload.get("user_feedback"),
                    payload.get("user_weight", 50),
                    payload["first_seen"],
                    payload.get("posted_at"),
                    payload.get("description"),
                    payload.get("failed_validation_count", 0),
                    payload.get("last_validated_at"),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        counters["persisted"] += 1
    conn.close()
    try:
        from job_finder.history import record_jobs_snapshot_from_db

        record_jobs_snapshot_from_db(path)
    except (OSError, sqlite3.Error, ImportError) as snap_err:
        # Snapshot is best-effort: surface in counters so /fetchjobs diagnostics catch it.
        counters["snapshot_failed"] = str(snap_err)

    # Helpful debug for /fetchjobs failures: persistence is otherwise silent.
    print(
        "persist_jobs debug summary: "
        + ", ".join(f"{k}={v}" for k, v in counters.items())
    )


def get_high_signal_jobs(
    db_path: Optional[str] = None,
    min_weight: int = 70,
) -> List[dict]:
    """Positive nudge rows: feedback='good' OR weight >= min_weight OR status in POSITIVE_GENRE.

    Including POSITIVE_GENRE_STATUSES means a job the user applied to (even one that
    closed without an offer) stays a positive genre signal for the next /fetchjobs run.
    The specific row won't be re-surfaced (discovery dedup excludes terminal statuses),
    but similar jobs will keep being prioritized.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    ensure_feedback_columns(conn)
    placeholders = ",".join("?" * len(POSITIVE_GENRE_STATUSES))
    params = (min_weight, *sorted(POSITIVE_GENRE_STATUSES))
    cur = conn.execute(
        f"""SELECT id, company, title, theme, rationale, user_weight, user_feedback, status
           FROM jobs
           WHERE user_feedback = 'good'
              OR COALESCE(user_weight, 50) >= ?
              OR status IN ({placeholders})
           ORDER BY
              CASE WHEN status = 'Won' THEN 0
                   WHEN status = 'InProgress' THEN 1
                   WHEN status = 'Applied' THEN 2
                   WHEN status = 'Closed' THEN 3
                   ELSE 4 END,
              COALESCE(user_weight, 50) DESC""",
        params,
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "company": r[1],
            "title": r[2],
            "theme": r[3],
            "rationale": r[4],
            "user_weight": r[5] if r[5] is not None else 50,
            "user_feedback": r[6],
            "status": r[7],
        }
        for r in rows
    ]


def update_jobs_status(
    updates: List[Tuple[str, str]],
    db_path: Optional[str] = None,
) -> int:
    """Set status on one or more rows. Each tuple: (job_id, new_status).
    Validates new_status against VALID_USER_STATUSES (rejects 'quarantine' — that's pruner-only).
    Returns count of rows actually updated.
    """
    invalid = [(jid, s) for jid, s in updates if s not in VALID_USER_STATUSES]
    if invalid:
        raise ValueError(f"invalid status values for user updates: {invalid}; allowed: {sorted(VALID_USER_STATUSES)}")
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    n = 0
    for job_id, new_status in updates:
        cur = conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (new_status, job_id))
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def update_jobs_feedback_batch(
    updates: List[Tuple[str, Optional[str], int]],
    db_path: Optional[str] = None,
) -> None:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    ensure_feedback_columns(conn)
    for job_id, feedback, weight in updates:
        conn.execute(
            "UPDATE jobs SET user_feedback = ?, user_weight = ? WHERE id = ?",
            (feedback, weight, job_id),
        )
    conn.commit()
    conn.close()


# --- Two-strike pruning helpers (Item B from the self-improvement plan) -------
#
# Persistence Agent calls these after persist_jobs. mark_validation_failure
# increments the per-row counter; the caller (Persistence Agent in fetchjobs.md)
# decides whether to quarantine (count == 1) or delete (count >= 2). The
# pruned-history JSONL feeds the FPR sampler — closes the loop that lets us
# notice if our own pruner is misbehaving.

_PRUNED_HISTORY_PATH = "data/pruned_history.jsonl"


def mark_validation_failure(
    conn: sqlite3.Connection,
    link: str,
    reasons: List[str],
) -> int:
    """Increment failed_validation_count for the row and stamp last_validated_at.

    Returns the new count. Caller decides whether to quarantine or delete.
    reasons is captured here only as a no-op argument so the call-site reads
    naturally; it is logged on delete by delete_and_log_pruned.
    """
    ensure_pruning_columns(conn)
    job_id = _job_id(link)
    now = _now_iso()
    cur = conn.execute(
        "SELECT failed_validation_count FROM jobs WHERE id = ?",
        (job_id,),
    )
    row = cur.fetchone()
    if row is None:
        return 0
    current = row[0] if row[0] is not None else 0
    new_count = int(current) + 1
    conn.execute(
        "UPDATE jobs SET failed_validation_count = ?, last_validated_at = ? WHERE id = ?",
        (new_count, now, job_id),
    )
    conn.commit()
    _ = reasons  # reserved for future structured logging; currently informational at call-site
    return new_count


def mark_validation_success(conn: sqlite3.Connection, link: str) -> None:
    """Reset failed_validation_count to 0 and stamp last_validated_at to now."""
    ensure_pruning_columns(conn)
    job_id = _job_id(link)
    now = _now_iso()
    conn.execute(
        "UPDATE jobs SET failed_validation_count = 0, last_validated_at = ? WHERE id = ?",
        (now, job_id),
    )
    conn.commit()


def delete_and_log_pruned(
    conn: sqlite3.Connection,
    link: str,
    company: str,
    title: str,
    fail_reasons: List[str],
    first_failed_at: Optional[str],
    run_id: str,
    history_path: str = _PRUNED_HISTORY_PATH,
) -> None:
    """Delete the row and append a structured record to data/pruned_history.jsonl."""
    ensure_pruning_columns(conn)
    job_id = _job_id(link)
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    record = {
        "link": link,
        "company": company,
        "title": title,
        "fail_reasons": list(fail_reasons) if fail_reasons else [],
        "first_failed_at": first_failed_at,
        "deleted_at": _now_iso(),
        "run_id": run_id,
    }
    p = Path(history_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


def force_delete_expired_quarantine(
    conn: sqlite3.Connection,
    ttl_days: int = 30,
) -> int:
    """Delete quarantined rows whose last_validated_at is older than ttl_days.

    Returns the count deleted. Operates only on rows with status='quarantine'.
    Rows with NULL last_validated_at are left alone (treated as not-yet-validated).
    """
    ensure_pruning_columns(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    cur = conn.execute(
        "SELECT id FROM jobs WHERE status = 'quarantine' "
        "AND last_validated_at IS NOT NULL AND last_validated_at < ?",
        (cutoff,),
    )
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return 0
    conn.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    return len(ids)


def sample_pruned_links_for_fpr_check(
    history_path: str = _PRUNED_HISTORY_PATH,
    sample_size: int = 10,
) -> List[Dict[str, Any]]:
    """Return a random sample of prior-pruned {link, company, title} records.

    The Persistence Agent re-HEAD-checks these to detect pruner false-positive
    drift. Returns [] if the history file does not exist or is empty.
    """
    p = Path(history_path)
    if not p.exists():
        return []
    records: List[Dict[str, Any]] = []
    with p.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or "link" not in rec:
                continue
            records.append({
                "link": rec.get("link"),
                "company": rec.get("company"),
                "title": rec.get("title"),
            })
    if not records:
        return []
    n = min(sample_size, len(records))
    return random.sample(records, n)
