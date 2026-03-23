"""
Update only the "wisdom" line in the profile. Everything else (identity, keywords, etc.) stays the same.
Used after a job search to save what the market is saying.
"""
from typing import Optional

from job_finder.config import empty_template, load_config, save_config
from job_finder.paths import resolve_active_config_path


def update_wisdom(new_wisdom: str, config_path: Optional[str] = None) -> None:
    """
    Load the profile, set the "wisdom" field to new_wisdom, and save.
    All other keys are left exactly as they were.
    Records an intelligence snapshot (wisdom) in data/history/intelligence_history.db.
    """
    path = config_path or resolve_active_config_path()
    config = load_config(path)
    if not config:
        # Avoid clobbering the profile with only {"wisdom": ...} when the config is missing/invalid.
        config = empty_template()
    config["wisdom"] = new_wisdom
    save_config(config, path)
    try:
        from job_finder.history import record_intelligence_snapshot

        record_intelligence_snapshot(new_wisdom or "", "[]")
    except Exception:
        pass
