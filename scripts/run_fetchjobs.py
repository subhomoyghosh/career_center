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
import sys
from typing import Any, Dict, List

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.link_validation import filter_valid_job_links
from job_finder.persistence import persist_jobs
from job_finder.wisdom import update_wisdom


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

    # Link validation is the quality gate; persistence is just a DB upsert.
    valid_jobs = filter_valid_job_links(jobs, require_title_in_body=False)
    persist_jobs(valid_jobs)

    if not args.no_wisdom and args.wisdom:
        update_wisdom(args.wisdom)

    print(f"Upserted {len(valid_jobs)} validated job(s) into data/sovereign_agent.db")


if __name__ == "__main__":
    main()

