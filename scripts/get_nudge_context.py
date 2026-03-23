#!/usr/bin/env python3
"""
Output high-signal jobs (user_feedback='good' or user_weight >= 70) for /fetchjobs context.
(Profile file: data/candidate_info.json — same as load_config.)
Run from project root: uv run python scripts/get_nudge_context.py
The agent should run this before building queries and use the output to nudge search and scoring.
"""
import json
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.persistence import get_high_signal_jobs
from job_finder.paths import get_db_path


def main() -> None:
    if not os.path.exists(get_db_path()):
        print(json.dumps({"high_signal_jobs": [], "message": "No DB yet; run orchestrator then /fetchjobs."}))
        return
    jobs = get_high_signal_jobs(min_weight=70)
    out = {
        "high_signal_jobs": jobs,
        "count": len(jobs),
        "message": f"Use these {len(jobs)} job(s) as positive signals: bias queries and scoring toward similar roles.",
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
