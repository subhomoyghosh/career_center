"""Tests for the archive_section patch type in src/job_finder/improve_changes.py.

Covers:
  - apply_proposal moves a markdown section into .claude/_archive/<...>.md and
    leaves a stub behind.
  - revert_change restores the source byte-exact and deletes the archive file.
  - Ambiguous heading (same heading appears twice) -> PreconditionError on apply.
  - archive_path outside .claude/_archive/ rejected at write_proposal time.
  - source_pre_hash_sha256 mismatch -> apply returns stale.

The test redirects PROJECT_ROOT and the ledger paths into a tmpdir and patches
`_file_is_git_tracked` so the apply path takes the semantic (non-git) route.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from job_finder import improve_changes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect improve_changes module state into tmp_path and force the
    semantic (non-git) apply branch. Returns a small helper bag."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Pre-create the archive dir parent so write_bytes_atomic always has a parent.
    (tmp_path / ".claude" / "_archive").mkdir(parents=True)

    monkeypatch.setattr(improve_changes, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(improve_changes, "PROPOSALS_PATH", data_dir / "improve_proposals.jsonl")
    monkeypatch.setattr(improve_changes, "CHANGES_PATH", data_dir / "improve_changes.jsonl")
    monkeypatch.setattr(improve_changes, "get_data_dir", lambda: str(data_dir))
    # Force apply onto the semantic (non-git) branch.
    monkeypatch.setattr(improve_changes, "_file_is_git_tracked", lambda _p: False)

    class Env:
        root = tmp_path
        data = data_dir

    return Env()


def _make_skill_file(root: Path, name: str = "fetchjobs.md") -> Path:
    """Create a fake skill file with three sections at level 2."""
    skill_dir = root / ".claude" / "commands"
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "# Top heading\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "## Hot Section\n"
        "\n"
        "Hot content that stays.\n"
        "\n"
        "## Cold Section\n"
        "\n"
        "Cold content body line 1.\n"
        "Cold content body line 2.\n"
        "\n"
        "### Cold subsection\n"
        "\n"
        "Some subsection text.\n"
        "\n"
        "## Tail Section\n"
        "\n"
        "Tail content.\n"
    )
    p = skill_dir / name
    p.write_text(body, encoding="utf-8")
    return p


def _archive_rel(name: str) -> str:
    return f".claude/_archive/{name}"


def _source_rel(p: Path, root: Path) -> str:
    return str(p.relative_to(root))


# ---------------------------------------------------------------------------
# Happy path: apply + revert round-trip
# ---------------------------------------------------------------------------


def test_archive_section_apply_then_revert_round_trips(env):
    src = _make_skill_file(env.root)
    src_rel = _source_rel(src, env.root)
    original_bytes = src.read_bytes()

    archive_rel = _archive_rel("fetchjobs__cold_section.md")
    stub_text = "## Cold Section (archived) — see .claude/_archive/fetchjobs__cold_section.md\n"

    proposal = {
        "pain_point": "cold_section_archive",
        "severity": "low",
        "summary": "archive cold section to keep skill file lean",
        "file_changed": src_rel,
        "patch": {
            "type": improve_changes.PATCH_ARCHIVE_SECTION,
            "source_path": src_rel,
            "section_heading": "Cold Section",
            "archive_path": archive_rel,
            "stub_text": stub_text,
        },
    }
    change_id = improve_changes.write_proposal(proposal)

    pre_metrics = {"valid_jobs": 12, "tokens_per_valid_job": 1500.0}
    result = improve_changes.apply_proposal(change_id, pre_metrics=pre_metrics)
    assert result["ok"] is True, result
    assert result["revert_mode"] == "semantic"
    assert result["semantic_diff"]["type"] == "archive_section"
    assert result["semantic_diff"]["original_level"] == 2
    assert "captured_content" in result["semantic_diff"]

    # Archive file exists and contains the moved section including its
    # subsection (### Cold subsection is deeper than the section level).
    archive_abs = env.root / archive_rel
    assert archive_abs.is_file()
    archive_text = archive_abs.read_text(encoding="utf-8")
    assert archive_text.startswith("## Cold Section\n")
    assert "Cold content body line 1." in archive_text
    assert "### Cold subsection" in archive_text
    assert "Some subsection text." in archive_text
    # Must stop before the next level-2 heading.
    assert "## Tail Section" not in archive_text

    # Source has the stub in place of the section, and the surrounding sections intact.
    new_src_text = src.read_text(encoding="utf-8")
    assert stub_text in new_src_text
    assert "Cold content body line 1." not in new_src_text
    assert "## Hot Section" in new_src_text
    assert "## Tail Section" in new_src_text
    assert "### Cold subsection" not in new_src_text  # subsection moved with parent

    # pre_metrics must be persisted on the change row.
    changes = improve_changes._read_jsonl(improve_changes.CHANGES_PATH)
    applied_rows = [r for r in changes if r.get("change_id") == change_id and r.get("event") != "revert"]
    assert applied_rows, "expected an applied row"
    assert applied_rows[0]["pre_metrics"] == pre_metrics

    # Now revert and confirm byte-exact restoration.
    revert_result = improve_changes.revert_change(change_id)
    assert revert_result["ok"] is True, revert_result
    assert revert_result["revert_mode"] == "semantic"
    assert src.read_bytes() == original_bytes
    assert not archive_abs.exists()


# ---------------------------------------------------------------------------
# pre_metrics kwarg is back-compat (None when omitted)
# ---------------------------------------------------------------------------


def test_apply_proposal_pre_metrics_optional(env):
    src = _make_skill_file(env.root, name="other.md")
    src_rel = _source_rel(src, env.root)
    proposal = {
        "pain_point": "back_compat_check",
        "severity": "low",
        "file_changed": src_rel,
        "patch": {
            "type": improve_changes.PATCH_ARCHIVE_SECTION,
            "source_path": src_rel,
            "section_heading": "Hot Section",
            "archive_path": _archive_rel("other__hot.md"),
            "stub_text": "## Hot Section (archived)\n",
        },
    }
    change_id = improve_changes.write_proposal(proposal)
    result = improve_changes.apply_proposal(change_id)  # no pre_metrics arg
    assert result["ok"] is True
    rows = improve_changes._read_jsonl(improve_changes.CHANGES_PATH)
    applied = [r for r in rows if r.get("change_id") == change_id and r.get("event") != "revert"]
    assert applied and applied[0]["pre_metrics"] is None


# ---------------------------------------------------------------------------
# Edge: ambiguous heading -> apply fails as stale (PreconditionError surfaced)
# ---------------------------------------------------------------------------


def test_archive_section_ambiguous_heading_returns_stale(env):
    skill_dir = env.root / ".claude" / "commands"
    skill_dir.mkdir(parents=True, exist_ok=True)
    src = skill_dir / "dup.md"
    src.write_text(
        "# Top\n\n## Duplicate\n\nfirst.\n\n## Other\n\ntext.\n\n## Duplicate\n\nsecond.\n",
        encoding="utf-8",
    )
    src_rel = _source_rel(src, env.root)
    proposal = {
        "pain_point": "dup_heading_test",
        "severity": "low",
        "file_changed": src_rel,
        "patch": {
            "type": improve_changes.PATCH_ARCHIVE_SECTION,
            "source_path": src_rel,
            "section_heading": "Duplicate",
            "archive_path": _archive_rel("dup__duplicate.md"),
            "stub_text": "## Duplicate (archived)\n",
        },
    }
    change_id = improve_changes.write_proposal(proposal)
    result = improve_changes.apply_proposal(change_id)
    assert result["ok"] is False
    assert result["reason"] == "stale"
    assert "ambiguous" in result["detail"].lower() or "appears" in result["detail"].lower()
    # Source untouched, no archive created.
    assert "first." in src.read_text(encoding="utf-8")
    assert "second." in src.read_text(encoding="utf-8")
    assert not (env.root / ".claude" / "_archive" / "dup__duplicate.md").exists()


# ---------------------------------------------------------------------------
# Edge: archive_path outside .claude/_archive/ -> rejected at write_proposal
# ---------------------------------------------------------------------------


def test_archive_path_outside_archive_dir_rejected_at_write(env):
    src = _make_skill_file(env.root, name="bad.md")
    src_rel = _source_rel(src, env.root)
    bad_paths = [
        "data/escape.md",
        ".claude/commands/escape.md",
        ".claude/_archive/../escape.md",
        "../escape.md",
    ]
    for bad in bad_paths:
        proposal = {
            "pain_point": "path_traversal_test",
            "severity": "low",
            "file_changed": src_rel,
            "patch": {
                "type": improve_changes.PATCH_ARCHIVE_SECTION,
                "source_path": src_rel,
                "section_heading": "Cold Section",
                "archive_path": bad,
                "stub_text": "## Cold Section (archived)\n",
            },
        }
        with pytest.raises(improve_changes.ImproveChangeError):
            improve_changes.write_proposal(proposal)


def test_archive_section_source_path_missing_rejected_at_write(env):
    proposal = {
        "pain_point": "missing_source_test",
        "severity": "low",
        "file_changed": ".claude/commands/does_not_exist.md",
        "patch": {
            "type": improve_changes.PATCH_ARCHIVE_SECTION,
            "source_path": ".claude/commands/does_not_exist.md",
            "section_heading": "X",
            "archive_path": _archive_rel("missing__x.md"),
            "stub_text": "## X (archived)\n",
        },
    }
    with pytest.raises(improve_changes.ImproveChangeError):
        improve_changes.write_proposal(proposal)


# ---------------------------------------------------------------------------
# Edge: source_pre_hash_sha256 mismatch -> stale at apply
# ---------------------------------------------------------------------------


def test_archive_section_source_hash_mismatch_returns_stale(env):
    src = _make_skill_file(env.root, name="hashed.md")
    src_rel = _source_rel(src, env.root)
    bogus_hash = "0" * 64  # never matches the real content
    proposal = {
        "pain_point": "hash_mismatch_test",
        "severity": "low",
        "file_changed": src_rel,
        "patch": {
            "type": improve_changes.PATCH_ARCHIVE_SECTION,
            "source_path": src_rel,
            "section_heading": "Cold Section",
            "archive_path": _archive_rel("hashed__cold.md"),
            "stub_text": "## Cold Section (archived)\n",
            "source_pre_hash_sha256": bogus_hash,
        },
    }
    change_id = improve_changes.write_proposal(proposal)
    result = improve_changes.apply_proposal(change_id)
    assert result["ok"] is False
    assert result["reason"] == "stale"
    assert "sha256" in result["detail"].lower() or "drift" in result["detail"].lower()
    # File untouched.
    assert "Cold content body line 1." in src.read_text(encoding="utf-8")
    assert not (env.root / ".claude" / "_archive" / "hashed__cold.md").exists()


def test_archive_section_source_hash_match_succeeds(env):
    src = _make_skill_file(env.root, name="ok.md")
    src_rel = _source_rel(src, env.root)
    real_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    proposal = {
        "pain_point": "hash_match_test",
        "severity": "low",
        "file_changed": src_rel,
        "patch": {
            "type": improve_changes.PATCH_ARCHIVE_SECTION,
            "source_path": src_rel,
            "section_heading": "Cold Section",
            "archive_path": _archive_rel("ok__cold.md"),
            "stub_text": "## Cold Section (archived)\n",
            "source_pre_hash_sha256": real_hash,
        },
    }
    change_id = improve_changes.write_proposal(proposal)
    result = improve_changes.apply_proposal(change_id)
    assert result["ok"] is True, result


# ---------------------------------------------------------------------------
# Validator: VALID_PATCH_TYPES contains archive_section
# ---------------------------------------------------------------------------


def test_archive_section_in_valid_patch_types():
    assert improve_changes.PATCH_ARCHIVE_SECTION == "archive_section"
    assert "archive_section" in improve_changes.VALID_PATCH_TYPES
