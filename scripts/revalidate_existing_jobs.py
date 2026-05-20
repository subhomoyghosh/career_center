"""Re-validate jobs already stored in SQLite against the current link_validation rules.

Why: after extending link_validation to catch the builtin.com "this job was
removed" wording and SPA-shell (Workday) leaks, rows that were inserted under
the older rules can remain in the DB indefinitely. This script re-runs the
current validator over them and feeds the existing
mark_validation_failure / mark_validation_success plumbing so the standard
pruner pipeline takes over from there. No schema changes.

Idempotent: safe to re-run. Only touches rows whose status indicates they
have not been acted on by the user.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from job_finder.link_validation import filter_valid_job_links  # noqa: E402
from job_finder.paths import get_db_path  # noqa: E402
from job_finder.persistence import (  # noqa: E402
    delete_and_log_pruned,
    mark_validation_failure,
    mark_validation_success,
)


# Statuses we touch. Anything the user has actively triaged is left alone.
ACTIONABLE_STATUSES = ("New", "InProgress")


def _domain(link: str) -> str:
    try:
        return (urlparse(link).hostname or "").lower()
    except ValueError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print which rows would be marked failed/passed without writing to the DB.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Delete dropped rows directly via delete_and_log_pruned, bypassing the "
            "two-strike counter. Use for a confident one-shot cleanup after extending "
            "the validator. Rows are logged to data/pruned_history.jsonl."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Optional override for the SQLite path. Defaults to get_db_path().",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    if not Path(db_path).exists():
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ACTIONABLE_STATUSES)
    rows = list(
        conn.execute(
            f"SELECT id, company, title, link, status FROM jobs WHERE status IN ({placeholders})",
            ACTIONABLE_STATUSES,
        )
    )
    if not rows:
        print("No actionable rows found.")
        return 0

    by_link = {r["link"]: r for r in rows}
    jobs = [{"id": r["id"], "title": r["title"], "link": r["link"]} for r in rows]
    print(
        f"Re-validating {len(jobs)} rows from {db_path} "
        f"(dry_run={args.dry_run}, prune={args.prune}) ..."
    )

    valid_jobs = filter_valid_job_links(jobs, require_title_in_body=False)
    valid_links = {j["link"] for j in valid_jobs}

    dropped = [j for j in jobs if j["link"] not in valid_links]
    kept = [j for j in jobs if j["link"] in valid_links]

    dropped_by_domain: Counter[str] = Counter(_domain(j["link"]) for j in dropped)

    print()
    print(f"Result: kept={len(kept)} dropped={len(dropped)}")
    if dropped_by_domain:
        print("Dropped by domain:")
        for dom, n in dropped_by_domain.most_common():
            print(f"  {dom or '(unknown)'}: {n}")
        print()
        print("Dropped URLs:")
        for j in dropped:
            print(f"  - {j['link']}")

    if args.dry_run:
        print()
        print("[dry-run] no DB writes performed.")
        return 0

    run_id = f"revalidate_existing_jobs_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    if args.prune:
        for j in dropped:
            row = by_link[j["link"]]
            delete_and_log_pruned(
                conn,
                link=j["link"],
                company=row["company"] or "",
                title=row["title"] or "",
                fail_reasons=["revalidate_existing_jobs"],
                first_failed_at=None,
                run_id=run_id,
            )
    else:
        for j in dropped:
            mark_validation_failure(conn, j["link"], reasons=["revalidate_existing_jobs"])

    for j in kept:
        mark_validation_success(conn, j["link"])

    print()
    if args.prune:
        print(f"DB updated: {len(dropped)} rows deleted (logged to data/pruned_history.jsonl), {len(kept)} success marks.")
    else:
        print(f"DB updated: {len(dropped)} failure marks, {len(kept)} success marks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
