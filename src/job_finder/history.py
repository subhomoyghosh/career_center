"""
Append-only snapshot history for candidate profile, jobs table, and market intelligence.
Separate SQLite DBs under data/history/ for safe revert and programmatic diff.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from job_finder.paths import (
    get_candidate_history_db_path,
    get_db_path,
    get_history_dir,
    get_intelligence_history_db_path,
    get_jobs_history_db_path,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_dir() -> None:
    os.makedirs(get_history_dir(), exist_ok=True)


def _init_candidate_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )"""
    )


def _init_jobs_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )"""
    )


def _init_intel_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            wisdom TEXT NOT NULL,
            aspects_json TEXT NOT NULL DEFAULT '[]'
        )"""
    )


def record_candidate_snapshot(config: Dict[str, Any]) -> Optional[int]:
    """Store full candidate JSON. Returns snapshot id or None on failure."""
    try:
        _ensure_dir()
        path = get_candidate_history_db_path()
        conn = sqlite3.connect(path)
        _init_candidate_db(conn)
        cur = conn.execute(
            "INSERT INTO snapshots (created_at, payload) VALUES (?, ?)",
            (_utc_now_iso(), json.dumps(config, ensure_ascii=False)),
        )
        sid = cur.lastrowid
        conn.commit()
        conn.close()
        return int(sid) if sid is not None else None
    except OSError:
        return None


def record_jobs_snapshot(jobs: List[Dict[str, Any]]) -> Optional[int]:
    """Store full jobs list as JSON (one row per snapshot)."""
    try:
        _ensure_dir()
        path = get_jobs_history_db_path()
        conn = sqlite3.connect(path)
        _init_jobs_db(conn)
        cur = conn.execute(
            "INSERT INTO snapshots (created_at, payload) VALUES (?, ?)",
            (_utc_now_iso(), json.dumps(jobs, ensure_ascii=False)),
        )
        sid = cur.lastrowid
        conn.commit()
        conn.close()
        return int(sid) if sid is not None else None
    except OSError:
        return None


def record_jobs_snapshot_from_db(db_path: Optional[str] = None) -> Optional[int]:
    """Read current jobs table and snapshot."""
    path = db_path or get_db_path()
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM jobs ORDER BY score DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return record_jobs_snapshot(rows)


def record_intelligence_snapshot(wisdom: str, aspects_json: str = "[]") -> Optional[int]:
    """Store wisdom string and optional aspects JSON (e.g. parsed table)."""
    try:
        _ensure_dir()
        p = get_intelligence_history_db_path()
        conn = sqlite3.connect(p)
        _init_intel_db(conn)
        cur = conn.execute(
            "INSERT INTO snapshots (created_at, wisdom, aspects_json) VALUES (?, ?, ?)",
            (_utc_now_iso(), wisdom or "", aspects_json or "[]"),
        )
        sid = cur.lastrowid
        conn.commit()
        conn.close()
        return int(sid) if sid is not None else None
    except OSError:
        return None


def list_snapshots(kind: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    kind: 'candidate' | 'jobs' | 'intelligence'.
    Returns rows with id, created_at, and preview (no full payload in list for size).
    """
    _ensure_dir()
    if kind == "candidate":
        db_path = get_candidate_history_db_path()
        if not os.path.isfile(db_path):
            return []
        conn = sqlite3.connect(db_path)
        _init_candidate_db(conn)
        cur = conn.execute(
            "SELECT id, created_at, substr(payload, 1, 120) AS preview FROM snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [{"id": r[0], "created_at": r[1], "preview": r[2]} for r in cur.fetchall()]
        conn.close()
        return rows
    if kind == "jobs":
        db_path = get_jobs_history_db_path()
        if not os.path.isfile(db_path):
            return []
        conn = sqlite3.connect(db_path)
        _init_jobs_db(conn)
        cur = conn.execute(
            "SELECT id, created_at, length(payload) AS bytes FROM snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [{"id": r[0], "created_at": r[1], "payload_bytes": r[2]} for r in cur.fetchall()]
        conn.close()
        return rows
    if kind == "intelligence":
        db_path = get_intelligence_history_db_path()
        if not os.path.isfile(db_path):
            return []
        conn = sqlite3.connect(db_path)
        _init_intel_db(conn)
        cur = conn.execute(
            "SELECT id, created_at, substr(wisdom, 1, 120) AS preview FROM snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [{"id": r[0], "created_at": r[1], "preview": r[2]} for r in cur.fetchall()]
        conn.close()
        return rows
    return []


def get_candidate_snapshot(snapshot_id: int) -> Optional[Dict[str, Any]]:
    db_path = get_candidate_history_db_path()
    if not os.path.isfile(db_path):
        return None
    conn = sqlite3.connect(db_path)
    _init_candidate_db(conn)
    cur = conn.execute("SELECT payload FROM snapshots WHERE id = ?", (snapshot_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0])


def get_jobs_snapshot(snapshot_id: int) -> Optional[List[Dict[str, Any]]]:
    db_path = get_jobs_history_db_path()
    if not os.path.isfile(db_path):
        return None
    conn = sqlite3.connect(db_path)
    _init_jobs_db(conn)
    cur = conn.execute("SELECT payload FROM snapshots WHERE id = ?", (snapshot_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0])


def clear_history_directory() -> None:
    """Remove all history DB files and profile disk fingerprint (used by reset)."""
    h = get_history_dir()
    if not os.path.isdir(h):
        return
    for name in (
        "candidate_history.db",
        "jobs_history.db",
        "intelligence_history.db",
        ".candidate_profile_fingerprint.json",
    ):
        p = os.path.join(h, name)
        if os.path.isfile(p):
            os.remove(p)
