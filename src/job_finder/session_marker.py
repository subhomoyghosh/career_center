"""Write a session marker file so the audit can locate the JSONL transcript without probing CC internals."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Empirically verified via D-spike:
#   $CLAUDE_CODE_SESSION_ID is exposed in the agent shell.
#   Main JSONL:    ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl  (flat file)
#   Subagent dir:  /private/tmp/claude-501/<encoded-cwd>/<session-id>/tasks/
# Encoding rule (verified by listing ~/.claude/projects/): Claude Code replaces
# BOTH "/" and "_" with "-" in the absolute cwd. The leading "-" (from the
# leading "/") is preserved. Periods are kept as-is in this layout.

def _encoded_project_dir(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-")

def discover_paths():
    """Returns (session_id, main_jsonl, subagent_dir, reason).

    reason is None on success and a short failure code otherwise.
    main_jsonl/subagent_dir may be None even when session_id is present
    (e.g. when the JSONL file is not yet on disk for the current turn).
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        return None, None, None, "no_session_id_env_var"
    cwd = os.getcwd()
    encoded = _encoded_project_dir(cwd)
    home = Path.home()
    main_jsonl = home / ".claude" / "projects" / encoded / f"{sid}.jsonl"
    subagent_dir = Path("/private/tmp/claude-501") / encoded / sid / "tasks"
    if not main_jsonl.exists():
        return sid, None, None, f"main_jsonl_missing:{main_jsonl}"
    return sid, str(main_jsonl), str(subagent_dir), None

def write_marker(marker_path: str = "data/last_session.json") -> dict:
    sid, main, sub, reason = discover_paths()
    out = {
        "detected": sid is not None and main is not None,
        "session_id": sid,
        "main_jsonl_path": main,
        "subagent_dir": sub,
        "run_date": datetime.now(timezone.utc).isoformat(),
        "fetchjobs_command": True,
    }
    if reason:
        out["reason"] = reason
    Path(marker_path).parent.mkdir(parents=True, exist_ok=True)
    Path(marker_path).write_text(json.dumps(out, indent=2))
    return out

if __name__ == "__main__":
    print(json.dumps(write_marker(), indent=2))
