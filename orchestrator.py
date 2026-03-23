import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.config import empty_template, save_config
from job_finder.paths import (
    get_candidate_info_path,
    get_db_path,
)
from job_finder.persistence import create_jobs_table


def setup_sovereign_system():
    """Create data dirs, empty config template, and DB only if they do not exist. Safe to run multiple times."""
    # Create directories using absolute paths so cwd doesn't matter.
    from job_finder.paths import get_data_dir

    project_root = os.path.abspath(os.path.dirname(__file__))
    # repo root = .../job_finder
    project_root = os.path.dirname(project_root)
    cursor_rules_dir = os.path.join(project_root, ".cursor", "rules")

    for folder in (get_data_dir(), cursor_rules_dir):
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)

    c_path = get_candidate_info_path()
    if not os.path.exists(c_path):
        save_config(empty_template(), c_path)
        print("Created data/candidate_info.json (empty template).")
    elif os.path.isfile(c_path):
        print("data/candidate_info.json already exists; left unchanged.")

    create_jobs_table(get_db_path())
    print("Database ready: data/sovereign_agent.db (jobs table).")
    print("Run again anytime; existing config and data are not overwritten.")


if __name__ == "__main__":
    setup_sovereign_system()
