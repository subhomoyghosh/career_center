"""
Save and read jobs in the database.
Each job: id, company, title, link, score, theme, rationale, status, user_feedback, user_weight, first_seen, posted_at.
The id is a hash of the link so we don't add the same job twice.

first_seen: ISO 8601 UTC timestamp of when WE first persisted this row. Always populated.
posted_at: ISO 8601 UTC timestamp of when the employer posted the job (best-effort,
           extracted by the agent from sources like Lever's createdAt). May be NULL.
"""
import hashlib
import sqlite3
from datetime import datetime, timezone
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
    posted_at TEXT
)
"""


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

    payload = {
        "company": company,
        "title": title,
        "link": _normalize_link(link),
        "score": score,
        "theme": theme,
        "rationale": rationale,
        "posted_at": _normalize_posted_at(posted_at_raw),
    }

    # Ensure all required fields are present and non-empty.
    # posted_at is optional and intentionally excluded from this check.
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
    conn.execute(
        """
        INSERT INTO jobs (id, company, title, link, score, theme, rationale, status, user_feedback, user_weight)
        SELECT id, company, title, link, score, theme, rationale,
               COALESCE(status, 'New'), user_feedback, COALESCE(user_weight, 50)
        FROM jobs_legacy_mig
        """
    )
    conn.execute("DROP TABLE jobs_legacy_mig")
    conn.commit()


def create_jobs_table(db_path: Optional[str] = None) -> None:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.execute(JOBS_SCHEMA)
    ensure_feedback_columns(conn)
    migrate_jobs_table_drop_legacy_posting_columns(conn)
    ensure_recency_columns(conn)
    conn.close()


def persist_jobs(jobs: List[dict], db_path: Optional[str] = None) -> None:
    """
    Insert or replace jobs. Required keys: company, title, link, score, theme, rationale.
    Preserves user_feedback and user_weight on upsert.
    """
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    ensure_feedback_columns(conn)
    migrate_jobs_table_drop_legacy_posting_columns(conn)
    ensure_recency_columns(conn)
    counters = {
        "received": len(jobs),
        "persisted": 0,
        "skipped_non_dict": 0,
        "skipped_missing_or_adaptation_failed": 0,
        "skipped_type_normalization": 0,
        "skipped_placeholder_link": 0,
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
            payload: Dict[str, Any] = {
                "company": str(adapted["company"]),
                "title": str(adapted["title"]),
                "link": str(adapted["link"]),
                "score": int(adapted["score"]),
                "theme": str(adapted["theme"]),
                "rationale": str(adapted["rationale"]),
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
        row = conn.execute(
            "SELECT user_feedback, user_weight, status, first_seen, posted_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is not None:
            feedback, weight, status, existing_first_seen, existing_posted_at = row
            payload = {
                **payload,
                "user_feedback": feedback,
                "user_weight": weight if weight is not None else 50,
                "status": status or "New",
                "first_seen": existing_first_seen or _now_iso(),
                # Prefer the freshly extracted value; fall back to existing.
                # This lets a later Lever-API hit upgrade a row first persisted via aggregator.
                "posted_at": new_posted_at if new_posted_at else existing_posted_at,
            }
        else:
            payload = {
                **payload,
                "user_feedback": j.get("user_feedback"),
                "user_weight": j.get("user_weight", 50),
                "status": "New",
                "first_seen": _now_iso(),
                "posted_at": new_posted_at,
            }
        conn.execute(
            """INSERT OR REPLACE INTO jobs (
                id, company, title, link, score, theme, rationale, status, user_feedback, user_weight,
                first_seen, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        counters["persisted"] += 1
    conn.commit()
    conn.close()
    try:
        from job_finder.history import record_jobs_snapshot_from_db

        record_jobs_snapshot_from_db(path)
    except Exception:
        pass

    # Helpful debug for /fetchjobs failures: persistence is otherwise silent.
    print(
        "persist_jobs debug summary: "
        + ", ".join(f"{k}={v}" for k, v in counters.items())
    )


def get_high_signal_jobs(
    db_path: Optional[str] = None,
    min_weight: int = 70,
) -> List[dict]:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    ensure_feedback_columns(conn)
    cur = conn.execute(
        """SELECT company, title, theme, rationale, user_weight, user_feedback
           FROM jobs
           WHERE user_feedback = 'good' OR COALESCE(user_weight, 50) >= ?
           ORDER BY COALESCE(user_weight, 50) DESC""",
        (min_weight,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "company": r[0],
            "title": r[1],
            "theme": r[2],
            "rationale": r[3],
            "user_weight": r[4] if r[4] is not None else 50,
            "user_feedback": r[5],
        }
        for r in rows
    ]


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
