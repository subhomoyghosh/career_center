"""
Detect external (manual) edits to the active profile JSON via content hash.
On load_config, if the file changed since last known write, append candidate_history.
"""
import hashlib
import json
import os
from typing import Any, Dict, Optional

from job_finder.paths import get_history_dir

FINGERPRINT_FILENAME = ".candidate_profile_fingerprint.json"


def fingerprint_path() -> str:
    return os.path.join(get_history_dir(), FINGERPRINT_FILENAME)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_content_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def read_fingerprint() -> Optional[Dict[str, str]]:
    p = fingerprint_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            out = json.load(f)
        if isinstance(out, dict):
            return out
    except (OSError, json.JSONDecodeError):
        pass
    return None


def write_fingerprint(profile_path: str, content_sha256: str) -> None:
    os.makedirs(get_history_dir(), exist_ok=True)
    payload = {"profile_path": profile_path, "sha256": content_sha256}
    with open(fingerprint_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def sync_fingerprint_from_disk(profile_path: str) -> None:
    """After any write to profile_path (save_config, etc.), align fingerprint with file bytes."""
    if not os.path.isfile(profile_path):
        return
    write_fingerprint(profile_path, file_content_sha256(profile_path))


def maybe_record_snapshot_on_external_edit(
    profile_path: str, loaded_config: Dict[str, Any]
) -> None:
    """
    If disk content differs from last fingerprint, treat as external edit: snapshot then refresh.

    No fingerprint yet: establish baseline (no snapshot — avoids duplicating pre-existing file).
    profile_path differs from fingerprint (active file switched): re-baseline only.
    """
    if not os.path.isfile(profile_path):
        return
    current_hash = file_content_sha256(profile_path)
    fp = read_fingerprint()
    if fp is None:
        write_fingerprint(profile_path, current_hash)
        return
    if fp.get("profile_path") != profile_path:
        write_fingerprint(profile_path, current_hash)
        return
    if fp.get("sha256") == current_hash:
        return
    try:
        from job_finder.history import record_candidate_snapshot

        record_candidate_snapshot(loaded_config)
    except Exception:
        pass
    write_fingerprint(profile_path, current_hash)
