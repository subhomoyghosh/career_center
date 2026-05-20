#!/usr/bin/env python3
"""
Persist-only helper for /fetchjobs.

Cursor (agent + MCP skills) is responsible for:
  - searching/discovering candidate job URLs
  - scoring/theme/rationale generation
  - preparing a job list for persistence

This script ONLY:
  - validates listing URLs via `filter_valid_job_links`
  - upserts jobs into `data/sovereign_agent.db`
  - (optionally) updates wisdom if you pass `--wisdom`

If you run this without `--jobs-json` / `--jobs-file`, it fails loudly so
we don't accidentally reintroduce hard-coded scraping/persistence.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.link_validation import filter_valid_job_links
from job_finder.paths import get_db_path
from job_finder.persistence import (
    delete_and_log_pruned,
    mark_validation_failure,
    mark_validation_success,
    persist_jobs,
)
from job_finder.wisdom import update_wisdom


_SWEEP_STATUSES = ("New", "InProgress")
_TWO_STRIKE_THRESHOLD = 2
_SWEEP_HISTORY_PATH = os.path.join(_root, "data", "sweep_history.jsonl")


def _sweep_existing_actionable(conn: sqlite3.Connection, run_id: str) -> Tuple[int, int, int]:
    """Two-strike re-validation of existing actionable rows.

    Reuses filter_valid_job_links (the same content-aware gate new candidates
    pass through), so dead URLs surface here before /fetchjobs shows results.
    Zero token cost — pure HTTP via requests; the LLM is not invoked. Writes a
    one-line summary to data/sweep_history.jsonl so the run is auditable; full
    per-row deletion records continue to live in data/pruned_history.jsonl.
    Returns (checked, marked, deleted).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    placeholders = ",".join("?" for _ in _SWEEP_STATUSES)
    rows = list(
        conn.execute(
            f"SELECT id, company, title, link FROM jobs WHERE status IN ({placeholders})",
            _SWEEP_STATUSES,
        )
    )
    if not rows:
        _append_sweep_history({
            "run_id": run_id, "started_at": started_at, "elapsed_sec": 0.0,
            "checked": 0, "marked": 0, "deleted": 0,
            "marked_links": [], "deleted_links": [],
        })
        return 0, 0, 0

    jobs = [{"id": r[0], "title": r[2] or "", "link": r[3]} for r in rows]
    valid = filter_valid_job_links(jobs, require_title_in_body=False)
    valid_links = {j["link"] for j in valid}

    marked_links: List[str] = []
    deleted_links: List[str] = []
    for r in rows:
        link = r[3]
        if link in valid_links:
            mark_validation_success(conn, link)
            continue
        count = mark_validation_failure(conn, link, reasons=["fetchjobs_sweep"])
        if count >= _TWO_STRIKE_THRESHOLD:
            delete_and_log_pruned(
                conn,
                link=link,
                company=r[1] or "",
                title=r[2] or "",
                fail_reasons=["fetchjobs_sweep"],
                first_failed_at=None,
                run_id=run_id,
            )
            deleted_links.append(link)
        else:
            marked_links.append(link)
    elapsed = round(time.monotonic() - t0, 3)
    _append_sweep_history({
        "run_id": run_id, "started_at": started_at, "elapsed_sec": elapsed,
        "checked": len(rows), "marked": len(marked_links), "deleted": len(deleted_links),
        "marked_links": marked_links, "deleted_links": deleted_links,
    })
    return len(rows), len(marked_links), len(deleted_links)


def _append_sweep_history(record: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_SWEEP_HISTORY_PATH), exist_ok=True)
    with open(_SWEEP_HISTORY_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def _parse_jobs(jobs_json: str) -> List[Dict[str, Any]]:
    data = json.loads(jobs_json)
    if not isinstance(data, list):
        raise ValueError("`--jobs-json` must be a JSON array of job dicts.")
    return [dict(x) for x in data if isinstance(x, dict)]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--jobs-json",
        type=str,
        default="",
        help='JSON array string: [{"company","title","link","score","theme","rationale"}, ...]',
    )
    p.add_argument(
        "--jobs-file",
        type=str,
        default="",
        help="Path to a JSON file containing the same array.",
    )
    p.add_argument(
        "--wisdom",
        type=str,
        default="",
        help="Optional wisdom string to store into data/candidate_info.json.",
    )
    p.add_argument(
        "--no-wisdom",
        action="store_true",
        help="Do not call update_wisdom even if --wisdom is provided.",
    )
    p.add_argument(
        "--skip-sweep",
        action="store_true",
        help="Skip the pre-flight re-validation of existing actionable rows.",
    )
    args = p.parse_args()

    if args.jobs_file:
        with open(args.jobs_file, "r", encoding="utf-8") as f:
            jobs = _parse_jobs(f.read())
    elif args.jobs_json:
        jobs = _parse_jobs(args.jobs_json)
    else:
        raise SystemExit(
            "No jobs provided. `/fetchjobs` agent must supply discovered jobs to this script "
            "via `--jobs-json` or `--jobs-file`."
        )

    # Pre-flight sweep: re-validate existing actionable rows so dead listings
    # don't survive across /fetchjobs runs. Two-strike rule guards against
    # single-blip false positives. Zero token cost (no LLM in this path).
    if not args.skip_sweep:
        db_path = get_db_path()
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            try:
                run_id = "fetchjobs_sweep_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                checked, marked, deleted = _sweep_existing_actionable(conn, run_id)
                print(f"Pre-flight sweep: checked={checked} marked={marked} deleted={deleted}")
            finally:
                conn.close()

    # Link validation is the quality gate; persistence is just a DB upsert.
    valid_jobs = filter_valid_job_links(jobs, require_title_in_body=False)
    persist_jobs(valid_jobs)

    if not args.no_wisdom and args.wisdom:
        update_wisdom(args.wisdom)

    print(f"Upserted {len(valid_jobs)} validated job(s) into data/sovereign_agent.db")


if __name__ == "__main__":
    main()

