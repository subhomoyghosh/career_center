#!/usr/bin/env python3
"""
Set user_feedback, user_weight, or status on a job via CLI.
Mirrors what the Streamlit UI does, so users aren't forced into the app to provide signal.

Two action families:

  Feedback / weight actions:
    uv run python scripts/feedback.py <job_id_or_link> good   [--weight N]
    uv run python scripts/feedback.py <job_id_or_link> bad    [--weight N]
    uv run python scripts/feedback.py <job_id_or_link> clear  [--weight N]

  Lifecycle status actions (DISENTANGLED from feedback — see persistence.JOB_STATUS_*):
    uv run python scripts/feedback.py <job_id_or_link> new
    uv run python scripts/feedback.py <job_id_or_link> applied
    uv run python scripts/feedback.py <job_id_or_link> inprogress
    uv run python scripts/feedback.py <job_id_or_link> closed     # applied + didn't go through; GENRE-POSITIVE
    uv run python scripts/feedback.py <job_id_or_link> won        # got offer
    uv run python scripts/feedback.py <job_id_or_link> notforme   # user-rejected without applying; GENRE-NEGATIVE

  Status actions only update `status`. To set both (e.g., applied + weight to lower so
  the row stops re-surfacing while staying genre-positive), run twice:
    uv run python scripts/feedback.py <link> closed
    uv run python scripts/feedback.py <link> clear --weight 20   # demotes specific row only

If you pass a full URL, it's hashed to md5 to match the DB id (same as persistence.py).
If you pass a 32-hex-char string, it's treated as the id directly.
If the job is not in the DB, prints an error and exits non-zero — never inserts.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_src = _root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from job_finder.paths import get_db_path
from job_finder.persistence import update_jobs_feedback_batch, update_jobs_status


FEEDBACK_ACTIONS = {"good", "bad", "clear"}
STATUS_ACTIONS = {
    "new": "New",
    "applied": "Applied",
    "inprogress": "InProgress",
    "closed": "Closed",
    "won": "Won",
    "notforme": "NotForMe",
}
ALL_ACTIONS = sorted(FEEDBACK_ACTIONS | set(STATUS_ACTIONS.keys()))


def resolve_job_id(arg: str) -> str:
    """If arg is a 32-hex md5, return as-is. Else treat as link and hash it."""
    arg = arg.strip()
    if len(arg) == 32 and all(c in "0123456789abcdef" for c in arg.lower()):
        return arg.lower()
    return hashlib.md5(arg.encode()).hexdigest()


def fetch_current(conn: sqlite3.Connection, job_id: str):
    row = conn.execute(
        "SELECT company, title, link, user_feedback, user_weight, status FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return row


def main():
    p = argparse.ArgumentParser(
        description="Set user_feedback / user_weight / status on a job from the CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Disentanglement: status='Closed' (applied + didn't go through) is GENRE-POSITIVE. "
            "Use lifecycle status actions to track applications without polluting the bad-set."
        ),
    )
    p.add_argument("job", help="Job id (md5) or full link URL")
    p.add_argument(
        "action",
        choices=ALL_ACTIONS,
        help=(
            "Feedback actions: good|bad|clear. "
            "Lifecycle status actions: new|applied|inprogress|closed|won|notforme."
        ),
    )
    p.add_argument(
        "--weight",
        type=int,
        default=None,
        metavar="N",
        help="Set user_weight (0-100). Applies only with feedback actions; ignored for status actions.",
    )
    args = p.parse_args()

    if args.weight is not None and not (0 <= args.weight <= 100):
        print(f"ERROR: --weight must be 0..100, got {args.weight}", file=sys.stderr)
        sys.exit(2)

    job_id = resolve_job_id(args.job)
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    before = fetch_current(conn, job_id)
    if before is None:
        conn.close()
        print(
            f"ERROR: no job found with id {job_id} (resolved from '{args.job}')",
            file=sys.stderr,
        )
        print(
            "Tip: pass either the md5 id from the jobs table, or the full link URL.",
            file=sys.stderr,
        )
        sys.exit(1)

    company, title, link, prev_fb, prev_w, prev_status = before

    if args.action in STATUS_ACTIONS:
        # Lifecycle action: update only the status column.
        if args.weight is not None:
            print(
                "WARNING: --weight is ignored for status actions. Run a separate "
                "command with a feedback action to change weight.",
                file=sys.stderr,
            )
        new_status = STATUS_ACTIONS[args.action]
        update_jobs_status([(job_id, new_status)])
    else:
        # Feedback action.
        feedback_value = None if args.action == "clear" else args.action

        # update_jobs_feedback_batch expects List[Tuple[str, Optional[str], int]]:
        # (job_id, feedback, weight). Weight is required — if --weight isn't passed,
        # pass through the current weight so it remains unchanged.
        # If the row had a NULL weight and no --weight is provided, default to 50.
        if args.weight is not None:
            weight_value = args.weight
        elif prev_w is not None:
            weight_value = prev_w
        else:
            weight_value = 50

        update_jobs_feedback_batch([(job_id, feedback_value, weight_value)])

    after = fetch_current(conn, job_id)
    conn.close()

    print(
        json.dumps(
            {
                "job_id": job_id,
                "company": company,
                "title": title,
                "link": link,
                "before": {
                    "user_feedback": prev_fb,
                    "user_weight": prev_w,
                    "status": prev_status,
                },
                "after": {
                    "user_feedback": after[3],
                    "user_weight": after[4],
                    "status": after[5],
                },
                "changed": (prev_fb != after[3]) or (prev_w != after[4]) or (prev_status != after[5]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
