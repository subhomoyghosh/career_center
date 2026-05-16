"""
Filter job dicts to only those whose link is accessible and the page looks like a live job listing.
Second pass before persist_jobs; input is not mutated.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from typing import Any, List

import requests

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
    "position has been filled",
    "job has been closed",
    "no longer accepting applications",
    "this position is no longer",
    "role has been filled",
    "listing has expired",
    "unable to find this job",
    "job not found",
    "doesn't exist",
    "does not exist",
    "oops, an error occurred",
    "something went wrong",
    "access denied",
]

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
    for phrase in DEAD_PAGE_PHRASES:
        if phrase.lower() in lower:
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
    session = requests.Session()
    session.max_redirects = 5

    def _fetch(args):
        job, link_norm = args
        return job, link_norm, _fetch_response(link_norm, timeout_sec, session)

    fetch_results: List[tuple] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(candidates))) as pool:
        fetch_results = list(pool.map(_fetch, candidates))

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
        if not (200 <= r.status_code < 300):
            counters["http_non_2xx"] += 1
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
