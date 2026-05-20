"""
Item G — LLM-driven nuanced feedback synthesizer.

Three subcommands:

  prepare   — Pull bad/good feedback jobs from the DB, emit a structured JSON
              package containing the evidence sets plus a self-contained
              LLM prompt that asks for hypotheses across four orthogonal axes.
              The script itself does NOT call an LLM. /improve (or any caller)
              feeds the prompt into an LLM stage and writes the result as
              proposals.json.

  validate  — Read proposals.json (LLM output). Run mechanical adversarial
              checks (citation, counter-evidence, confounding, confidence
              downgrade) in Python — never deferred to an LLM. Emit per-
              hypothesis verdicts.

  apply     — Append PASS-verdict hypotheses to one of three fixed top-level
              keys in candidate_info.json: inclinations, disinclinations,
              learn_skills. Invoked only AFTER human approval — never auto.

Design constraint: every gate that protects against LLM hallucination
(sample-size, evidence citation, counter-evidence) runs in Python against
the DB, so the LLM cannot talk its way past it.

Used as soft +5/-5/+3 bias by /fetchjobs scoring — never a hard filter.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from typing import Any

from job_finder.config import load_config, save_config
from job_finder.paths import get_db_path, resolve_active_config_path


SYNTHESIZER_VERSION = "1.0"

VALID_AXES = ("company", "skill", "domain", "problem_type")
VALID_DIRECTIONS = ("inclination", "disinclination")
VALID_CONFIDENCES = ("HIGH", "MED", "LOW")
APPLY_TARGETS = ("inclinations", "disinclinations", "learn_skills")


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _load_jobs(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    cur = conn.execute(
        f"""
        SELECT id, company, title, theme, rationale, description,
               user_feedback, user_weight, status
        FROM jobs
        WHERE {where}
        """,
        params,
    )
    rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "company": r[1] or "",
                "title": r[2] or "",
                "theme": r[3] or "",
                "rationale": r[4] or "",
                # Legacy rows have NULL description — keep None so consumers see the gap.
                "description": r[5],
                "user_feedback": r[6],
                "user_weight": r[7],
                "status": r[8] or "New",
            }
        )
    return out


def _evidence_text(job: dict) -> str:
    """Text against which Python substring checks run. Falls back to rationale
    when description is NULL (legacy rows persisted before Item F)."""
    parts = [
        job.get("description") or "",
        job.get("rationale") or "",
        job.get("theme") or "",
        job.get("title") or "",
        job.get("company") or "",
    ]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------

_LLM_PROMPT_TEMPLATE = """\
You are a calibrated revealed-preference analyst. The user has marked a small
set of jobs as good/bad (or weighted them high/low). Your job is to hypothesize
WHY — across FOUR orthogonal axes — and emit STRICT JSON the validator can check.

# Evidence sets (verbatim, do not paraphrase fields)

## BAD set — genre-NEGATIVE signal
Membership: user_feedback='bad'  OR  (user_weight <= {bad_weight_max} AND status NOT in {{Applied,InProgress,Closed,Won}})  OR  status='NotForMe'
{bad_set_json}

## GOOD set — genre-POSITIVE signal
Membership: user_feedback='good'  OR  user_weight >= {good_weight_min}  OR  status in {{Applied,InProgress,Closed,Won}}
{good_set_json}

# IMPORTANT — application lifecycle ≠ genre preference

A row with status in {{Applied, InProgress, Closed, Won}} is GENRE-POSITIVE even if
the user later set a low weight. The applied-intent is the strongest possible
positive signal: the user invested time to apply. A low weight on such a row
means "do not re-surface this specific job", NOT "I dislike this genre".

If a GOOD-set row has status='Closed' (applied + didn't go through), use it as
strong evidence for inclination hypotheses (skill, domain, problem_type) but do
NOT hypothesize about the company axis using that row — the user already tried
that company. Use status='Won' rows as the strongest evidence for any axis.

status='NotForMe' is the only status that itself signals genre disinclination.

# Axes (orthogonal — hypothesize per axis independently)

1. company   — org size / stage / sector / peer-similarity
2. skill     — recurring skill the user lacks or wants to grow
3. domain    — sub-domain within an approved priority_domain
4. problem_type — IC vs management, applied vs research, productized vs consulting

For each direction (inclination from GOOD set, disinclination from BAD set),
walk the four axes independently.

# Mandatory rules (the Python validator WILL enforce these — failing items are dropped)

- Every hypothesis MUST cite >= 3 job ids from the relevant evidence set.
- If an axis has < 3 supporting jobs, emit a SINGLE object:
    {{"axis": "<axis>", "direction": "<direction>", "pattern": "INSUFFICIENT_DATA",
      "evidence_job_ids": [], "confidence": "LOW", "reasoning": "<axis>: <n> jobs < min_sample"}}
- Do NOT cite a job whose description/rationale/title/company does not actually
  contain the claimed feature (substring or close lexical variant). The validator
  performs case-insensitive substring matching on skills and company names.
- Output a SINGLE JSON array. No prose, no markdown, no code fences.

# Per-hypothesis schema (strict)

{{
  "axis": "company|skill|domain|problem_type",
  "direction": "inclination|disinclination",
  "pattern": "<short noun phrase the validator can substring-match for skill/company; abstract phrase OK for domain/problem_type>",
  "evidence_job_ids": ["<id>", "<id>", "<id>", ...],
  "confidence": "HIGH|MED|LOW",
  "reasoning": "<<= 200 chars, mechanism-level, no hedging>"
}}

Confidence calibration:
- HIGH  — >= 5 evidence jobs, no obvious counter-example
- MED   — 3-4 evidence jobs, no obvious counter-example
- LOW   — 3 jobs OR partial contradiction

Return ONLY the JSON array."""


_VALIDATOR_INSTRUCTIONS = (
    "After the LLM emits proposals.json (a JSON array of hypothesis objects), "
    "run: `uv run python scripts/synthesize_feedback_patterns.py validate "
    "--proposals proposals.json`. The validator performs mechanical checks in "
    "Python against the DB: (1) every cited job_id must exist; (2) for SKILL "
    "and COMPANY axes, the claimed pattern must appear as a case-insensitive "
    "substring in description/rationale/title/company of at least one cited "
    "job; (3) every disinclination is scanned against the GOOD set for counter-"
    "evidence (and vice versa); (4) if >=80% of the bad set shares an already-"
    "known trait from candidate_info.json (e.g., one priority_domain), other-"
    "axis hypotheses are flagged CONFOUNDED; (5) confidence is downgraded one "
    "step per issue (HIGH+small sample -> MED, MED+contradiction -> LOW, "
    "LOW+any issue -> DROPPED). Output is per-hypothesis verdicts (PASS / "
    "EVIDENCE_FALSIFIED / EVIDENCE_MISSING / CONTRADICTED / CONFOUNDED / "
    "DROPPED). Only PASS hypotheses are eligible for `apply`, and only after "
    "human approval through /improve."
)


def cmd_prepare(args: argparse.Namespace) -> dict:
    """Build bad/good sets with status disentanglement.

    Critical rule: status IN POSITIVE_GENRE wins over a low user_weight when both
    are present. A user who applied to a job (status='Closed') and set weight=20
    afterwards meant "don't re-surface THIS exact row" — not "I dislike this genre."
    The applied-intent dominates the genre classification.
    """
    # Avoid project-package import to keep this script standalone-runnable; mirror the constants.
    POSITIVE_GENRE = ("Applied", "InProgress", "Closed", "Won")
    NEGATIVE_GENRE = ("NotForMe",)
    pos_ph = ",".join("?" * len(POSITIVE_GENRE))
    neg_ph = ",".join("?" * len(NEGATIVE_GENRE))

    conn = sqlite3.connect(get_db_path())
    try:
        # BAD set membership:
        #   feedback='bad'  OR  (weight <= max AND status NOT in POSITIVE_GENRE)  OR  status in NEGATIVE_GENRE
        # The middle clause is the disentanglement: a low weight on a Closed/Applied
        # row is NOT a genre disinclination — it's "stop showing me this exact row."
        bad_jobs = _load_jobs(
            conn,
            f"""(
                user_feedback = 'bad'
                OR (user_weight IS NOT NULL AND user_weight <= ? AND status NOT IN ({pos_ph}))
                OR status IN ({neg_ph})
            )""",
            (args.bad_weight_max, *POSITIVE_GENRE, *NEGATIVE_GENRE),
        )
        # GOOD set membership:
        #   feedback='good'  OR  weight >= min  OR  status in POSITIVE_GENRE
        # The applied/closed/won rows are always positive genre signal regardless of weight.
        good_jobs = _load_jobs(
            conn,
            f"""(
                user_feedback = 'good'
                OR (user_weight IS NOT NULL AND user_weight >= ?)
                OR status IN ({pos_ph})
            )""",
            (args.good_weight_min, *POSITIVE_GENRE),
        )
    finally:
        conn.close()

    # Status breakdown for transparency — the consumer can see WHY each row landed where it did.
    def _status_breakdown(rows: list[dict]) -> dict:
        out: dict[str, int] = {}
        for j in rows:
            out[j.get("status") or "New"] = out.get(j.get("status") or "New", 0) + 1
        return out

    bad_sufficient = len(bad_jobs) >= args.min_sample
    good_sufficient = len(good_jobs) >= args.min_sample

    prompt = _LLM_PROMPT_TEMPLATE.format(
        bad_weight_max=args.bad_weight_max,
        good_weight_min=args.good_weight_min,
        bad_set_json=json.dumps(bad_jobs, indent=2, ensure_ascii=False),
        good_set_json=json.dumps(good_jobs, indent=2, ensure_ascii=False),
    )

    payload: dict[str, Any] = {
        "synthesizer_version": SYNTHESIZER_VERSION,
        "prepared_at": _iso_now(),
        "thresholds": {
            "bad_weight_max": args.bad_weight_max,
            "good_weight_min": args.good_weight_min,
            "min_sample": args.min_sample,
        },
        "bad_set": {
            "count": len(bad_jobs),
            "jobs": bad_jobs,
            "sufficient_for_synthesis": bad_sufficient,
            "status_breakdown": _status_breakdown(bad_jobs),
        },
        "good_set": {
            "count": len(good_jobs),
            "jobs": good_jobs,
            "sufficient_for_synthesis": good_sufficient,
            "status_breakdown": _status_breakdown(good_jobs),
        },
        "_disentanglement_note": (
            "Set membership disentangles application lifecycle from genre preference. "
            "A row with status in {Applied, InProgress, Closed, Won} is GENRE-POSITIVE "
            "regardless of user_weight — the user's applied-intent dominates. A low weight "
            "on a Closed row means 'don't re-surface this specific row' not 'I dislike this genre'. "
            "status='NotForMe' is the only genre-negative status. The LLM prompt below also "
            "tells the model to treat these statuses accordingly."
        ),
        "llm_prompt_template": prompt,
        "validator_instructions": _VALIDATOR_INSTRUCTIONS,
    }

    if not bad_sufficient and not good_sufficient:
        payload["skip_reason"] = "insufficient_bad_data; insufficient_good_data"
    elif not bad_sufficient:
        payload["skip_reason"] = "insufficient_bad_data"
    elif not good_sufficient:
        payload["skip_reason"] = "insufficient_good_data"

    return payload


# ---------------------------------------------------------------------------
# Subcommand: validate
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return (s or "").casefold()


def _lookup_jobs_by_ids(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    # Chunk to keep parameter count sane.
    out: dict[str, dict] = {}
    CHUNK = 200
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        placeholders = ",".join(["?"] * len(chunk))
        cur = conn.execute(
            f"""
            SELECT id, company, title, theme, rationale, description,
                   user_feedback, user_weight, status
            FROM jobs
            WHERE id IN ({placeholders})
            """,
            tuple(chunk),
        )
        for r in cur.fetchall():
            out[r[0]] = {
                "id": r[0],
                "company": r[1] or "",
                "title": r[2] or "",
                "theme": r[3] or "",
                "rationale": r[4] or "",
                "description": r[5],
                "user_feedback": r[6],
                "user_weight": r[7],
                "status": r[8] or "New",
            }
    return out


def _validate_evidence_citation(
    hyp: dict, jobs_by_id: dict[str, dict]
) -> tuple[str | None, str, list[str]]:
    """Return (verdict_or_None, detail, missing_ids).

    For SKILL and COMPANY axes: pattern must substring-match against at least
    one cited job's evidence text. For DOMAIN and PROBLEM_TYPE: phrasings are
    abstract — defer to human approval (UNVALIDATABLE).
    """
    cited_ids: list[str] = list(hyp.get("evidence_job_ids") or [])
    if not cited_ids:
        return "EVIDENCE_MISSING", "no evidence_job_ids cited", []

    missing = [jid for jid in cited_ids if jid not in jobs_by_id]
    if missing:
        return (
            "EVIDENCE_MISSING",
            f"{len(missing)}/{len(cited_ids)} cited ids not in DB: {missing[:3]}",
            missing,
        )

    axis = hyp.get("axis")
    pattern = _norm(hyp.get("pattern", ""))
    if not pattern:
        return "EVIDENCE_FALSIFIED", "empty pattern", []

    if axis in ("skill", "company"):
        hits = []
        for jid in cited_ids:
            text = _norm(_evidence_text(jobs_by_id[jid]))
            if pattern in text:
                hits.append(jid)
        if not hits:
            return (
                "EVIDENCE_FALSIFIED",
                f"pattern '{hyp.get('pattern')}' not found in any of {len(cited_ids)} cited jobs",
                [],
            )
        if axis == "company" and len(hits) < len(cited_ids):
            # Trivial check should always pass — log partial mismatch.
            return None, f"company substring matched {len(hits)}/{len(cited_ids)}", []
        return None, f"substring matched {len(hits)}/{len(cited_ids)} cited jobs", []

    # domain / problem_type: not validatable mechanically.
    return None, "abstract axis — substring check skipped (UNVALIDATABLE)", []


def _check_counter_evidence(
    hyp: dict, opposite_set: list[dict]
) -> tuple[bool, list[str]]:
    """Return (contradicted, contradicting_job_ids).

    Only meaningful for SKILL / COMPANY (substring-matchable). For DOMAIN /
    PROBLEM_TYPE we cannot mechanically detect counter-evidence.
    """
    axis = hyp.get("axis")
    if axis not in ("skill", "company"):
        return False, []
    pattern = _norm(hyp.get("pattern", ""))
    if not pattern:
        return False, []
    hits = []
    for job in opposite_set:
        if pattern in _norm(_evidence_text(job)):
            hits.append(job["id"])
    return (len(hits) > 0), hits


def _detect_confound(bad_jobs: list[dict], known_traits: list[str]) -> str | None:
    """If >=80% of bad jobs share an already-known priority_domain or peer_company,
    return the shared trait label (for flagging other-axis hypotheses)."""
    if not bad_jobs or not known_traits:
        return None
    threshold = 0.8 * len(bad_jobs)
    for trait in known_traits:
        norm = _norm(trait)
        if not norm:
            continue
        hits = sum(1 for j in bad_jobs if norm in _norm(_evidence_text(j)))
        if hits >= threshold:
            return trait
    return None


def _downgrade(current: str, steps: int) -> str | None:
    order = ["HIGH", "MED", "LOW", None]
    if current not in ("HIGH", "MED", "LOW"):
        return None
    idx = order.index(current) + steps
    if idx >= len(order):
        return None
    return order[idx]


def cmd_validate(args: argparse.Namespace) -> list[dict]:
    with open(args.proposals, "r", encoding="utf-8") as f:
        proposals = json.load(f)
    if not isinstance(proposals, list):
        print(
            json.dumps(
                {
                    "error": "proposals.json must be a JSON array of hypothesis objects",
                    "received_type": type(proposals).__name__,
                }
            )
        )
        sys.exit(2)

    # Determine current bad / good sets to support counter-evidence checks.
    # Reuse the same thresholds AND status disentanglement as cmd_prepare so the
    # validator partitions exactly the same rows. Without this, a Closed-with-low-
    # weight row would silently land in bad_jobs here while cmd_prepare correctly
    # routed it to good_jobs — counter-evidence checks would then falsely flag
    # disinclination hypotheses cited from that row.
    POSITIVE_GENRE = ("Applied", "InProgress", "Closed", "Won")
    NEGATIVE_GENRE = ("NotForMe",)
    pos_ph = ",".join("?" * len(POSITIVE_GENRE))
    neg_ph = ",".join("?" * len(NEGATIVE_GENRE))
    conn = sqlite3.connect(get_db_path())
    try:
        bad_jobs = _load_jobs(
            conn,
            f"""(
                user_feedback = 'bad'
                OR (user_weight IS NOT NULL AND user_weight <= ? AND status NOT IN ({pos_ph}))
                OR status IN ({neg_ph})
            )""",
            (args.bad_weight_max, *POSITIVE_GENRE, *NEGATIVE_GENRE),
        )
        good_jobs = _load_jobs(
            conn,
            f"""(
                user_feedback = 'good'
                OR (user_weight IS NOT NULL AND user_weight >= ?)
                OR status IN ({pos_ph})
            )""",
            (args.good_weight_min, *POSITIVE_GENRE),
        )

        all_cited: set[str] = set()
        for h in proposals:
            for jid in (h.get("evidence_job_ids") or []):
                all_cited.add(jid)
        jobs_by_id = _lookup_jobs_by_ids(conn, list(all_cited))
    finally:
        conn.close()

    # Load known traits from candidate_info.json for confounding check.
    try:
        cfg = load_config()
        known_traits = list(cfg.get("priority_domains") or []) + list(
            cfg.get("peer_companies") or []
        )
    except Exception:
        known_traits = []

    confounder = _detect_confound(bad_jobs, known_traits)

    verdicts: list[dict] = []
    for hyp in proposals:
        # Skip the placeholder INSUFFICIENT_DATA markers the prompt may emit.
        if hyp.get("pattern") == "INSUFFICIENT_DATA":
            verdicts.append(
                {
                    "hypothesis": hyp,
                    "verdict": "DROPPED",
                    "verdict_detail": "INSUFFICIENT_DATA marker — no proposal",
                    "final_confidence": None,
                    "ready_for_human_approval": False,
                }
            )
            continue

        # Shape sanity.
        axis = hyp.get("axis")
        direction = hyp.get("direction")
        confidence = hyp.get("confidence")
        evidence_ids = hyp.get("evidence_job_ids") or []
        if axis not in VALID_AXES or direction not in VALID_DIRECTIONS or confidence not in VALID_CONFIDENCES:
            verdicts.append(
                {
                    "hypothesis": hyp,
                    "verdict": "DROPPED",
                    "verdict_detail": (
                        f"shape invalid (axis={axis} direction={direction} confidence={confidence})"
                    ),
                    "final_confidence": None,
                    "ready_for_human_approval": False,
                }
            )
            continue

        if len(evidence_ids) < 3:
            verdicts.append(
                {
                    "hypothesis": hyp,
                    "verdict": "DROPPED",
                    "verdict_detail": f"only {len(evidence_ids)} evidence ids; need >= 3",
                    "final_confidence": None,
                    "ready_for_human_approval": False,
                }
            )
            continue

        # 1. Evidence citation check.
        cite_verdict, cite_detail, _missing = _validate_evidence_citation(hyp, jobs_by_id)
        if cite_verdict in ("EVIDENCE_MISSING", "EVIDENCE_FALSIFIED"):
            verdicts.append(
                {
                    "hypothesis": hyp,
                    "verdict": cite_verdict,
                    "verdict_detail": cite_detail,
                    "final_confidence": None,
                    "ready_for_human_approval": False,
                }
            )
            continue

        # 2. Counter-evidence check.
        opposite = good_jobs if direction == "disinclination" else bad_jobs
        contradicted, contra_ids = _check_counter_evidence(hyp, opposite)

        # 3. Confounding check.
        confounded = (
            confounder is not None
            and axis != "domain"
            and _norm(hyp.get("pattern", "")) != _norm(confounder)
        )

        # 4. Confidence downgrade.
        issues = 0
        partial_contra = False
        if contradicted:
            # Single counter-example = partial; >=2 = full contradiction.
            if len(contra_ids) >= 2:
                verdicts.append(
                    {
                        "hypothesis": hyp,
                        "verdict": "CONTRADICTED",
                        "verdict_detail": (
                            f"{len(contra_ids)} contradicting jobs in opposite set: "
                            f"{contra_ids[:5]}"
                        ),
                        "final_confidence": None,
                        "ready_for_human_approval": False,
                    }
                )
                continue
            partial_contra = True
            issues += 1
        if confounded:
            issues += 1
        if len(evidence_ids) == 3:
            issues += 1  # smallest legal sample

        final_conf = _downgrade(confidence, issues) if issues else confidence
        if final_conf is None:
            verdicts.append(
                {
                    "hypothesis": hyp,
                    "verdict": "DROPPED",
                    "verdict_detail": (
                        f"LOW + {issues} issue(s) — dropped per confidence downgrade rule"
                    ),
                    "final_confidence": None,
                    "ready_for_human_approval": False,
                }
            )
            continue

        # PASS (possibly with CONFOUNDED note).
        if confounded:
            primary_verdict = "CONFOUNDED"
            detail = (
                f"shared trait '{confounder}' present in >=80% of bad set; "
                f"pattern may be downstream of it. {cite_detail}"
            )
            ready = False  # surface to human; do not auto-stage
        else:
            primary_verdict = "PASS"
            detail = cite_detail + (
                f"; partial-contradiction (1 counter-example in opposite set: {contra_ids[:1]})"
                if partial_contra
                else ""
            )
            ready = True

        verdicts.append(
            {
                "hypothesis": hyp,
                "verdict": primary_verdict,
                "verdict_detail": detail,
                "final_confidence": final_conf,
                "ready_for_human_approval": ready,
            }
        )

    return verdicts


# ---------------------------------------------------------------------------
# Subcommand: apply
# ---------------------------------------------------------------------------

_PAIN_POINT_BY_TARGET = {
    "inclinations": "PATTERN_INCLINATION_FOUND",
    "disinclinations": "PATTERN_DISINCLINATION_FOUND",
    "learn_skills": "PATTERN_LEARN_SKILL_FOUND",
}


def _severity_for_target(target: str) -> str:
    return "LOW" if target == "learn_skills" else "MEDIUM"


def _conf_str(conf) -> str:
    try:
        return f"{float(conf):.2f}"
    except (TypeError, ValueError):
        return str(conf or "?")


def cmd_apply(args: argparse.Namespace) -> dict:
    if args.to not in APPLY_TARGETS:
        print(
            json.dumps({"error": f"--to must be one of {APPLY_TARGETS}", "got": args.to})
        )
        sys.exit(2)

    with open(args.validated, "r", encoding="utf-8") as f:
        validated = json.load(f)
    if not isinstance(validated, list):
        print(json.dumps({"error": "validated.json must be a JSON array of verdict objects"}))
        sys.exit(2)

    write_via_proposals = not getattr(args, "immediate", False)

    cfg = load_config()
    existing = list(cfg.get(args.to) or [])

    appended: list[dict] = []
    staged: list[dict] = []
    skipped: list[dict] = []
    now = _iso_now()

    if write_via_proposals:
        from job_finder import improve_changes

    for v in validated:
        if v.get("verdict") != "PASS":
            skipped.append({"reason": v.get("verdict"), "hypothesis": v.get("hypothesis")})
            continue
        hyp = v.get("hypothesis") or {}
        final_conf = v.get("final_confidence") or hyp.get("confidence")

        if args.to == "learn_skills":
            entry = {
                "skill": hyp.get("pattern", ""),
                "source_jobs": list(hyp.get("evidence_job_ids") or []),
                "confidence": final_conf,
                "added_at": now,
            }
            entry_label = entry["skill"]
        else:
            # inclinations / disinclinations — enforce direction alignment.
            expected_dir = "inclination" if args.to == "inclinations" else "disinclination"
            if hyp.get("direction") != expected_dir:
                skipped.append(
                    {
                        "reason": f"direction mismatch (got {hyp.get('direction')}, target {args.to})",
                        "hypothesis": hyp,
                    }
                )
                continue
            entry = {
                "pattern": hyp.get("pattern", ""),
                "evidence_job_ids": list(hyp.get("evidence_job_ids") or []),
                "confidence": final_conf,
                "source_axis": hyp.get("axis"),
                "added_at": now,
            }
            entry_label = entry["pattern"]

        if write_via_proposals:
            proposal = {
                "pain_point": _PAIN_POINT_BY_TARGET.get(args.to, "PATTERN_UNKNOWN"),
                "severity": _severity_for_target(args.to),
                "summary": (
                    f"Append {args.to[:-1] if args.to.endswith('s') else args.to}: "
                    f"'{entry_label}' (confidence {_conf_str(final_conf)})"
                ),
                "evidence": {
                    "metric": f"validated.{args.to}",
                    "value": 1,
                    "source_axis": hyp.get("axis"),
                    "evidence_job_ids": list(hyp.get("evidence_job_ids") or []),
                },
                "file_changed": "data/candidate_info.json",
                "patch": {
                    "type": "json_append",
                    "key_path": [args.to],
                    "appended_items": [entry],
                },
            }
            change_id = improve_changes.write_proposal(proposal)
            staged.append({**entry, "change_id": change_id})
        else:
            existing.append(entry)
            appended.append(entry)

    if write_via_proposals:
        return {
            "applied_to": args.to,
            "config_path": resolve_active_config_path(),
            "mode": "proposals",
            "staged_count": len(staged),
            "skipped_count": len(skipped),
            "staged": staged,
            "skipped": skipped,
        }

    cfg[args.to] = existing
    save_config(cfg)

    return {
        "applied_to": args.to,
        "config_path": resolve_active_config_path(),
        "mode": "immediate",
        "appended_count": len(appended),
        "skipped_count": len(skipped),
        "appended": appended,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Synthesize revealed-preference patterns from user feedback (Item G)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prepare", help="Build evidence bundle + LLM prompt.")
    pp.add_argument("--bad-weight-max", type=int, default=30)
    pp.add_argument("--good-weight-min", type=int, default=70)
    pp.add_argument("--min-sample", type=int, default=3)

    pv = sub.add_parser("validate", help="Run adversarial checks on LLM proposals.")
    pv.add_argument("--proposals", required=True, help="Path to proposals.json")
    pv.add_argument("--bad-weight-max", type=int, default=30)
    pv.add_argument("--good-weight-min", type=int, default=70)

    pa = sub.add_parser(
        "apply",
        help=(
            "Stage PASS-verdict hypotheses as proposals in data/improve_proposals.jsonl "
            "(user reviews + approves in Streamlit). Pass --immediate to bypass the "
            "queue and write directly to candidate_info.json (legacy behavior)."
        ),
    )
    pa.add_argument("--validated", required=True, help="Path to validated.json (verdict array)")
    pa.add_argument("--to", required=True, choices=APPLY_TARGETS)
    pa.add_argument(
        "--immediate",
        action="store_true",
        default=False,
        help=(
            "Bypass the proposal queue and write directly to candidate_info.json. "
            "Default behavior stages a proposal per entry for UI approval."
        ),
    )

    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.cmd == "prepare":
        out = cmd_prepare(args)
    elif args.cmd == "validate":
        out = cmd_validate(args)
    elif args.cmd == "apply":
        out = cmd_apply(args)
    else:  # pragma: no cover - argparse enforces required=True
        raise SystemExit(2)

    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
