import os
import sqlite3
from typing import Any, Dict, Tuple

import pandas as pd

from job_finder.config import load_config
from job_finder.persistence import (
    ensure_feedback_columns,
    migrate_jobs_table_drop_legacy_posting_columns,
)
from job_finder.paths import get_db_path

from job_finder.ui_helpers import source_from_link


def load_data() -> Tuple[Dict[str, Any], pd.DataFrame]:
    config = load_config()
    if not config:
        return {}, pd.DataFrame()
    if not os.path.exists(get_db_path()):
        return config, pd.DataFrame()

    conn = sqlite3.connect(get_db_path())
    ensure_feedback_columns(conn)
    migrate_jobs_table_drop_legacy_posting_columns(conn)
    jobs = pd.read_sql_query("SELECT * FROM jobs ORDER BY score DESC", conn)
    conn.close()

    if not jobs.empty:
        jobs = jobs.copy()
        if "link" in jobs.columns:
            jobs["source"] = jobs["link"].map(source_from_link)
        if "user_feedback" not in jobs.columns:
            jobs["user_feedback"] = None
        if "user_weight" not in jobs.columns:
            jobs["user_weight"] = 50
        jobs["user_feedback"] = jobs["user_feedback"].map(
            lambda x: "Good" if x == "good" else "Bad" if x == "bad" else "—"
        ).fillna("—")
        jobs["user_weight"] = jobs["user_weight"].fillna(50).astype(int)

    return config, jobs


def profile_from_config(config: dict) -> dict:
    """Normalize config to a flat profile (flat schema first, then legacy nested)."""
    cp = config.get("candidate_profile") or {}
    sp = config.get("search_parameters") or {}
    return {
        "core_identity": config.get("core_identity")
        or cp.get("core_identity")
        or config.get("identity", ""),
        "golden_keywords": config.get("golden_keywords") or sp.get("search_keywords", ""),
        "scientific_moat": config.get("scientific_moat")
        or cp.get("scientific_moat")
        or config.get("technical_moat", []),
        "engineering_stack": config.get("engineering_stack") or cp.get("engineering_stack", []),
        "target_seniority": config.get("target_seniority")
        or cp.get("target_seniority")
        or config.get("target_level", ""),
        "target_country": config.get("target_country") or sp.get("target_country", "USA"),
        "priority_industries": config.get("priority_industries")
        or sp.get("priority_industries")
        or config.get("priority_domains", []),
        "priority_domains": config.get("priority_domains") or config.get("priority_industries", []),
        "search_targets": config.get("search_targets") or sp.get("search_targets", []),
        "noise_keywords": config.get("noise_keywords")
        or sp.get("noise_filters")
        or config.get("noise_keywords", []),
        "wisdom": config.get("wisdom")
        or config.get("market_wisdom")
        or "Awaiting next /fetchjobs run...",
        "peer_companies": config.get("peer_companies") or sp.get("peer_companies", []),
    }

