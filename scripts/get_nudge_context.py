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

import sqlite3

from job_finder.persistence import get_high_signal_jobs
from job_finder.paths import get_db_path

_STOPWORDS = {"senior", "staff", "principal", "lead", "head", "of", "and", "the", "a", "an", "in", "at", "for", "i", "ii", "iii"}


def _bad_title_tokens(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT title FROM jobs WHERE user_feedback = 'bad'")
    rows = cur.fetchall()
    conn.close()
    freq: dict = {}
    for (title,) in rows:
        for tok in title.lower().split():
            tok = tok.strip("(),/-")
            if tok and tok not in _STOPWORDS and len(tok) > 2:
                freq[tok] = freq.get(tok, 0) + 1
    return sorted(freq, key=lambda t: -freq[t])


def main() -> None:
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(json.dumps({"high_signal_jobs": [], "message": "No DB yet; run orchestrator then /fetchjobs."}))
        return
    jobs = get_high_signal_jobs(min_weight=70)
    bad_tokens = _bad_title_tokens(db_path)
    out = {
        "high_signal_jobs": jobs,
        "count": len(jobs),
        "message": f"Use these {len(jobs)} job(s) as positive signals: bias queries and scoring toward similar roles.",
        "bad_feedback_title_tokens": bad_tokens,
        "bad_feedback_note": "Title tokens from user_feedback='bad' jobs, frequency-ranked. Use for CANDIDATE_PROFILE_DRIFT: propose adding top tokens to noise_keywords if absent.",
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
