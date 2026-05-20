"""Tests for compute_recent_apply_regressions in scripts/audit_run_efficiency.py.

Loads the script as a module (it lives outside any package), monkeypatches
PROJECT_ROOT to a tmp_path, writes synthetic improve_changes.jsonl rows
covering every filter branch, and asserts the regression/validated split.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = REPO_ROOT / "scripts" / "audit_run_efficiency.py"


@pytest.fixture
def audit_module():
    """Import scripts/audit_run_efficiency.py as a module without requiring it
    to be on PYTHONPATH. Cached across tests in the session is fine — we always
    monkeypatch PROJECT_ROOT per test."""
    spec = importlib.util.spec_from_file_location("audit_run_efficiency", AUDIT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_run_efficiency"] = mod
    spec.loader.exec_module(mod)
    return mod


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_recent_apply_regressions_filters_and_classifies(tmp_path, monkeypatch, audit_module):
    monkeypatch.setattr(audit_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    changes_path = tmp_path / "data" / "improve_changes.jsonl"

    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(days=1))
    old = _iso(now - timedelta(days=30))

    rows = [
        # Should REGRESS: valid_jobs dropped from 20 to 10 (50%, threshold 17)
        {
            "change_id": "imp_regress_vj",
            "status": "applied",
            "timestamp": recent,
            "applied_in_session": "OTHER_SESSION",
            "pre_metrics": {
                "valid_jobs": 20,
                "pct_high_score": 0.5,
                "tokens_per_valid_job": 1000.0,
            },
        },
        # Should VALIDATE: metrics roughly match
        {
            "change_id": "imp_validate_ok",
            "status": "applied",
            "timestamp": recent,
            "applied_in_session": "OTHER_SESSION",
            "pre_metrics": {
                "valid_jobs": 15,
                "pct_high_score": 0.4,
                "tokens_per_valid_job": 1200.0,
            },
        },
        # Skip — no pre_metrics
        {
            "change_id": "imp_no_pre",
            "status": "applied",
            "timestamp": recent,
            "applied_in_session": "OTHER_SESSION",
            "pre_metrics": None,
        },
        # Skip — too old
        {
            "change_id": "imp_too_old",
            "status": "applied",
            "timestamp": old,
            "applied_in_session": "OTHER_SESSION",
            "pre_metrics": {"valid_jobs": 100, "tokens_per_valid_job": 100.0},
        },
        # Skip — reverted already
        {
            "change_id": "imp_already_reverted",
            "status": "applied",
            "timestamp": recent,
            "applied_in_session": "OTHER_SESSION",
            "reverted_at": _iso(now - timedelta(hours=1)),
            "pre_metrics": {"valid_jobs": 50, "tokens_per_valid_job": 500.0},
        },
        # Skip — same session as current audit
        {
            "change_id": "imp_same_session",
            "status": "applied",
            "timestamp": recent,
            "applied_in_session": "CURRENT_SESSION",
            "pre_metrics": {"valid_jobs": 50, "tokens_per_valid_job": 500.0},
        },
        # Should REGRESS: tokens_per_valid_job grew 2x
        {
            "change_id": "imp_regress_tokens",
            "status": "applied",
            "timestamp": _iso(now - timedelta(days=2)),
            "applied_in_session": "OTHER_SESSION",
            "pre_metrics": {
                "valid_jobs": 15,
                "pct_high_score": 0.4,
                "tokens_per_valid_job": 600.0,  # current is 1200 -> 2.0x
            },
        },
        # Skip — non-applied status
        {
            "change_id": "imp_proposed",
            "status": "proposed",
            "timestamp": recent,
            "pre_metrics": {"valid_jobs": 50},
        },
        # Skip — validated_at already set
        {
            "change_id": "imp_already_validated",
            "status": "applied",
            "timestamp": recent,
            "applied_in_session": "OTHER_SESSION",
            "validated_at": _iso(now - timedelta(hours=1)),
            "pre_metrics": {"valid_jobs": 15},
        },
    ]

    with changes_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        # garbage non-JSON line + non-dict line — must be tolerated
        f.write("not a json line\n")
        f.write("[1,2,3]\n")

    current_efficiency = {
        "main_tokens": 18000,
        "valid_jobs": 15,
        "tokens_per_valid_job": 1200.0,
        "cache_hit_rate": 0.6,
        "subagent_token_share": 0.1,
        "pct_high_score": 0.4,
    }

    out = audit_module.compute_recent_apply_regressions(
        current_efficiency, current_session_id="CURRENT_SESSION"
    )

    regression_ids = {r["change_id"] for r in out["regressions"]}
    validated_ids = {v["change_id"] for v in out["validated"]}

    # Eligible rows after all filters: imp_regress_vj, imp_validate_ok, imp_regress_tokens
    assert out["n_changes_checked"] == 3
    assert regression_ids == {"imp_regress_vj", "imp_regress_tokens"}
    assert validated_ids == {"imp_validate_ok"}

    # Excluded change_ids are nowhere in the result.
    excluded = {
        "imp_no_pre", "imp_too_old", "imp_already_reverted",
        "imp_same_session", "imp_proposed", "imp_already_validated",
    }
    assert regression_ids.isdisjoint(excluded)
    assert validated_ids.isdisjoint(excluded)

    # Regression reasons must include the threshold ratio strings.
    by_id = {r["change_id"]: r for r in out["regressions"]}
    vj_reasons = " | ".join(by_id["imp_regress_vj"]["regression_reasons"])
    assert "valid_jobs" in vj_reasons
    assert "0.85x" in vj_reasons
    # pre 20 -> current 15: 15 < 20*0.85=17 so it triggers; expect numbers in message
    assert "20" in vj_reasons and "15" in vj_reasons

    tok_reasons = " | ".join(by_id["imp_regress_tokens"]["regression_reasons"])
    assert "tokens_per_valid_job" in tok_reasons
    assert "1.5x" in tok_reasons

    # Sort order: oldest applied_at first.
    applied_ats = [r["applied_at"] for r in out["regressions"]]
    assert applied_ats == sorted(applied_ats)

    # _note documents the criteria.
    assert "7 days" in out["_note"] or "lookback" in out["_note"].lower() or "applied" in out["_note"]


def test_recent_apply_regressions_missing_file(tmp_path, monkeypatch, audit_module):
    """No improve_changes.jsonl => graceful empty result, no exception."""
    monkeypatch.setattr(audit_module, "PROJECT_ROOT", tmp_path)
    out = audit_module.compute_recent_apply_regressions(
        {"valid_jobs": 10, "pct_high_score": 0.3, "tokens_per_valid_job": 500.0},
        current_session_id="X",
    )
    assert out == {
        "n_changes_checked": 0,
        "regressions": [],
        "validated": [],
        "_note": "no improve_changes.jsonl yet",
    }


def test_recent_apply_regressions_skips_when_current_is_none(tmp_path, monkeypatch, audit_module):
    """If a current metric is None, the corresponding check is skipped — never fabricate."""
    monkeypatch.setattr(audit_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    changes_path = tmp_path / "data" / "improve_changes.jsonl"
    now = datetime.now(timezone.utc)
    row = {
        "change_id": "imp_partial",
        "status": "applied",
        "timestamp": _iso(now - timedelta(hours=2)),
        "applied_in_session": "OTHER",
        # Only pct_high_score is set; valid_jobs and tokens_per_valid_job are None.
        "pre_metrics": {"valid_jobs": None, "pct_high_score": 0.5, "tokens_per_valid_job": None},
    }
    changes_path.write_text(json.dumps(row) + "\n")

    # current pct_high_score=None -> the only comparable check is skipped -> validates.
    out = audit_module.compute_recent_apply_regressions(
        {"valid_jobs": None, "pct_high_score": None, "tokens_per_valid_job": None},
        current_session_id="CURRENT",
    )
    assert out["n_changes_checked"] == 1
    assert [v["change_id"] for v in out["validated"]] == ["imp_partial"]
    assert out["regressions"] == []
