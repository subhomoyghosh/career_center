"""
Filter job dicts to only those whose link is accessible and the page looks like a live job listing.
Second pass before persist_jobs; input is not mutated.

Diagnostic summarizers below (compute_pruner_fpr_alert / count_stale_links_*) are
called by the Persistence Agent during /fetchjobs Step 7 to emit run_diagnostics
fields; the agent does the HEAD probes and writes data/fpr_recheck_latest.json,
these functions only summarize the latest artifact + DB state.
"""
import json
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

import requests

from job_finder.paths import get_data_dir, get_db_path

logger = logging.getLogger(__name__)

_PLACEHOLDER_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "localhost",
}

DEAD_PAGE_PHRASES = [
    "no longer available",
    "job expired",
    "error 404",
    "404 - page not found",
    "404 not found",
    "page not found",
    "this job has been removed",
    "job has been closed",
    "no longer accepting applications",
    "role has been filled",
    "unable to find this job",
    "job not found",
    "doesn't exist",
    "does not exist",
    "oops, an error occurred",
    "something went wrong",
    "access denied",
]

# Highly specific phrases — a single match is sufficient to mark dead.
STRONG_DEAD_PHRASES = (
    "this job is no longer",
    "this position is no longer",
    "position has been filled",
    "listing has expired",
)

# If body is huge but these dominate, treat as board index not a single job (heuristic).
_BOARD_ONLY_HINTS = (
    "all jobs",
    "department",
    "filter by",
    "select a job",
    "no jobs match",
    "view all openings",
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MIN_BODY_CHARS = 400
MIN_BODY_CHARS_LINKEDIN = 150
MIN_BODY_CHARS_RELAXED = 200
MIN_TITLE_ECHO_CHARS = 12


def _link_valid(link: str) -> bool:
    if not (isinstance(link, str) and len(link.strip()) > 0):
        return False
    s = link.strip().lower()
    if not s.startswith(("http://", "https://")):
        return False
    try:
        host = (urlparse(s).hostname or "").lower()
    except ValueError:
        return False
    return host not in _PLACEHOLDER_HOSTS


def _normalize_link(link: Any) -> str:
    """
    Normalize agent-provided links into a fully qualified http(s) URL.

    This mirrors the persistence layer normalization but stays local to avoid
    circular imports.
    """
    if not isinstance(link, str):
        return ""
    raw = link.strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        return raw
    if low.startswith("//"):
        return "https:" + raw
    if "." in low and "/" in raw and " " not in raw and "\n" not in raw and "\t" not in raw:
        return "https://" + raw
    return ""


def _classify_status(status_code: int) -> str:
    """Bucket an HTTP status into success/terminal/transient/other.

    - terminal: listing is genuinely gone (404, 410). Drop the job.
    - transient: bot-block / rate-limit / server hiccup (403, 408, 425, 429, 5xx).
      Keep the job; a real death will reappear next run.
    - success: 2xx — proceed to content check.
    - other: anything else (e.g. 3xx that didn't redirect, unusual 4xx).
      Treated as terminal (legacy behavior).
    """
    if 200 <= status_code < 300:
        return "success"
    if status_code in (404, 410):
        return "terminal"
    if status_code in (403, 408, 425, 429) or 500 <= status_code < 600:
        return "transient"
    return "terminal"


def _fetch_response(url: str, timeout_sec: int, session: requests.Session):
    try:
        return session.get(
            url,
            timeout=timeout_sec,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except (requests.RequestException, requests.Timeout):
        return None


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _title_words_echoed_in_body(title: str, body_lower: str) -> bool:
    """Require a meaningful chunk of the job title to appear (reduces generic board pages)."""
    if not title or len(title.strip()) < MIN_TITLE_ECHO_CHARS:
        return True
    t = _normalize_for_match(title)
    if len(t) < MIN_TITLE_ECHO_CHARS:
        return True
    # Longest phrase match: take first 4 words or full title if short
    words = t.split()[:6]
    if not words:
        return True
    phrase = " ".join(words[: min(4, len(words))])
    if len(phrase) >= MIN_TITLE_ECHO_CHARS and phrase in body_lower:
        return True
    # Fallback: at least 2 significant tokens (len>2)
    sig = [w for w in words if len(w) > 2][:3]
    hits = sum(1 for w in sig if w in body_lower)
    return hits >= max(1, len(sig) - 1)


def _looks_like_board_without_job(title: str, body_lower: str) -> bool:
    if not title or len(_normalize_for_match(title)) < 8:
        return False
    if not _title_words_echoed_in_body(title, body_lower):
        if any(h in body_lower for h in _BOARD_ONLY_HINTS):
            return True
    return False


def _final_url_indicates_dead(response, original_url: str = "") -> bool:
    """
    Detect dead-job redirects that return 200 but land on an error/board page.

    Greenhouse: dead `/jobs/{id}` URLs redirect to `/{org}?error=true`.
    Generic ATS pattern: original path contained `/jobs/{id}` but final URL no longer does.
    """
    final_url = getattr(response, "url", "") or ""
    if not final_url:
        return False
    final_low = final_url.lower()
    if "error=true" in final_low or "error_code=" in final_low:
        return True
    if original_url:
        m = re.search(r"/jobs/([0-9a-f-]{4,})", original_url, flags=re.I)
        if m:
            job_id = m.group(1).lower()
            if job_id not in final_low:
                return True
    return False


def _body_parseable_and_not_dead(
    response,
    job_title: str = "",
    *,
    min_body_chars: int = MIN_BODY_CHARS,
    is_linkedin: bool = False,
    original_url: str = "",
) -> bool:
    try:
        response.raise_for_status()
        text = response.text
    except (requests.RequestException, ValueError):
        return False
    if _final_url_indicates_dead(response, original_url=original_url):
        return False
    if not text or len(text.strip()) < min_body_chars:
        return False
    lower = text.lower()
    if is_linkedin:
        # LinkedIn often returns sign-in / bot interstitials with 200s; reject those.
        if any(
            phrase in lower
            for phrase in (
                "sign in",
                "log in",
                "your session",
                "to continue",
                "we've detected unusual activity",
            )
        ):
            return False
    # Strong phrases are specific enough that one match implies dead.
    if any(phrase in lower for phrase in STRONG_DEAD_PHRASES):
        return False
    # Generic phrases are too loose individually; require >=2 distinct matches.
    distinct_dead_hits = sum(1 for phrase in DEAD_PAGE_PHRASES if phrase.lower() in lower)
    if distinct_dead_hits >= 2:
        return False
    if job_title and _looks_like_board_without_job(job_title, lower):
        return False
    if job_title and not _title_words_echoed_in_body(job_title, lower):
        return False
    return True


def filter_valid_job_links(
    jobs: List[dict],
    timeout_sec: int = 12,
    check_content: bool = True,
    require_title_in_body: bool = True,
    fallback_to_link_only_on_network_failure: bool = True,
    fallback_to_link_only_on_content_failure: bool = True,
    max_workers: int = 10,
) -> List[dict]:
    """
    Keep jobs whose link returns 2xx, body is substantial, not a dead-page message,
    and (unless LinkedIn) the HTML should echo enough of the job title to avoid generic board pages.
    Set require_title_in_body=False to only check HTTP + dead phrases + min length.

    Fetches run in parallel (max_workers threads) — wall time is dominated by the
    slowest single request rather than sum of all timeouts.

    If the runtime cannot fetch pages (e.g. network is blocked), this function can
    otherwise drop everything. When enabled, we detect the "all fetches failed
    with no response" case and fall back to link-only validation (syntactic URL
    checks + placeholder host removal).

    Additionally, when `require_title_in_body=False`, some ATS pages return a
    short/bot-interstitial HTML but still have a valid job URL. When enabled,
    if *all* candidates fail content parsing/dead-page checks (but HTTP was 2xx),
    we fall back to link-only validation as a best-effort.
    """
    if not jobs:
        return []

    print(
        "filter_valid_job_links debug: "
        + f"received={len(jobs)} require_title_in_body={require_title_in_body} check_content={check_content}"
    )

    # Phase 1: syntactic validation — instant, no network.
    candidates: List[tuple] = []
    invalid_link_count = 0
    for job in jobs:
        link_norm = _normalize_link(job.get("link"))
        if not _link_valid(link_norm):
            invalid_link_count += 1
        else:
            candidates.append((job, link_norm))

    counters = {
        "checked": len(candidates),
        "invalid_link": invalid_link_count,
        "fetch_none": 0,
        "http_non_2xx": 0,
        "http_terminal_4xx": 0,
        "http_transient": 0,
        "content_failed": 0,
        "returned": 0,
    }

    if not candidates:
        counters["returned"] = 0
        counters["fallback_to_link_only"] = False
        counters["fallback_reason"] = "none"
        print("filter_valid_job_links debug summary: " + ", ".join(f"{k}={v}" for k, v in counters.items()))
        return []

    # Phase 2: parallel HTTP fetches — one thread per candidate, bounded by max_workers.
    # requests.Session is thread-safe for concurrent reads; connection pool is shared.
    with requests.Session() as session:
        session.max_redirects = 5

        def _fetch(args):
            job, link_norm = args
            return job, link_norm, _fetch_response(link_norm, timeout_sec, session)

        with ThreadPoolExecutor(max_workers=min(max_workers, len(candidates))) as pool:
            fetch_results: List[tuple] = list(pool.map(_fetch, candidates))

    # Phase 3: content checks — CPU-bound string ops, serial, fast.
    valid = []
    for job, link_norm, r in fetch_results:
        link_s = link_norm.lower()
        is_linkedin = "linkedin.com" in link_s
        relax_title = is_linkedin or not require_title_in_body
        title = str(job.get("title") or "")

        if r is None:
            counters["fetch_none"] += 1
            continue
        status_bucket = _classify_status(r.status_code)
        if status_bucket == "terminal":
            counters["http_terminal_4xx"] += 1
            counters["http_non_2xx"] += 1
            continue
        if status_bucket == "transient":
            counters["http_transient"] += 1
            counters["http_non_2xx"] += 1
            # Keep the job alive this run; transient errors don't persist for weeks.
            job2 = dict(job)
            job2["link"] = link_norm
            job2["link_validation_transient"] = True
            valid.append(job2)
            continue
        if check_content:
            tcheck = "" if relax_title else title
            min_body = (
                MIN_BODY_CHARS_LINKEDIN
                if is_linkedin
                else (MIN_BODY_CHARS if require_title_in_body else MIN_BODY_CHARS_RELAXED)
            )
            if not _body_parseable_and_not_dead(
                r,
                tcheck,
                min_body_chars=min_body,
                is_linkedin=is_linkedin,
                original_url=link_norm,
            ):
                counters["content_failed"] += 1
                continue
        job2 = dict(job)
        job2["link"] = link_norm
        valid.append(job2)

    counters["returned"] = len(valid)
    dropped = len(jobs) - len(valid)

    # Fallback: all fetches got no response → network constraint, use link-only.
    if (
        fallback_to_link_only_on_network_failure
        and not valid
        and counters["checked"] > 0
        and counters["fetch_none"] == counters["checked"]
        and counters["http_non_2xx"] == 0
        and counters["content_failed"] == 0
    ):
        valid = [dict(job) | {"link": link_norm} for job, link_norm in candidates]
        counters["returned"] = len(valid)
        counters["fallback_to_link_only"] = True
        counters["fallback_reason"] = "network_fetch_none_all"
        print(
            "filter_valid_job_links debug fallback: "
            + f"all fetches failed; returning link-only jobs returned={len(valid)}"
        )
    # Fallback: all fetches returned 2xx but content checks failed → bot interstitials.
    elif (
        fallback_to_link_only_on_content_failure
        and not valid
        and counters["checked"] > 0
        and counters["fetch_none"] == 0
        and counters["http_non_2xx"] == 0
        and counters["content_failed"] == counters["checked"]
        and not require_title_in_body
    ):
        valid = [dict(job) | {"link": link_norm} for job, link_norm in candidates]
        counters["returned"] = len(valid)
        counters["fallback_to_link_only"] = True
        counters["fallback_reason"] = "content_failed_all"
        print(
            "filter_valid_job_links debug fallback: "
            + f"all content checks failed; returning link-only jobs returned={len(valid)}"
        )
    else:
        counters["fallback_to_link_only"] = False
        counters["fallback_reason"] = "none"

    if dropped > 0:
        logger.info(
            "Link validation: %d checked, %d removed (bad URL, HTTP error, dead listing, or title mismatch).",
            len(jobs),
            dropped,
        )
    print(
        "filter_valid_job_links debug summary: "
        + ", ".join(f"{k}={v}" for k, v in counters.items())
    )
    return valid


# --- Diagnostic summarizers (consumed by /fetchjobs Step 7) -------------------
#
# Idempotent, side-effect-free. Each returns zeros/False when the underlying
# artifact or column is missing, so the diagnostics line always emits cleanly.

_FPR_RECHECK_ARTIFACT = "fpr_recheck_latest.json"
_FPR_ALERT_THRESHOLD = 0.05
_FPR_MIN_SAMPLE = 10


def _fpr_artifact_path() -> str:
    return os.path.join(get_data_dir(), _FPR_RECHECK_ARTIFACT)


def compute_pruner_fpr_alert(
    artifact_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Summarize the latest pruner FPR re-check artifact.

    Returns {'pruner_fpr_alert': bool, 'fpr': float, 'sample_size': int,
    'resurrected_ids': List[str]}. If the artifact does not exist or the sample
    is below _FPR_MIN_SAMPLE (10), the alert is False and fpr is 0.0.

    The artifact at data/fpr_recheck_latest.json is written by the Persistence
    Agent after it HEAD-checks links sampled via sample_pruned_links_for_fpr_check.
    Expected schema (any missing field is treated as zero/empty):
      {"sample_size": int, "resurrected": [{"link": str, ...}, ...]}
    """
    path = Path(artifact_path or _fpr_artifact_path())
    zero = {
        "pruner_fpr_alert": False,
        "fpr": 0.0,
        "sample_size": 0,
        "resurrected_ids": [],
    }
    if not path.exists():
        return zero
    try:
        with path.open("r") as f:
            rec = json.load(f)
    except (json.JSONDecodeError, OSError):
        return zero
    if not isinstance(rec, dict):
        return zero
    try:
        sample_size = int(rec.get("sample_size", 0) or 0)
    except (TypeError, ValueError):
        sample_size = 0
    resurrected = rec.get("resurrected") or []
    if not isinstance(resurrected, list):
        resurrected = []
    resurrected_ids = [
        str(r.get("link", "")) for r in resurrected
        if isinstance(r, dict) and r.get("link")
    ]
    if sample_size < _FPR_MIN_SAMPLE:
        return {
            "pruner_fpr_alert": False,
            "fpr": 0.0,
            "sample_size": sample_size,
            "resurrected_ids": resurrected_ids,
        }
    fpr = len(resurrected_ids) / sample_size if sample_size > 0 else 0.0
    return {
        "pruner_fpr_alert": fpr > _FPR_ALERT_THRESHOLD,
        "fpr": fpr,
        "sample_size": sample_size,
        "resurrected_ids": resurrected_ids,
    }


def _open_jobs_db(db_path: Optional[str]) -> Optional[sqlite3.Connection]:
    """Open the jobs DB read-only-safe. Returns None if the file is missing
    or the jobs table does not yet exist."""
    path = db_path or get_db_path()
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        )
        if cur.fetchone() is None:
            conn.close()
            return None
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _jobs_columns(conn: sqlite3.Connection) -> List[str]:
    try:
        cur = conn.execute("PRAGMA table_info(jobs)")
        return [row[1] for row in cur.fetchall()]
    except sqlite3.Error:
        return []


def count_stale_links_quarantined(db_path: Optional[str] = None) -> int:
    """Return COUNT(*) of jobs.status = 'quarantine'. Returns 0 if the DB,
    table, or status column is missing."""
    conn = _open_jobs_db(db_path)
    if conn is None:
        return 0
    try:
        if "status" not in _jobs_columns(conn):
            return 0
        cur = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'quarantine'"
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def count_stale_links_ttl_expired(
    db_path: Optional[str] = None,
    ttl_days: int = 60,
) -> int:
    """Return COUNT(*) of rows whose last_validated_at is older than ttl_days.

    Returns 0 if the DB, table, or last_validated_at column is missing.
    Rows with NULL last_validated_at are excluded (treated as not-yet-validated).
    """
    conn = _open_jobs_db(db_path)
    if conn is None:
        return 0
    try:
        if "last_validated_at" not in _jobs_columns(conn):
            return 0
        cur = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE last_validated_at IS NOT NULL "
            "AND julianday('now') - julianday(last_validated_at) > ?",
            (ttl_days,),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
