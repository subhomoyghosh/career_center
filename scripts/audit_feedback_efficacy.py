#!/usr/bin/env python3
"""
Cross-run feedback efficacy audit (Item H).

Pure read-only analysis. Computes three RLHF-style observability metrics from the
main DB + `data/history/jobs_history.db` snapshots:

  1. positive_followup_rate          — did the system act on positive feedback?
  2. negative_followup_avoidance_rate — did the system stop surfacing what user rejected?
  3. user_reaction_matrix             — did the user agree with the system's last scoring?

Plus a bonus `ui_engagement_rate` across the most recent N snapshots.

These metrics are observability for the human reader and the `/improve` LLM stage.
They MUST NOT be used as auto-tuning gradients (see `_observability_note`).

Usage:
    uv run python scripts/audit_feedback_efficacy.py
    uv run python scripts/audit_feedback_efficacy.py --lookback-snapshots 5 --verbose
    uv run python scripts/audit_feedback_efficacy.py --title-overlap-threshold 3

CLI exit code is always 0 unless a real exception escapes. "audit_failed: true" is
a normal output path (e.g., < 2 snapshots) and still exits 0 — consumers parse JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Stopword set is duplicated from scripts/get_nudge_context.py so this audit is
# stdlib-only and never imports project code (read-only contract). Keep in sync.
_STOPWORDS: Set[str] = {
    # Seniority + articles + Roman numerals
    "senior", "staff", "principal", "lead", "head", "of", "and", "the", "a", "an",
    "in", "at", "for", "i", "ii", "iii",
    # Role-noun vocabulary present in every title regardless of fit.
    "data", "scientist", "engineer", "engineering", "ml", "machine", "learning",
    "applied", "research", "researcher", "analytics", "analyst", "intelligence",
    "ai", "artificial", "sr", "jr", "junior", "intern", "remote", "hybrid",
    "onsite", "contract", "fulltime", "full-time",
}

# Status genre sets — MUST stay in sync with `src/job_finder/persistence.py`
# constants of the same name. Duplicated here to keep this script stdlib-only
# (no project imports). Single source of truth within this file.
POSITIVE_GENRE_STATUSES: frozenset = frozenset({"Applied", "InProgress", "Closed", "Won"})
NEGATIVE_GENRE_STATUSES: frozenset = frozenset({"NotForMe"})

AUDIT_VERSION = "1.0"
_OBSERVABILITY_NOTE = (
    "These metrics are reported, NOT used as auto-tuning gradients. "
    "Optimizing negative_followup_avoidance_rate too hard narrows discovery "
    "(reward-hacking). They exist for human and /improve LLM-stage reasoning only."
)

# Project layout — script is canonical entrypoint, run from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAIN_DB = _REPO_ROOT / "data" / "sovereign_agent.db"
_HISTORY_DB = _REPO_ROOT / "data" / "history" / "jobs_history.db"


# ---------------------------------------------------------------------------
# Token helpers (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _title_tokens(title: Optional[str]) -> Set[str]:
    if not title:
        return set()
    out: Set[str] = set()
    for raw in title.lower().split():
        tok = raw.strip("(),/-:;|.\"'")
        if tok and len(tok) > 2 and tok not in _STOPWORDS:
            out.add(tok)
    return out


def _company_key(company: Optional[str]) -> str:
    return (company or "").strip().lower()


# ---------------------------------------------------------------------------
# Snapshot loading (handles malformed payloads gracefully)
# ---------------------------------------------------------------------------

def _load_snapshots(
    history_db: Path, limit: int, anomalies: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Returns up to `limit` most-recent snapshots, newest first.

    Each snapshot: {"id": int, "created_at": str, "jobs": List[dict]}.
    Malformed JSON payloads are recorded in `anomalies` and skipped.
    """
    conn = sqlite3.connect(str(history_db))
    try:
        # Pull more than `limit` raw rows so we can skip malformed ones and still
        # return up to `limit` valid snapshots when possible.
        raw = conn.execute(
            "SELECT id, created_at, payload FROM snapshots ORDER BY id DESC LIMIT ?",
            (limit * 2,),
        ).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for sid, created_at, payload in raw:
        if len(out) >= limit:
            break
        try:
            jobs = json.loads(payload)
            if not isinstance(jobs, list):
                raise ValueError(f"payload root is {type(jobs).__name__}, expected list")
        except (json.JSONDecodeError, ValueError) as e:
            anomalies.append({
                "snapshot_id": sid,
                "created_at": created_at,
                "error_type": "malformed_payload",
                "detail": str(e)[:200],
            })
            continue
        out.append({"id": sid, "created_at": created_at, "jobs": jobs})
    return out


# ---------------------------------------------------------------------------
# Matching logic (deterministic)
# ---------------------------------------------------------------------------

def _job_matches_anchor(
    candidate: Dict[str, Any],
    anchor_company: str,
    anchor_theme: Optional[str],
    anchor_tokens: Set[str],
    overlap_threshold: int,
) -> bool:
    """A candidate job matches an anchor if at least one of three rules fires."""
    cand_company = _company_key(candidate.get("company"))
    if anchor_company and cand_company == anchor_company:
        return True
    cand_theme = candidate.get("theme")
    if anchor_theme and cand_theme and cand_theme == anchor_theme:
        return True
    if anchor_tokens:
        cand_tokens = _title_tokens(candidate.get("title"))
        if len(anchor_tokens & cand_tokens) >= overlap_threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# Metric 1 / Metric 2 — followup rate against most-recent snapshot's NEW jobs
# ---------------------------------------------------------------------------

def _compute_followup(
    anchor_rows: List[Tuple[str, str, Optional[str], Optional[str]]],
    new_jobs: List[Dict[str, Any]],
    overlap_threshold: int,
    sample_limit: int,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Returns (count_with_followup, samples). Each anchor counted once."""
    samples: List[Dict[str, Any]] = []
    count_with = 0
    for anchor_id, anchor_company, anchor_title, anchor_theme in anchor_rows:
        ck = _company_key(anchor_company)
        tk = _title_tokens(anchor_title)
        matched: List[str] = []
        for cand in new_jobs:
            cand_id = cand.get("id")
            if not cand_id:
                continue
            if _job_matches_anchor(cand, ck, anchor_theme, tk, overlap_threshold):
                matched.append(cand_id)
        if matched:
            count_with += 1
            if len(samples) < sample_limit:
                samples.append({
                    # Generic key the caller renames per-metric.
                    "anchor_job_id": anchor_id,
                    "matched_new_job_ids": matched,
                })
    return count_with, samples


def _query_anchor_rows(
    main_db: Path, where_clause: str, params: Tuple[Any, ...] = ()
) -> List[Tuple[str, str, Optional[str], Optional[str]]]:
    conn = sqlite3.connect(str(main_db))
    try:
        rows = conn.execute(
            f"SELECT id, company, title, theme FROM jobs WHERE {where_clause}",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


# ---------------------------------------------------------------------------
# Metric 3 — user reaction matrix (compares cur vs prior snapshot)
# ---------------------------------------------------------------------------

def _norm_weight(w: Any) -> int:
    """Snapshots may store weight as None when default; treat None as 50."""
    if w is None:
        return 50
    try:
        return int(w)
    except (TypeError, ValueError):
        return 50


def _norm_feedback(f: Any) -> Optional[str]:
    if f is None:
        return None
    s = str(f).strip().lower()
    return s or None


def _user_signal_present(weight: int, feedback: Optional[str]) -> bool:
    return feedback is not None or weight != 50


def _reaction_changed(
    prior_w: int, prior_f: Optional[str], cur_w: int, cur_f: Optional[str]
) -> bool:
    return (prior_w != cur_w) or (prior_f != cur_f)


def _compute_reaction_matrix(
    cur_snapshot: Dict[str, Any],
    prior_snapshot: Dict[str, Any],
    sample_limit: int,
) -> Dict[str, Any]:
    prior_by_id: Dict[str, Dict[str, Any]] = {
        j["id"]: j for j in prior_snapshot["jobs"] if isinstance(j, dict) and j.get("id")
    }
    # Disentanglement: see module-level POSITIVE_GENRE_STATUSES / NEGATIVE_GENRE_STATUSES
    # (mirrored from persistence.py to keep this script stdlib-only). A row whose
    # status is in POSITIVE_GENRE_STATUSES was acted on by the user — applying is the
    # strongest positive intent signal — so even a subsequent low weight is "stop
    # showing me THIS row", not "system overscored." Exclude these from
    # MISCALIBRATED_HIGH; bucket them in the APPLIED_TRACK counters so the lifecycle
    # signal is still visible but distinct from scoring drift.
    counters = {
        "CORRECT_HIGH": 0,
        "MISCALIBRATED_HIGH": 0,
        "MISSED_HIGH": 0,
        "NO_SIGNAL": 0,
        # Application-lifecycle buckets — informational, NOT scoring-drift signal.
        "APPLIED_OPEN": 0,       # status='Applied' or 'InProgress'
        "APPLIED_CLOSED": 0,     # status='Closed' (applied, didn't go through)
        "APPLIED_WON": 0,        # status='Won' (offer)
        "EXPLICIT_NOTFORME": 0,  # status='NotForMe' (user-rejected without applying)
    }
    miscalibrated_samples: List[Dict[str, Any]] = []
    applied_closed_samples: List[Dict[str, Any]] = []
    total = 0
    for cur_job in cur_snapshot["jobs"]:
        if not isinstance(cur_job, dict):
            continue
        jid = cur_job.get("id")
        if not jid or jid not in prior_by_id:
            continue
        prior_job = prior_by_id[jid]
        total += 1
        cur_w = _norm_weight(cur_job.get("user_weight"))
        cur_f = _norm_feedback(cur_job.get("user_feedback"))
        prior_w = _norm_weight(prior_job.get("user_weight"))
        prior_f = _norm_feedback(prior_job.get("user_feedback"))
        cur_status = (cur_job.get("status") or "New").strip()
        sys_score = cur_job.get("score")
        try:
            sys_score_int = int(sys_score) if sys_score is not None else None
        except (TypeError, ValueError):
            sys_score_int = None

        # Lifecycle short-circuit: a terminal-status row is NOT a scoring-drift datum.
        # Bucket it for visibility and move on.
        if cur_status == "Applied" or cur_status == "InProgress":
            counters["APPLIED_OPEN"] += 1
            continue
        if cur_status == "Closed":
            counters["APPLIED_CLOSED"] += 1
            if len(applied_closed_samples) < sample_limit:
                applied_closed_samples.append({
                    "job_id": jid,
                    "company": cur_job.get("company"),
                    "title": cur_job.get("title"),
                    "system_score": sys_score_int,
                    "user_weight": cur_w,
                    "status": cur_status,
                })
            continue
        if cur_status == "Won":
            counters["APPLIED_WON"] += 1
            continue
        if cur_status == "NotForMe":
            counters["EXPLICIT_NOTFORME"] += 1
            continue

        # Beyond this point: cur_status == 'New' (or any non-terminal). Scoring-drift logic applies.
        # NO_SIGNAL = user did not move the dials at all and they are at defaults.
        no_user_signal_now = not _user_signal_present(cur_w, cur_f)
        no_change_since_prior = not _reaction_changed(prior_w, prior_f, cur_w, cur_f)
        if no_user_signal_now and no_change_since_prior:
            counters["NO_SIGNAL"] += 1
            continue

        positive_reaction = (cur_w >= 70) or (cur_f == "good")
        negative_reaction = (cur_w <= 30) or (cur_f == "bad")
        high_score = sys_score_int is not None and sys_score_int >= 85

        if high_score and positive_reaction:
            counters["CORRECT_HIGH"] += 1
        elif high_score and negative_reaction:
            counters["MISCALIBRATED_HIGH"] += 1
            if len(miscalibrated_samples) < sample_limit:
                miscalibrated_samples.append({
                    "job_id": jid,
                    "company": cur_job.get("company"),
                    "title": cur_job.get("title"),
                    "system_score": sys_score_int,
                    "user_weight": cur_w,
                    "user_feedback": cur_f,
                    "status": cur_status,
                })
        elif (not high_score) and positive_reaction:
            counters["MISSED_HIGH"] += 1
        else:
            counters["NO_SIGNAL"] += 1

    return {
        **counters,
        "total_compared": total,
        "miscalibrated_examples": miscalibrated_samples,
        "applied_closed_examples": applied_closed_samples,
        "_disentanglement_note": (
            "MISCALIBRATED_HIGH excludes rows with status in {Applied,InProgress,Closed,Won,NotForMe}. "
            "Those are bucketed in APPLIED_OPEN/APPLIED_CLOSED/APPLIED_WON/EXPLICIT_NOTFORME. "
            "A high-scored Closed row is not scoring drift — the user applied; the system was right "
            "to surface; the outcome was external. Treat APPLIED_CLOSED as informational (life-event), "
            "NOT as a SCORING_DRIFT_DETECTED trigger."
        ),
    }


# ---------------------------------------------------------------------------
# Bonus — UI engagement rate across recent N snapshots
# ---------------------------------------------------------------------------

def _compute_ui_engagement(snapshots: List[Dict[str, Any]]) -> Optional[float]:
    """Across the last N snapshots, what fraction of newly-surfaced jobs (jobs
    appearing for the first time in that snapshot vs the snapshot before it)
    got any user signal in that same snapshot's payload?

    Requires ≥ 2 snapshots to detect "newly surfaced"; returns None otherwise.
    """
    if len(snapshots) < 2:
        return None
    # snapshots are newest-first; pair each with the next (older) one.
    new_total = 0
    new_with_signal = 0
    for i in range(len(snapshots) - 1):
        newer = snapshots[i]["jobs"]
        older_ids = {
            j["id"] for j in snapshots[i + 1]["jobs"]
            if isinstance(j, dict) and j.get("id")
        }
        for j in newer:
            if not isinstance(j, dict):
                continue
            jid = j.get("id")
            if not jid or jid in older_ids:
                continue
            new_total += 1
            w = _norm_weight(j.get("user_weight"))
            f = _norm_feedback(j.get("user_feedback"))
            if _user_signal_present(w, f):
                new_with_signal += 1
    if new_total == 0:
        return None
    return round(new_with_signal / new_total, 4)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _truncate_samples(metric: Dict[str, Any], verbose: bool, default_n: int = 5) -> None:
    if verbose:
        return
    if "samples" in metric and isinstance(metric["samples"], list):
        metric["samples"] = metric["samples"][:default_n]
    if "miscalibrated_examples" in metric and isinstance(metric["miscalibrated_examples"], list):
        metric["miscalibrated_examples"] = metric["miscalibrated_examples"][:default_n]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-run feedback efficacy audit (read-only).",
    )
    parser.add_argument(
        "--lookback-snapshots", type=int, default=5,
        help="How many snapshots to load (default 5). The reaction matrix always "
             "uses the two newest; older ones feed ui_engagement_rate.",
    )
    parser.add_argument(
        "--title-overlap-threshold", type=int, default=3,
        help="Min distinct non-stopword title tokens shared for a title-match "
             "(default 3). Arbitrary heuristic — tune per signal-to-noise taste.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Include full sample arrays (default truncated to 5 per category).",
    )
    args = parser.parse_args(argv)

    if args.lookback_snapshots < 2:
        # Reaction matrix needs at least 2; clamp silently to the minimum needed.
        args.lookback_snapshots = 2

    anomalies: List[Dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- Snapshot availability check (clean failure mode) ---
    if not _HISTORY_DB.exists():
        print(json.dumps({
            "audit_version": AUDIT_VERSION,
            "computed_at": now_iso,
            "audit_failed": True,
            "reason": "history_db_missing",
            "history_db_path": str(_HISTORY_DB),
            "_observability_note": _OBSERVABILITY_NOTE,
        }, indent=2))
        return 0

    snapshots = _load_snapshots(_HISTORY_DB, args.lookback_snapshots, anomalies)
    if len(snapshots) < 2:
        print(json.dumps({
            "audit_version": AUDIT_VERSION,
            "computed_at": now_iso,
            "audit_failed": True,
            "reason": "insufficient_snapshots",
            "snapshot_count": len(snapshots),
            "_anomalies": anomalies,
            "_observability_note": _OBSERVABILITY_NOTE,
        }, indent=2))
        return 0

    cur_snap, prior_snap = snapshots[0], snapshots[1]
    prior_ids: Set[str] = {
        j["id"] for j in prior_snap["jobs"]
        if isinstance(j, dict) and j.get("id")
    }
    new_jobs_in_cur: List[Dict[str, Any]] = [
        j for j in cur_snap["jobs"]
        if isinstance(j, dict) and j.get("id") and j["id"] not in prior_ids
    ]

    if not _MAIN_DB.exists():
        print(json.dumps({
            "audit_version": AUDIT_VERSION,
            "computed_at": now_iso,
            "audit_failed": True,
            "reason": "main_db_missing",
            "main_db_path": str(_MAIN_DB),
            "_observability_note": _OBSERVABILITY_NOTE,
        }, indent=2))
        return 0

    # --- Anchor sets from the main DB (current state of user feedback) ---
    # Disentanglement: applied/closed/won rows belong in positive anchors regardless
    # of weight (applied-intent dominates). negative anchors EXCLUDE positive-genre
    # statuses — a low weight on a Closed row means 'stop showing me this row', not
    # 'avoid this genre'.
    pos_statuses = tuple(sorted(POSITIVE_GENRE_STATUSES))
    pos_ph = ",".join("?" * len(pos_statuses))
    neg_statuses = tuple(sorted(NEGATIVE_GENRE_STATUSES))
    # Negative anchor uses NEGATIVE_GENRE_STATUSES for the explicit-rejection arm
    # AND POSITIVE_GENRE_STATUSES (negated) for the genre-exclusion arm.
    positive_anchors = _query_anchor_rows(
        _MAIN_DB,
        "user_weight >= 70 OR user_feedback = 'good' "
        f"OR status IN ({pos_ph})",
        pos_statuses,
    )
    neg_in_ph = ",".join("?" * len(neg_statuses))
    negative_anchors = _query_anchor_rows(
        _MAIN_DB,
        f"(user_feedback = 'bad' OR user_weight <= 30 OR status IN ({neg_in_ph})) "
        f"AND status NOT IN ({pos_ph})",
        neg_statuses + pos_statuses,
    )

    conn = sqlite3.connect(str(_MAIN_DB))
    try:
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()

    # --- Metric 1: positive followup rate ---
    sample_cap = max(5, len(positive_anchors)) if args.verbose else 5
    if positive_anchors:
        pos_count, pos_samples = _compute_followup(
            positive_anchors, new_jobs_in_cur,
            args.title_overlap_threshold, sample_cap,
        )
        # Verify sample ids are real members of cur snapshot's payload (guardrail).
        cur_ids_set = {j.get("id") for j in cur_snap["jobs"] if isinstance(j, dict)}
        for s in pos_samples:
            s["matched_new_job_ids"] = [
                mid for mid in s["matched_new_job_ids"] if mid in cur_ids_set
            ]
        # Rename anchor_job_id -> positive_job_id per spec.
        pos_samples_out = [
            {"positive_job_id": s["anchor_job_id"], "matched_new_job_ids": s["matched_new_job_ids"]}
            for s in pos_samples
        ]
        positive_metric: Dict[str, Any] = {
            "positive_set_size": len(positive_anchors),
            "positive_set_with_followup": pos_count,
            "rate": round(pos_count / len(positive_anchors), 4),
            "samples": pos_samples_out,
        }
    else:
        positive_metric = {
            "positive_set_size": 0,
            "positive_set_with_followup": 0,
            "rate": None,
            "samples": [],
        }
    _truncate_samples(positive_metric, args.verbose)

    # --- Metric 2: negative followup avoidance rate ---
    sample_cap_n = max(5, len(negative_anchors)) if args.verbose else 5
    if negative_anchors:
        neg_count, neg_samples = _compute_followup(
            negative_anchors, new_jobs_in_cur,
            args.title_overlap_threshold, sample_cap_n,
        )
        cur_ids_set = {j.get("id") for j in cur_snap["jobs"] if isinstance(j, dict)}
        for s in neg_samples:
            s["matched_new_job_ids"] = [
                mid for mid in s["matched_new_job_ids"] if mid in cur_ids_set
            ]
        neg_samples_out = [
            {"negative_job_id": s["anchor_job_id"], "matched_new_job_ids": s["matched_new_job_ids"]}
            for s in neg_samples
        ]
        avoidance = round(1.0 - (neg_count / len(negative_anchors)), 4)
        negative_metric: Dict[str, Any] = {
            "negative_set_size": len(negative_anchors),
            "negative_set_with_new_matches": neg_count,
            "avoidance_rate": avoidance,
            "samples": neg_samples_out,
        }
    else:
        negative_metric = {
            "negative_set_size": 0,
            "negative_set_with_new_matches": 0,
            "avoidance_rate": None,
            "samples": [],
        }
    _truncate_samples(negative_metric, args.verbose)

    # --- Metric 3: user reaction matrix ---
    reaction_sample_cap = 50 if args.verbose else 5
    reaction = _compute_reaction_matrix(cur_snap, prior_snap, reaction_sample_cap)
    _truncate_samples(reaction, args.verbose)

    # --- Bonus: UI engagement rate across loaded snapshots ---
    ui_engagement = _compute_ui_engagement(snapshots)

    out: Dict[str, Any] = {
        "audit_version": AUDIT_VERSION,
        "computed_at": now_iso,
        "snapshots_compared": {
            "current_snapshot_id": cur_snap["id"],
            "current_snapshot_at": cur_snap["created_at"],
            "prior_snapshot_id": prior_snap["id"],
            "prior_snapshot_at": prior_snap["created_at"],
            "lookback_snapshots_loaded": len(snapshots),
            "new_jobs_in_current_snapshot": len(new_jobs_in_cur),
        },
        "current_db": {
            "total_jobs": total_jobs,
            "positive_set_size": len(positive_anchors),
            "negative_set_size": len(negative_anchors),
        },
        "config": {
            "title_overlap_threshold": args.title_overlap_threshold,
            "lookback_snapshots_requested": args.lookback_snapshots,
            "verbose": args.verbose,
        },
        "positive_followup_rate": positive_metric,
        "negative_followup_avoidance_rate": negative_metric,
        "user_reaction_matrix": reaction,
        "ui_engagement_rate": ui_engagement,
    }
    if anomalies:
        out["_anomalies"] = anomalies
    out["_observability_note"] = _OBSERVABILITY_NOTE

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
