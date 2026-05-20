"""
Load and save the candidate profile (data/candidate_info.json).
Snapshots append to data/history/candidate_history.db on each save and when the file changes on disk
outside save_config (e.g. manual JSON edit) — detected on the next load_config.
"""
import json
import os
from typing import Any, Optional

from job_finder.candidate_disk_sync import (
    maybe_record_snapshot_on_external_edit,
    sync_fingerprint_from_disk,
)
from job_finder.paths import resolve_active_config_path


def _to_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    if isinstance(x, str):
        return x
    return str(x)


def _to_list_of_str(x: Any) -> list:
    """
    Normalize list-ish fields for UI and downstream prompt building.
    Keeps the original semantics when x is already a list; otherwise, tries best-effort parsing.
    """
    if x is None:
        return []
    if isinstance(x, list):
        return [str(i).strip() for i in x if i is not None and str(i).strip()]
    if isinstance(x, str):
        parts = [p.strip() for p in x.split(",")]
        return [p for p in parts if p]
    return [str(x).strip()]


def _normalize_excluded_simple(x: Any) -> list:
    items = _to_list_of_str(x)
    out = []
    for s in items:
        s2 = s.strip().lower()
        if s2:
            out.append(s2)
    seen: set = set()
    return [v for v in out if not (v in seen or seen.add(v))]


def _normalize_excluded_pairs(x: Any) -> list:
    items = _to_list_of_str(x)
    out = []
    seen: set = set()
    for raw in items:
        if ":" not in raw:
            continue
        company, _, area = raw.partition(":")
        company = company.strip().lower()
        area = area.strip().lower()
        if not company or not area:
            continue
        norm = f"{company}:{area}"
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _normalize_config_shape(config: dict) -> dict:
    """
    Best-effort normalization that preserves existing values.
    This makes the project robust when users paste partial/legacy JSON.
    """
    defaults = empty_template()
    out = dict(defaults)
    out.update(config or {})

    # Normalize known fields only; keep all other unknown/legacy keys intact.
    out["core_identity"] = _to_str(out.get("core_identity", ""), "")
    out["scientific_moat"] = _to_list_of_str(out.get("scientific_moat", []))
    out["engineering_stack"] = _to_list_of_str(out.get("engineering_stack", []))
    out["target_seniority"] = _to_str(out.get("target_seniority", ""), "")
    out["target_country"] = _to_str(out.get("target_country", "USA"), "USA")
    out["priority_domains"] = _to_list_of_str(out.get("priority_domains", []))
    out["golden_keywords"] = _to_str(out.get("golden_keywords", ""), "")
    out["search_targets"] = _to_list_of_str(out.get("search_targets", []))
    out["noise_keywords"] = _to_list_of_str(out.get("noise_keywords", []))
    out["wisdom"] = _to_str(out.get("wisdom", ""), "")
    out["peer_companies"] = _to_list_of_str(out.get("peer_companies", []))
    out["excluded_companies"] = _normalize_excluded_simple(out.get("excluded_companies", []))
    out["excluded_areas"] = _normalize_excluded_simple(out.get("excluded_areas", []))
    out["excluded_pairs"] = _normalize_excluded_pairs(out.get("excluded_pairs", []))

    # Synthesized revealed-preference patterns (Item G).
    # Each is List[dict]; preserved verbatim if already a list, defaulted to [] otherwise.
    # Soft-bias signals only — never a hard discovery filter (see Discovery non-degradation rule).
    for _k in ("inclinations", "disinclinations", "learn_skills"):
        _v = out.get(_k, [])
        out[_k] = _v if isinstance(_v, list) else []

    # Legacy/alias keys sometimes used by the app's `_profile()` normalization.
    if "priority_industries" in out:
        out["priority_industries"] = _to_list_of_str(out.get("priority_industries", []))
    if "noise_filters" in out:
        out["noise_filters"] = _to_list_of_str(out.get("noise_filters", []))
    if "technical_moat" in out:
        out["technical_moat"] = _to_list_of_str(out.get("technical_moat", []))
    if "candidate_profile" in out:
        cp = out.get("candidate_profile") or {}
        if isinstance(cp, dict):
            # Avoid changing nested legacy semantics too much; normalize top-level only.
            out["candidate_profile"] = cp
    if "search_parameters" in out:
        sp = out.get("search_parameters") or {}
        if isinstance(sp, dict):
            out["search_parameters"] = sp

    return out


def load_config(path: Optional[str] = None) -> dict:
    """
    Read the profile from disk. Returns a dict with all keys.
    If the file does not exist, returns an empty dict.
    Uses candidate_info.json (see paths.resolve_active_config_path).

    If the file bytes changed since the last save_config (or since first-seen baseline), appends
    a candidate_history snapshot (manual editor parity with Streamlit saves).
    """
    config_path = path or resolve_active_config_path()
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    try:
        maybe_record_snapshot_on_external_edit(config_path, data)
    except Exception:
        pass
    return _normalize_config_shape(data)


def save_config(config: dict, path: Optional[str] = None, record_snapshot: bool = True) -> None:
    """
    Write the profile to disk. All keys are preserved (indent=2).
    When record_snapshot is True, appends to data/history/candidate_history.db (best-effort).
    Always refreshes the on-disk fingerprint so load_config does not double-snapshot the same write.
    """
    config_path = path or resolve_active_config_path()
    os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
    existing: dict = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as _f:
                existing = json.load(_f) or {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = {**existing, **(config or {})}
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    if record_snapshot:
        try:
            from job_finder.history import record_candidate_snapshot

            record_candidate_snapshot(config)
        except Exception:
            pass
    try:
        sync_fingerprint_from_disk(config_path)
    except Exception:
        pass


def empty_template() -> dict:
    """
    Blank profile used on reset or first orchestrator run.
    Same keys as the real profile, just empty values.
    """
    return {
        "core_identity": "",
        "scientific_moat": [],
        "engineering_stack": [],
        "target_seniority": "",
        "target_country": "USA",
        "auto_improve_audit_enabled": False,
        "priority_domains": [],
        "golden_keywords": "",
        "search_targets": [],
        "noise_keywords": ["Junior", "Intern", "Contract"],
        "wisdom": "",
        "peer_companies": [],
        # Item G — revealed-preference patterns synthesized from user_feedback/user_weight.
        # See scripts/synthesize_feedback_patterns.py. Used as +5/-5/+3 soft bias in scoring.
        "inclinations": [],
        "disinclinations": [],
        "learn_skills": [],
        "excluded_companies": [],
        "excluded_areas": [],
        "excluded_pairs": [],
    }


def get_exclusions(config: dict) -> tuple:
    """Return (companies, areas, pairs) normalized for filter-time use.

    Re-normalizes at the call site so a hand-edited JSON between load and use
    cannot inject empty entries that would substring-match every theme.
    """
    companies = _normalize_excluded_simple(config.get("excluded_companies", []))
    areas = _normalize_excluded_simple(config.get("excluded_areas", []))
    pairs_raw = _normalize_excluded_pairs(config.get("excluded_pairs", []))
    pairs = [tuple(p.split(":", 1)) for p in pairs_raw]
    return companies, areas, pairs
