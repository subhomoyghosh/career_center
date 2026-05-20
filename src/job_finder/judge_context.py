"""
Structured context for LLM-as-judge (nudge, listing links, wisdom, intelligence table).
Used by scripts/dump_judge_context.py and scripts/evaluate_nudge_system.py.
"""
import json
import os
import sqlite3
from typing import Any, Dict, List

from job_finder.paths import get_db_path, resolve_active_config_path
from job_finder.persistence import (
    ensure_feedback_columns,
    get_high_signal_jobs,
    migrate_jobs_table_drop_legacy_posting_columns,
)
from job_finder.wisdom_intel import wisdom_text_to_intelligence_rows


def _get_high_signal_jobs(db_path: str, min_weight: int = 70) -> List[dict]:
    """Delegates to persistence.get_high_signal_jobs so judge/UI share lifecycle semantics."""
    if not os.path.exists(db_path):
        return []
    return get_high_signal_jobs(db_path=db_path, min_weight=min_weight)


def _sample_jobs_link_audit(conn: sqlite3.Connection, limit: int = 10) -> List[dict]:
    """Top jobs by score: fields needed to verify listing URLs vs title/rationale."""
    cur = conn.execute(
        """SELECT company, title, link, score,
                  substr(rationale, 1, 200) AS rationale_preview
           FROM jobs ORDER BY score DESC LIMIT ?""",
        (limit,),
    )
    return [
        {
            "company": r[0],
            "title": r[1],
            "link": r[2],
            "score": r[3],
            "rationale_preview": r[4],
        }
        for r in cur.fetchall()
    ]


def _sample_jobs_wisdom_context(conn: sqlite3.Connection, limit: int = 15) -> List[dict]:
    cur = conn.execute(
        """SELECT company, title, theme, score,
                  substr(rationale, 1, 280) AS rationale_preview
           FROM jobs ORDER BY score DESC LIMIT ?""",
        (limit,),
    )
    return [
        {
            "company": r[0],
            "title": r[1],
            "theme": r[2],
            "score": r[3],
            "rationale_preview": r[4],
        }
        for r in cur.fetchall()
    ]


def _load_wisdom_bundle(config_path: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"wisdom_raw": "", "intelligence_rows": [], "config_path": config_path}
    if not os.path.exists(config_path):
        return out
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        w = (config.get("wisdom") or config.get("market_wisdom") or "").strip()
        out["wisdom_raw"] = w
        out["intelligence_rows"] = wisdom_text_to_intelligence_rows(w)
    except (OSError, json.JSONDecodeError):
        pass
    return out


def build_judge_report() -> Dict[str, Any]:
    """
    Full report dict for judge prompts and in-chat analysis.
    """
    config_path = resolve_active_config_path()
    db_path = get_db_path()
    report: Dict[str, Any] = {
        "config_path": config_path,
        "config_exists": os.path.exists(config_path),
        "db_exists": os.path.isfile(db_path),
        "config_has_wisdom": False,
        "high_signal_count": 0,
        "high_signal_jobs": [],
        "schema_ok": False,
        "core_columns_ok": False,
        "jobs_link_audit_sample": [],
        "jobs_wisdom_context": [],
        "wisdom_raw": "",
        "intelligence_rows": [],
        "verdict": "UNKNOWN",
        "message": "",
    }
    wb = _load_wisdom_bundle(config_path)
    report["wisdom_raw"] = wb["wisdom_raw"]
    report["intelligence_rows"] = wb["intelligence_rows"]
    report["config_has_wisdom"] = bool(wb["wisdom_raw"])

    if not report["db_exists"]:
        report["message"] = "No jobs DB; run orchestrator and at least one /fetchjobs."
        report["verdict"] = "FAIL"
        return report

    conn = sqlite3.connect(db_path)
    try:
        ensure_feedback_columns(conn)
        migrate_jobs_table_drop_legacy_posting_columns(conn)
        cur = conn.execute("PRAGMA table_info(jobs)")
        cols = [row[1] for row in cur.fetchall()]
        report["schema_ok"] = "user_feedback" in cols and "user_weight" in cols
        need = ("id", "company", "title", "link", "score", "theme", "rationale")
        report["core_columns_ok"] = all(c in cols for c in need)
        report["jobs_link_audit_sample"] = _sample_jobs_link_audit(conn, 10)
        report["jobs_wisdom_context"] = _sample_jobs_wisdom_context(conn, 15)
    finally:
        conn.close()

    high_signal = _get_high_signal_jobs(db_path, min_weight=70)
    report["high_signal_count"] = len(high_signal)
    report["high_signal_jobs"] = high_signal

    if not report["schema_ok"]:
        report["verdict"] = "FAIL"
        report["message"] = "Jobs table missing user_feedback or user_weight columns."
        return report

    report["verdict"] = "OK"
    report["message"] = (
        f"Nudge system ready: {report['high_signal_count']} high-signal job(s) will be used to bias next /fetchjobs."
    )
    return report
