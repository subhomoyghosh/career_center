"""
Auto-improve change ledger + revert engine.

Two append-only JSONL files in data/:

    improve_proposals.jsonl
        Pending proposals written by `/improve --audit-only`. UI shows them in an
        approval queue. Status flips to applied/dismissed/stale when actioned.

    improve_changes.jsonl
        Applied changes. Each has a git commit_sha so revert is a structured
        `git revert --no-edit <sha>`. Conflicts surface explicit messages naming
        the offending later commit.

Design rules (non-negotiable):
- Nothing auto-applies. Every change is either user-approved in the UI or via
  `/improve --apply <change_id>` in chat.
- Every apply re-validates the precondition (the file or JSON value may have
  drifted between audit and approval). A drifted proposal is marked `stale`,
  never silently coerced.
- PYTHON_CODE_EDIT changes pass py_compile + import smoke-test before commit;
  failure leaves the working tree untouched.
- Reverts ride on `git revert` for 3-way-merge semantics. Cascading conflicts
  bubble up as `{ok: False, conflict: {...}}` — the UI tells the user which
  later commits to revert first.

Note on the project's git working tree: this module commits to whatever branch
is checked out. Commits are tagged `[improve] <pain_point> — <summary>
[change_id=<id>]` so the user can grep/cherry-pick. Apply aborts if the working
tree has unrelated uncommitted changes (we won't sweep the user's WIP into
an /improve commit).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from job_finder.paths import PROJECT_ROOT, get_data_dir, resolve_active_config_path

PROPOSALS_PATH = Path(get_data_dir()) / "improve_proposals.jsonl"
CHANGES_PATH = Path(get_data_dir()) / "improve_changes.jsonl"

# Patch types the engine can apply + revert.
PATCH_TEXT_REPLACE = "text_replace"
PATCH_JSON_SET = "json_set"
PATCH_JSON_APPEND = "json_append"
PATCH_JSON_REMOVE = "json_remove"

VALID_PATCH_TYPES = frozenset(
    {PATCH_TEXT_REPLACE, PATCH_JSON_SET, PATCH_JSON_APPEND, PATCH_JSON_REMOVE}
)

# Proposal lifecycle states. Stored in the proposal record's `status` field.
PROPOSAL_PENDING = "pending"
PROPOSAL_APPLIED = "applied"
PROPOSAL_DISMISSED = "dismissed"
PROPOSAL_STALE = "stale"           # file drifted between audit and approval
PROPOSAL_BLOCKED = "blocked"       # syntax check / git pre-commit failed

VALID_PROPOSAL_STATUSES = frozenset(
    {PROPOSAL_PENDING, PROPOSAL_APPLIED, PROPOSAL_DISMISSED, PROPOSAL_STALE, PROPOSAL_BLOCKED}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ImproveChangeError(Exception):
    """Base — all errors from this module subclass this so callers can catch one."""


class PreconditionError(ImproveChangeError):
    """File / JSON-value drifted between audit and apply (or revert)."""


class GitConflictError(ImproveChangeError):
    """git revert failed; later commits must be reverted first."""


class SyntaxCheckError(ImproveChangeError):
    """PYTHON_CODE_EDIT failed py_compile or import smoke-test."""


class WorkingTreeError(ImproveChangeError):
    """Unrelated uncommitted changes exist; refuse to mix them into an /improve commit."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_proposal(proposal: Dict[str, Any]) -> str:
    """Validate and persist a pending proposal. Returns the generated change_id.

    Caller (the /improve audit) supplies:
        pain_point, severity, summary, evidence (dict), file_changed (path),
        patch (dict matching one of the four types)
    """
    _ensure_data_dir()
    _validate_proposal_shape(proposal)

    change_id = _make_change_id(proposal["pain_point"])
    now = _utc_now_iso()
    record = {
        "change_id": change_id,
        "created_at": now,
        "pain_point": proposal["pain_point"],
        "severity": proposal["severity"],
        "evidence": proposal.get("evidence", {}),
        "summary": proposal.get("summary", ""),
        "file_changed": proposal["file_changed"],
        "patch": proposal["patch"],
        "precondition": _capture_precondition(proposal["file_changed"], proposal["patch"]),
        "status": PROPOSAL_PENDING,
    }
    _append_jsonl(PROPOSALS_PATH, record)
    return change_id


def list_pending_proposals(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Pending proposals, newest first."""
    proposals = _coalesce_proposals()
    pending = [p for p in proposals if p.get("status") == PROPOSAL_PENDING]
    pending.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    if limit is not None:
        return pending[:limit]
    return pending


def list_all_proposals(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every proposal we know about, newest first. Used by the UI when the user
    wants to see dismissed/stale rows in addition to pending."""
    proposals = _coalesce_proposals()
    proposals.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    if limit is not None:
        return proposals[:limit]
    return proposals


def dismiss_proposal(change_id: str, reason: str = "user_dismissed") -> Dict[str, Any]:
    """Flip a pending proposal to dismissed. Idempotent (re-dismiss is no-op)."""
    return _update_proposal_status(change_id, PROPOSAL_DISMISSED, reason=reason)


def apply_proposal(change_id: str, approved_by: str = "ui_user") -> Dict[str, Any]:
    """Approve + log one proposal.

    Files tracked by git get committed (revert via `git revert`).
    Gitignored files (e.g. data/candidate_info.json — personal data the user
    intentionally keeps out of git) skip the commit; revert uses the semantic
    inverse-op stored in the change record's patch.

    Returns one of:
        {ok: True,  change_id, commit_sha?, semantic_diff, revert_mode}
        {ok: False, reason: "stale" | "blocked_by_syntax" | "blocked_by_dirty_tree"
                              | "blocked_by_already_applied" | "not_found"
                              | "blocked_by_git",
                    detail: <human-readable>}
    """
    proposal = _find_proposal(change_id)
    if proposal is None:
        return {"ok": False, "reason": "not_found", "detail": f"unknown change_id: {change_id}"}
    if proposal.get("status") != PROPOSAL_PENDING:
        return {
            "ok": False,
            "reason": "blocked_by_already_applied",
            "detail": f"status={proposal.get('status')}",
        }

    file_changed = proposal["file_changed"]
    use_git = _file_is_git_tracked(file_changed)

    # Only enforce a clean working tree when we plan to commit; if the change
    # only touches gitignored files we don't risk sweeping the user's WIP.
    if use_git:
        dirty = _git_dirty_paths()
        if dirty:
            return {
                "ok": False,
                "reason": "blocked_by_dirty_tree",
                "detail": (
                    "Working tree has uncommitted changes outside /improve scope. "
                    "Commit or stash these first: " + ", ".join(dirty[:5]) +
                    ("…" if len(dirty) > 5 else "")
                ),
                "dirty_paths": dirty,
            }

    # Re-validate precondition (the file may have drifted since audit).
    try:
        _verify_precondition(proposal)
    except PreconditionError as e:
        _update_proposal_status(change_id, PROPOSAL_STALE, reason=str(e))
        return {"ok": False, "reason": "stale", "detail": str(e)}

    # Execute the edit.
    try:
        semantic_diff = _execute_patch(proposal)
    except SyntaxCheckError as e:
        _update_proposal_status(change_id, PROPOSAL_BLOCKED, reason=f"syntax: {e}")
        return {"ok": False, "reason": "blocked_by_syntax", "detail": str(e)}

    commit_sha: Optional[str] = None
    revert_mode = "semantic"
    if use_git:
        try:
            commit_sha = _git_commit_for(proposal)
            revert_mode = "git"
        except subprocess.CalledProcessError as e:
            _rollback_via_git_checkout(proposal["file_changed"])
            stderr = ""
            if hasattr(e, "stderr") and e.stderr:
                stderr = e.stderr.strip() if isinstance(e.stderr, str) else str(e.stderr)
            return {
                "ok": False,
                "reason": "blocked_by_git",
                "detail": f"git commit failed: {stderr or e}",
            }

    applied_at = _utc_now_iso()
    change_record = {
        "change_id": change_id,
        "applied_at": applied_at,
        "approved_by": approved_by,
        "commit_sha": commit_sha,
        "revert_mode": revert_mode,
        "patch": proposal["patch"],  # needed for semantic revert
        "pain_point": proposal["pain_point"],
        "severity": proposal["severity"],
        "file_changed": file_changed,
        "summary": proposal.get("summary", ""),
        "semantic_diff": semantic_diff,
        "reverted": False,
        "revert_commit_sha": None,
        "reverted_at": None,
    }
    _append_jsonl(CHANGES_PATH, change_record)
    _update_proposal_status(change_id, PROPOSAL_APPLIED, reason="ui_user")
    _maybe_mirror_to_improve_log(change_record)

    return {
        "ok": True,
        "change_id": change_id,
        "commit_sha": commit_sha,
        "revert_mode": revert_mode,
        "semantic_diff": semantic_diff,
    }


def revert_change(change_id: str, reverted_by: str = "ui_user") -> Dict[str, Any]:
    """Revert one applied change. Uses `git revert` when the change was committed,
    otherwise applies the semantic inverse of the recorded patch.

    Returns:
        {ok: True,  change_id, revert_mode: "git"|"semantic", revert_commit_sha?}
        {ok: False, reason: "already_reverted" | "not_found" | "conflict"
                              | "blocked_by_dirty_tree" | "stale_semantic",
                    detail: ...,
                    conflicting_files?: [...]}
    """
    record = _find_change_record(change_id)
    if record is None:
        return {"ok": False, "reason": "not_found", "detail": f"unknown change_id: {change_id}"}
    if record.get("reverted"):
        return {
            "ok": False,
            "reason": "already_reverted",
            "detail": f"reverted at {record.get('reverted_at')} via {record.get('revert_commit_sha') or 'semantic-inverse'}",
        }

    mode = record.get("revert_mode") or ("git" if record.get("commit_sha") else "semantic")
    if mode == "git":
        return _revert_via_git(record, reverted_by)
    return _revert_via_semantic_inverse(record, reverted_by)


def _revert_via_git(record: Dict[str, Any], reverted_by: str) -> Dict[str, Any]:
    change_id = record["change_id"]
    dirty = _git_dirty_paths()
    if dirty:
        return {
            "ok": False,
            "reason": "blocked_by_dirty_tree",
            "detail": "Working tree has uncommitted changes; resolve before reverting.",
            "files": dirty,
        }
    sha = record["commit_sha"]
    rc, out, err = _git_run(["revert", "--no-edit", sha])
    if rc != 0:
        _git_run(["revert", "--abort"])
        offending = _parse_revert_conflict(err or out)
        return {
            "ok": False,
            "reason": "conflict",
            "detail": (
                f"git revert conflicted on commit {sha[:7]}. Revert any later /improve "
                "commits that touched the same lines first, then retry."
            ),
            "raw_stderr": (err or "").strip()[:500],
            "conflicting_files": offending,
        }
    rc2, sha_out, _ = _git_run(["rev-parse", "HEAD"])
    revert_sha = sha_out.strip() if rc2 == 0 else ""
    _append_jsonl(
        CHANGES_PATH,
        {
            "event": "revert",
            "change_id": change_id,
            "reverted_at": _utc_now_iso(),
            "reverted_by": reverted_by,
            "revert_commit_sha": revert_sha,
            "of_commit_sha": sha,
            "revert_mode": "git",
        },
    )
    return {
        "ok": True,
        "change_id": change_id,
        "revert_mode": "git",
        "revert_commit_sha": revert_sha,
    }


def _revert_via_semantic_inverse(record: Dict[str, Any], reverted_by: str) -> Dict[str, Any]:
    """Apply the inverse of the recorded patch without touching git history.

    text_replace: swap new_string back to old_string (must appear uniquely).
    json_set:     set key back to old_value (must currently equal new_value).
    json_append:  remove the specific appended_items (matched by composite key;
                  later appends to the same list are preserved).
    json_remove:  restore old_value at key_path (must currently be absent).
    """
    change_id = record["change_id"]
    patch = record.get("patch")
    if not patch:
        return {
            "ok": False,
            "reason": "stale_semantic",
            "detail": "change record has no patch payload; cannot reconstruct inverse",
        }
    file_changed = record["file_changed"]
    abs_path = _abs_repo_path(file_changed)
    ptype = patch["type"]
    try:
        if ptype == PATCH_TEXT_REPLACE:
            text = Path(abs_path).read_text(encoding="utf-8")
            new_s = patch["new_string"]
            old_s = patch["old_string"]
            occurrences = text.count(new_s)
            if occurrences == 0:
                return {
                    "ok": False,
                    "reason": "stale_semantic",
                    "detail": (
                        f"target text not present in {file_changed} — file changed "
                        "since this /improve edit. A later change may have overwritten it."
                    ),
                }
            if occurrences > 1:
                return {
                    "ok": False,
                    "reason": "conflict",
                    "detail": (
                        f"target text appears {occurrences} times in {file_changed}; "
                        "ambiguous revert. Edit manually."
                    ),
                }
            Path(abs_path).write_text(text.replace(new_s, old_s, 1), encoding="utf-8")
        elif ptype == PATCH_JSON_SET:
            cfg = _read_json(abs_path)
            current = _walk_keys(cfg, patch["key_path"], default=_SENTINEL)
            if _stable_repr(current) != _stable_repr(patch["new_value"]):
                return {
                    "ok": False,
                    "reason": "conflict",
                    "detail": (
                        f"current value at {patch['key_path']} no longer equals what was set; "
                        "a later change overwrote it. Revert later changes first."
                    ),
                }
            _set_keys(cfg, patch["key_path"], patch["old_value"])
            _save_json_file(abs_path, cfg)
        elif ptype == PATCH_JSON_APPEND:
            cfg = _read_json(abs_path)
            current = _walk_keys(cfg, patch["key_path"], default=None)
            if current is None or not isinstance(current, list):
                return {
                    "ok": False,
                    "reason": "stale_semantic",
                    "detail": f"{patch['key_path']} is not a list; cannot revert append",
                }
            remaining = list(current)
            removed_count = 0
            for target in patch["appended_items"]:
                idx = _find_matching_item_index(remaining, target)
                if idx is not None:
                    remaining.pop(idx)
                    removed_count += 1
            if removed_count == 0:
                return {
                    "ok": False,
                    "reason": "stale_semantic",
                    "detail": (
                        f"appended items not found in {patch['key_path']} — likely "
                        "already removed manually or by a later revert"
                    ),
                }
            _set_keys(cfg, patch["key_path"], remaining)
            _save_json_file(abs_path, cfg)
        elif ptype == PATCH_JSON_REMOVE:
            cfg = _read_json(abs_path)
            current = _walk_keys(cfg, patch["key_path"], default=_SENTINEL)
            if not isinstance(current, _Sentinel):
                return {
                    "ok": False,
                    "reason": "conflict",
                    "detail": f"{patch['key_path']} already has a value; cannot restore",
                }
            _set_keys(cfg, patch["key_path"], patch["old_value"])
            _save_json_file(abs_path, cfg)
        else:
            return {
                "ok": False,
                "reason": "stale_semantic",
                "detail": f"unknown patch type for semantic revert: {ptype}",
            }
    except OSError as e:
        return {"ok": False, "reason": "stale_semantic", "detail": f"file IO failed: {e}"}

    _append_jsonl(
        CHANGES_PATH,
        {
            "event": "revert",
            "change_id": change_id,
            "reverted_at": _utc_now_iso(),
            "reverted_by": reverted_by,
            "revert_commit_sha": None,
            "of_commit_sha": record.get("commit_sha"),
            "revert_mode": "semantic",
        },
    )
    return {
        "ok": True,
        "change_id": change_id,
        "revert_mode": "semantic",
        "revert_commit_sha": None,
    }


def _find_matching_item_index(items: List[Any], target: Any) -> Optional[int]:
    """Find the first item in `items` that matches `target`. For dicts, match by
    composite key (`pattern` + `added_at` if both present, else full dict-equal).
    For primitives, strict equality."""
    if isinstance(target, dict) and "pattern" in target and "added_at" in target:
        for i, x in enumerate(items):
            if isinstance(x, dict) and x.get("pattern") == target.get("pattern") \
                    and x.get("added_at") == target.get("added_at"):
                return i
        return None
    for i, x in enumerate(items):
        if _stable_repr(x) == _stable_repr(target):
            return i
    return None


def _save_json_file(abs_path: str, cfg: Dict[str, Any]) -> None:
    """Use save_config() when the target is the active candidate profile (so
    snapshot + fingerprint logic stays consistent); otherwise atomic-write."""
    if Path(abs_path).resolve() == Path(resolve_active_config_path()).resolve():
        from job_finder.config import save_config
        save_config(cfg)
    else:
        _write_json_atomic(abs_path, cfg)


def list_applied_changes(
    limit: int = 20,
    include_reverted: bool = True,
) -> List[Dict[str, Any]]:
    """Applied changes newest first, with reverted=True flag set when a sibling
    revert event exists in the log. Use this for the UI history table."""
    records = _coalesce_changes()
    if not include_reverted:
        records = [r for r in records if not r.get("reverted")]
    records.sort(key=lambda r: r.get("applied_at", ""), reverse=True)
    return records[:limit]


def status_of(change_id: str) -> str:
    """One of: 'pending', 'applied', 'reverted', 'dismissed', 'stale', 'blocked', 'unknown'."""
    proposal = _find_proposal(change_id)
    if proposal:
        if proposal.get("status") == PROPOSAL_APPLIED:
            record = _find_change_record(change_id)
            if record and record.get("reverted"):
                return "reverted"
            return "applied"
        return proposal.get("status", "unknown")
    record = _find_change_record(change_id)
    if record:
        return "reverted" if record.get("reverted") else "applied"
    return "unknown"


# ---------------------------------------------------------------------------
# Internals — schema validation + IDs
# ---------------------------------------------------------------------------


def _validate_proposal_shape(p: Dict[str, Any]) -> None:
    required_top = ("pain_point", "severity", "file_changed", "patch")
    missing = [k for k in required_top if k not in p]
    if missing:
        raise ImproveChangeError(f"proposal missing keys: {missing}")
    patch = p["patch"]
    if not isinstance(patch, dict) or "type" not in patch:
        raise ImproveChangeError("patch must be a dict with a 'type' field")
    ptype = patch["type"]
    if ptype not in VALID_PATCH_TYPES:
        raise ImproveChangeError(f"patch.type {ptype!r} not in {sorted(VALID_PATCH_TYPES)}")
    if ptype == PATCH_TEXT_REPLACE:
        for k in ("old_string", "new_string"):
            if k not in patch:
                raise ImproveChangeError(f"text_replace patch missing {k}")
        if patch["old_string"] == patch["new_string"]:
            raise ImproveChangeError("text_replace: old_string must differ from new_string")
    elif ptype == PATCH_JSON_SET:
        for k in ("key_path", "old_value", "new_value"):
            if k not in patch:
                raise ImproveChangeError(f"json_set patch missing {k}")
        if not isinstance(patch["key_path"], list) or not patch["key_path"]:
            raise ImproveChangeError("json_set.key_path must be a non-empty list")
    elif ptype == PATCH_JSON_APPEND:
        for k in ("key_path", "appended_items"):
            if k not in patch:
                raise ImproveChangeError(f"json_append patch missing {k}")
        if not isinstance(patch["appended_items"], list):
            raise ImproveChangeError("json_append.appended_items must be a list")
        if not isinstance(patch["key_path"], list) or not patch["key_path"]:
            raise ImproveChangeError("json_append.key_path must be a non-empty list")
    elif ptype == PATCH_JSON_REMOVE:
        for k in ("key_path", "old_value"):
            if k not in patch:
                raise ImproveChangeError(f"json_remove patch missing {k}")


def _make_change_id(pain_point: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(2)  # 4 hex chars, plenty to disambiguate
    safe_pp = re.sub(r"[^A-Za-z0-9_]", "_", pain_point)[:48]
    return f"imp_{ts}_{safe_pp}_{suffix}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_data_dir() -> None:
    os.makedirs(get_data_dir(), exist_ok=True)


# ---------------------------------------------------------------------------
# Internals — JSONL append + read (append-only; status flips via new lines)
# ---------------------------------------------------------------------------


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _coalesce_proposals() -> List[Dict[str, Any]]:
    """Build the current view of each proposal by replaying the JSONL.
    Later lines for the same change_id are status updates; the last one wins."""
    rows = _read_jsonl(PROPOSALS_PATH)
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = row.get("change_id")
        if not cid:
            continue
        if "patch" in row:
            # Initial proposal row.
            by_id[cid] = dict(row)
        else:
            # Status update.
            if cid in by_id:
                by_id[cid].update(row)
    return list(by_id.values())


def _coalesce_changes() -> List[Dict[str, Any]]:
    """Build the current view of each applied change. Revert events flip the
    sibling record's reverted/revert_commit_sha/reverted_at fields."""
    rows = _read_jsonl(CHANGES_PATH)
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cid = row.get("change_id")
        if not cid:
            continue
        if row.get("event") == "revert":
            if cid in by_id:
                by_id[cid]["reverted"] = True
                by_id[cid]["revert_commit_sha"] = row.get("revert_commit_sha")
                by_id[cid]["reverted_at"] = row.get("reverted_at")
                by_id[cid]["reverted_by"] = row.get("reverted_by")
        else:
            # Initial applied record (or later status patch).
            if cid not in by_id:
                by_id[cid] = dict(row)
            else:
                by_id[cid].update(row)
    return list(by_id.values())


def _find_proposal(change_id: str) -> Optional[Dict[str, Any]]:
    for p in _coalesce_proposals():
        if p.get("change_id") == change_id:
            return p
    return None


def _find_change_record(change_id: str) -> Optional[Dict[str, Any]]:
    for r in _coalesce_changes():
        if r.get("change_id") == change_id:
            return r
    return None


def _update_proposal_status(change_id: str, status: str, reason: str = "") -> Dict[str, Any]:
    if status not in VALID_PROPOSAL_STATUSES:
        raise ImproveChangeError(f"invalid status: {status}")
    _append_jsonl(
        PROPOSALS_PATH,
        {
            "change_id": change_id,
            "status": status,
            "status_changed_at": _utc_now_iso(),
            "status_reason": reason,
        },
    )
    updated = _find_proposal(change_id) or {}
    return {"ok": True, "change_id": change_id, "status": updated.get("status", status)}


# ---------------------------------------------------------------------------
# Internals — precondition capture + verification
# ---------------------------------------------------------------------------


def _capture_precondition(file_changed: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    abs_path = _abs_repo_path(file_changed)
    pre: Dict[str, Any] = {"captured_at": _utc_now_iso()}
    ptype = patch["type"]
    if ptype == PATCH_TEXT_REPLACE:
        try:
            pre["file_sha256"] = _file_sha256(abs_path)
        except OSError:
            pre["file_sha256"] = None
    elif ptype in (PATCH_JSON_SET, PATCH_JSON_APPEND, PATCH_JSON_REMOVE):
        try:
            cfg = _read_json(abs_path)
            current = _walk_keys(cfg, patch["key_path"], default=None)
            pre["current_value_repr"] = _stable_repr(current)
        except OSError:
            pre["current_value_repr"] = None
    return pre


def _verify_precondition(proposal: Dict[str, Any]) -> None:
    patch = proposal["patch"]
    abs_path = _abs_repo_path(proposal["file_changed"])
    ptype = patch["type"]
    if ptype == PATCH_TEXT_REPLACE:
        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError as e:
            raise PreconditionError(f"cannot read {abs_path}: {e}")
        old = patch["old_string"]
        new = patch["new_string"]
        if new in text and old not in text:
            raise PreconditionError(
                "new_string already present and old_string absent — change appears already applied"
            )
        if old not in text:
            raise PreconditionError(
                f"old_string not found in {proposal['file_changed']}; file drifted since audit"
            )
        if text.count(old) > 1:
            raise PreconditionError(
                f"old_string appears {text.count(old)} times in {proposal['file_changed']}; "
                "ambiguous — audit must regenerate with a more specific anchor"
            )
    elif ptype == PATCH_JSON_SET:
        try:
            cfg = _read_json(abs_path)
        except OSError as e:
            raise PreconditionError(f"cannot read {abs_path}: {e}")
        current = _walk_keys(cfg, patch["key_path"], default=_SENTINEL)
        if _stable_repr(current) != _stable_repr(patch["old_value"]):
            raise PreconditionError(
                f"json_set: value at {patch['key_path']} drifted since audit"
            )
    elif ptype == PATCH_JSON_APPEND:
        # Append is idempotent (we always extend the list with appended_items).
        # The only failure is "key exists and is not a list".
        try:
            cfg = _read_json(abs_path)
        except OSError as e:
            raise PreconditionError(f"cannot read {abs_path}: {e}")
        current = _walk_keys(cfg, patch["key_path"], default=[])
        if current is not None and not isinstance(current, list):
            raise PreconditionError(
                f"json_append: {patch['key_path']} is not a list ({type(current).__name__})"
            )
    elif ptype == PATCH_JSON_REMOVE:
        try:
            cfg = _read_json(abs_path)
        except OSError as e:
            raise PreconditionError(f"cannot read {abs_path}: {e}")
        current = _walk_keys(cfg, patch["key_path"], default=_SENTINEL)
        if _stable_repr(current) != _stable_repr(patch["old_value"]):
            raise PreconditionError(
                f"json_remove: value at {patch['key_path']} drifted since audit"
            )


# ---------------------------------------------------------------------------
# Internals — execute the patch
# ---------------------------------------------------------------------------


def _execute_patch(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a semantic_diff dict for the UI."""
    patch = proposal["patch"]
    file_changed = proposal["file_changed"]
    abs_path = _abs_repo_path(file_changed)
    ptype = patch["type"]

    if ptype == PATCH_TEXT_REPLACE:
        text = Path(abs_path).read_text(encoding="utf-8")
        new_text = text.replace(patch["old_string"], patch["new_string"], 1)
        # PYTHON_CODE_EDIT extra safety: stage + py_compile + import smoke.
        if abs_path.endswith(".py"):
            _stage_and_smoke_test_python(abs_path, new_text)
        Path(abs_path).write_text(new_text, encoding="utf-8")
        return {
            "type": "text_replace",
            "file": file_changed,
            "removed_chars": len(patch["old_string"]),
            "added_chars": len(patch["new_string"]),
            "preview_before": _truncate(patch["old_string"], 200),
            "preview_after": _truncate(patch["new_string"], 200),
        }

    if ptype in (PATCH_JSON_SET, PATCH_JSON_APPEND, PATCH_JSON_REMOVE):
        cfg = _read_json(abs_path)
        if ptype == PATCH_JSON_SET:
            _set_keys(cfg, patch["key_path"], patch["new_value"])
            diff = {
                "type": "json_set",
                "file": file_changed,
                "field": ".".join(map(str, patch["key_path"])),
                "before": patch["old_value"],
                "after": patch["new_value"],
            }
        elif ptype == PATCH_JSON_APPEND:
            current = _walk_keys(cfg, patch["key_path"], default=None)
            if current is None:
                current = []
                _set_keys(cfg, patch["key_path"], current)
            for item in patch["appended_items"]:
                current.append(copy.deepcopy(item))
            diff = {
                "type": "json_append",
                "file": file_changed,
                "field": ".".join(map(str, patch["key_path"])),
                "added": patch["appended_items"],
            }
        else:  # PATCH_JSON_REMOVE
            _del_keys(cfg, patch["key_path"])
            diff = {
                "type": "json_remove",
                "file": file_changed,
                "field": ".".join(map(str, patch["key_path"])),
                "removed": patch["old_value"],
            }
        # Use save_config when targeting candidate_info.json so snapshot +
        # fingerprint logic stays consistent with the rest of the app.
        if Path(abs_path).resolve() == Path(resolve_active_config_path()).resolve():
            from job_finder.config import save_config
            save_config(cfg)
        else:
            _write_json_atomic(abs_path, cfg)
        return diff

    raise ImproveChangeError(f"unknown patch type at execute: {ptype}")


def _stage_and_smoke_test_python(target_abs: str, new_text: str) -> None:
    """Write candidate text to a temp staging file, py_compile + import smoke,
    raise SyntaxCheckError on failure (target file untouched)."""
    stage_dir = tempfile.mkdtemp(prefix="improve_stage_")
    try:
        stage_path = Path(stage_dir) / Path(target_abs).name
        stage_path.write_text(new_text, encoding="utf-8")
        # py_compile.
        rc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(stage_path)],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            raise SyntaxCheckError(
                f"py_compile failed for {Path(target_abs).name}: {rc.stderr.strip()[:300]}"
            )
        # Import smoke: substitute the staged file in place momentarily and
        # try to import its module. This catches NameError / ImportError that
        # py_compile misses.
        try:
            rel = Path(target_abs).relative_to(PROJECT_ROOT)
        except ValueError:
            return  # outside project; skip import smoke
        if not str(rel).startswith("src/job_finder/"):
            return  # only smoke-test our own package
        module_part = str(rel)[len("src/"):].replace(os.sep, ".").removesuffix(".py")
        backup = Path(target_abs).read_bytes()
        Path(target_abs).write_text(new_text, encoding="utf-8")
        try:
            smoke = subprocess.run(
                [sys.executable, "-c", f"import {module_part}"],
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT,
            )
            if smoke.returncode != 0:
                raise SyntaxCheckError(
                    f"import smoke failed for {module_part}: {smoke.stderr.strip()[:300]}"
                )
        finally:
            # Restore so the actual write happens via the caller, not here.
            Path(target_abs).write_bytes(backup)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Internals — git plumbing
# ---------------------------------------------------------------------------


def _git_run(args: List[str], check: bool = False) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", PROJECT_ROOT, *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _file_is_git_tracked(rel_or_abs: str) -> bool:
    """True when the file is tracked by git (not gitignored). Determines whether
    apply uses the git path or the semantic-inverse path."""
    abs_path = _abs_repo_path(rel_or_abs)
    try:
        rel = str(Path(abs_path).relative_to(PROJECT_ROOT))
    except ValueError:
        return False
    rc, _, _ = _git_run(["check-ignore", "-q", rel])
    if rc == 0:
        return False  # path matches a gitignore rule
    return True


def _git_dirty_paths() -> List[str]:
    rc, out, _ = _git_run(["status", "--porcelain"])
    if rc != 0:
        return []
    paths: List[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # "XY path" — skip our own staged paths since apply will commit them.
        path = line[3:].strip()
        # We allow data/improve_*.jsonl since the apply itself writes them.
        if path.startswith("data/improve_proposals.jsonl") or path.startswith(
            "data/improve_changes.jsonl"
        ):
            continue
        paths.append(path)
    return paths


def _git_commit_for(proposal: Dict[str, Any]) -> str:
    file_changed = proposal["file_changed"]
    abs_path = _abs_repo_path(file_changed)
    try:
        rel = str(Path(abs_path).relative_to(PROJECT_ROOT))
    except ValueError:
        rel = file_changed
    add_args = ["add", "--", rel]
    rc_a, _, err_a = _git_run(add_args)
    if rc_a != 0:
        raise subprocess.CalledProcessError(rc_a, add_args, stderr=err_a)
    summary = proposal.get("summary", "").splitlines()[0][:80] if proposal.get("summary") else ""
    msg = f"[improve] {proposal['pain_point']} — {summary} [change_id={proposal['change_id']}]"
    rc_c, _, err_c = _git_run(["commit", "-m", msg])
    if rc_c != 0:
        raise subprocess.CalledProcessError(rc_c, ["git", "commit"], stderr=err_c)
    rc_h, sha, _ = _git_run(["rev-parse", "HEAD"])
    if rc_h != 0:
        raise subprocess.CalledProcessError(rc_h, ["git", "rev-parse"])
    return sha.strip()


def _rollback_via_git_checkout(file_changed: str) -> None:
    abs_path = _abs_repo_path(file_changed)
    try:
        rel = str(Path(abs_path).relative_to(PROJECT_ROOT))
    except ValueError:
        return
    _git_run(["checkout", "--", rel])


def _parse_revert_conflict(stderr: str) -> List[str]:
    """Pull the conflicting file paths out of `git revert` stderr."""
    paths: List[str] = []
    for line in (stderr or "").splitlines():
        line = line.strip()
        if line.startswith("CONFLICT") and ":" in line:
            tail = line.split(":", 1)[1].strip()
            if " " in tail:
                paths.append(tail.split()[-1])
    return paths


# ---------------------------------------------------------------------------
# Internals — JSON path helpers
# ---------------------------------------------------------------------------


class _Sentinel:
    pass


_SENTINEL = _Sentinel()


def _walk_keys(obj: Any, key_path: List[Any], default: Any = None) -> Any:
    cur = obj
    for k in key_path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and isinstance(k, int) and 0 <= k < len(cur):
            cur = cur[k]
        else:
            return default
    return cur


def _set_keys(obj: Any, key_path: List[Any], value: Any) -> None:
    cur = obj
    for k in key_path[:-1]:
        if isinstance(cur, dict):
            if k not in cur or not isinstance(cur[k], (dict, list)):
                cur[k] = {}
            cur = cur[k]
        else:
            raise ImproveChangeError(f"cannot set keys through non-dict at {k}")
    cur[key_path[-1]] = value


def _del_keys(obj: Any, key_path: List[Any]) -> None:
    cur = obj
    for k in key_path[:-1]:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return
    last = key_path[-1]
    if isinstance(cur, dict) and last in cur:
        del cur[last]


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp_improve"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _stable_repr(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _abs_repo_path(rel_or_abs: str) -> str:
    if os.path.isabs(rel_or_abs):
        return rel_or_abs
    return os.path.join(PROJECT_ROOT, rel_or_abs)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Legacy mirror — keep §1.8 dedup working
# ---------------------------------------------------------------------------


def _maybe_mirror_to_improve_log(change_record: Dict[str, Any]) -> None:
    """Write a thin summary line to data/improve_log.jsonl so the existing
    14-day dedup logic in /improve §1.8 keeps working without changes."""
    legacy_path = Path(get_data_dir()) / "improve_log.jsonl"
    entry = {
        "timestamp": change_record.get("applied_at"),
        "pain_point": change_record.get("pain_point"),
        "severity": change_record.get("severity"),
        "file_changed": change_record.get("file_changed"),
        "section": "",
        "evidence": "",
        "approved_by": change_record.get("approved_by", "ui_user"),
        "summary": change_record.get("summary", ""),
        "change_id": change_record.get("change_id"),
        "commit_sha": change_record.get("commit_sha"),
    }
    try:
        _append_jsonl(legacy_path, entry)
    except OSError:
        pass
