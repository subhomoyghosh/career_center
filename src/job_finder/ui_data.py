import os
import sqlite3
from typing import Any, Dict, Tuple

import pandas as pd
import streamlit as st

from job_finder.config import load_config
from job_finder.persistence import (
    ensure_feedback_columns,
    migrate_jobs_table_drop_legacy_posting_columns,
)
from job_finder.paths import get_db_path, resolve_active_config_path

from job_finder.ui_helpers import source_from_link


def _path_mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _db_fingerprint(db_path: str) -> Tuple[float, int]:
    try:
        st_res = os.stat(db_path)
        return (st_res.st_mtime, st_res.st_size)
    except OSError:
        return (0.0, 0)


def _data_signature() -> Tuple[str, str, Tuple[float, int], float]:
    db_path = get_db_path()
    cfg_path = resolve_active_config_path()
    return (db_path, cfg_path, _db_fingerprint(db_path), _path_mtime(cfg_path))


@st.cache_resource(show_spinner=False)
def _migrate_once(db_path: str, _db_fp: Tuple[float, int]) -> bool:
    if not os.path.exists(db_path):
        return False
    conn = sqlite3.connect(db_path)
    try:
        ensure_feedback_columns(conn)
        migrate_jobs_table_drop_legacy_posting_columns(conn)
    finally:
        conn.close()
    return True


@st.cache_data(show_spinner=False)
def _load_data_cached(sig: Tuple[str, str, Tuple[float, int], float]) -> Tuple[Dict[str, Any], pd.DataFrame]:
    db_path, _cfg_path, db_fp, _cfg_mtime = sig
    config = load_config()
    if not config:
        return {}, pd.DataFrame()
    if not os.path.exists(db_path):
        return config, pd.DataFrame()

    _migrate_once(db_path, db_fp)

    conn = sqlite3.connect(db_path)
    try:
        jobs = pd.read_sql_query("SELECT * FROM jobs ORDER BY score DESC", conn)
    finally:
        conn.close()

    if not jobs.empty:
        from job_finder.exclusions import apply_exclusions
        records, _ = apply_exclusions(jobs.to_dict("records"), config)
        jobs = pd.DataFrame(records, columns=jobs.columns) if records else pd.DataFrame(columns=jobs.columns)

    if not jobs.empty:
        if "link" in jobs.columns:
            jobs["source"] = jobs["link"].map(source_from_link)
        if "user_feedback" not in jobs.columns:
            jobs["user_feedback"] = None
        if "user_weight" not in jobs.columns:
            jobs["user_weight"] = 50
        if "status" not in jobs.columns:
            jobs["status"] = "New"
        jobs["user_feedback"] = jobs["user_feedback"].map(
            lambda x: "Good" if x == "good" else "Bad" if x == "bad" else "—"
        ).fillna("—")
        jobs["user_weight"] = jobs["user_weight"].fillna(50).astype(int)
        jobs["status"] = jobs["status"].fillna("New").astype(str)

    return config, jobs


def load_data() -> Tuple[Dict[str, Any], pd.DataFrame]:
    return _load_data_cached(_data_signature())


def invalidate_data_cache() -> None:
    """Drop cached jobs/config so the next render reads fresh from disk."""
    _load_data_cached.clear()


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
        "excluded_companies": config.get("excluded_companies") or [],
        "excluded_areas": config.get("excluded_areas") or [],
        "excluded_pairs": config.get("excluded_pairs") or [],
        "auto_improve_audit_enabled": bool(config.get("auto_improve_audit_enabled", False)),
    }
