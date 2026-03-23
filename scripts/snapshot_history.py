#!/usr/bin/env python3
"""
List snapshot history (candidate profile, jobs table, intelligence / wisdom).
Run from project root: uv run python scripts/snapshot_history.py [candidate|jobs|intelligence]
"""
import json
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.history import get_candidate_snapshot, list_snapshots


def main() -> None:
    kind = (sys.argv[1] if len(sys.argv) > 1 else "candidate").lower()
    if kind not in ("candidate", "jobs", "intelligence"):
        print("Usage: snapshot_history.py [candidate|jobs|intelligence]")
        sys.exit(2)
    rows = list_snapshots(kind, limit=30)
    print(json.dumps(rows, indent=2))
    if kind == "candidate" and rows:
        latest_id = rows[0]["id"]
        payload = get_candidate_snapshot(latest_id)
        if payload:
            print(f"\n--- Latest candidate snapshot id={latest_id} (keys) ---")
            print(list(payload.keys()))


if __name__ == "__main__":
    main()
