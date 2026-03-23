"""
Reset the project to a clean state: empty profile template, empty jobs DB, and clear snapshot history.
Does not delete resume PDF(s) in data/. Run from project root.
"""
import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.config import empty_template, save_config
from job_finder.history import clear_history_directory
from job_finder.paths import (
    get_candidate_info_path,
    get_data_dir,
    get_db_path,
)


def main():
    data_dir = get_data_dir()
    if not os.path.isdir(data_dir):
        print(f"{data_dir}/ not found. Run orchestrator.py first.")
        return
    clear_history_directory()
    print("Cleared data/history/*.db and profile fingerprint (snapshots).")
    p = get_candidate_info_path()
    if os.path.isfile(p):
        os.remove(p)
        print(f"Removed {p}.")
    db_path = get_db_path()
    if os.path.isfile(db_path):
        os.remove(db_path)
        print("Removed data/sovereign_agent.db (jobs cleared).")
    else:
        print("No database file found; nothing to remove.")
    save_config(empty_template(), get_candidate_info_path(), record_snapshot=False)
    print("Wrote empty template to data/candidate_info.json (no history snapshot).")
    print("Run orchestrator.py to recreate the DB, then /setup and /fetchjobs as needed.")


if __name__ == "__main__":
    main()
