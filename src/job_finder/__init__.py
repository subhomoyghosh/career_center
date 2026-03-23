"""
Job Finder — shared logic for config, jobs DB, and wisdom.
Use these so the app and scripts stay simple.
"""
from job_finder.config import load_config, save_config, empty_template
from job_finder.paths import get_config_path, get_db_path, get_data_dir, resolve_active_config_path
from job_finder.link_validation import filter_valid_job_links
from job_finder.persistence import (
    persist_jobs,
    create_jobs_table,
    update_jobs_feedback_batch,
    ensure_feedback_columns,
    get_high_signal_jobs,
)
from job_finder.wisdom import update_wisdom
from job_finder.wisdom_intel import wisdom_text_to_intelligence_rows
from job_finder.judge_context import build_judge_report

__all__ = [
    "filter_valid_job_links",
    "load_config",
    "save_config",
    "empty_template",
    "get_config_path",
    "get_db_path",
    "get_data_dir",
    "resolve_active_config_path",
    "persist_jobs",
    "create_jobs_table",
    "update_jobs_feedback_batch",
    "ensure_feedback_columns",
    "get_high_signal_jobs",
    "update_wisdom",
    "wisdom_text_to_intelligence_rows",
    "build_judge_report",
]
