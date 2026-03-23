"""
Where config, databases, and history live.

Important: use absolute paths anchored at the repo root so persistence does not
depend on the current working directory (Cursor may invoke code with a
different cwd).
"""
import os

_THIS_FILE_DIR = os.path.dirname(os.path.abspath(__file__))  # .../src/job_finder
PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_FILE_DIR, "..", ".."))  # repo root

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CANDIDATE_INFO_JSON = "candidate_info.json"


def get_data_dir() -> str:
    """Path to the data folder (e.g. 'data')."""
    return DATA_DIR


def get_candidate_info_path() -> str:
    """Preferred profile path: data/candidate_info.json."""
    return os.path.join(DATA_DIR, CANDIDATE_INFO_JSON)


def resolve_active_config_path() -> str:
    """
    Which profile JSON is active for read/write.
    Always use data/candidate_info.json (create via orchestrator on new installs).
    """
    return get_candidate_info_path()


def get_config_path() -> str:
    """Alias for resolve_active_config_path() — active candidate profile JSON."""
    return resolve_active_config_path()


def get_db_path() -> str:
    """Path to the jobs database: data/sovereign_agent.db."""
    return os.path.join(DATA_DIR, "sovereign_agent.db")


def get_history_dir() -> str:
    """Folder for snapshot history databases: data/history/."""
    return os.path.join(DATA_DIR, "history")


def get_candidate_history_db_path() -> str:
    return os.path.join(get_history_dir(), "candidate_history.db")


def get_jobs_history_db_path() -> str:
    return os.path.join(get_history_dir(), "jobs_history.db")


def get_intelligence_history_db_path() -> str:
    return os.path.join(get_history_dir(), "intelligence_history.db")
