#!/usr/bin/env python3
"""
Audit token + latency efficiency of the most recent /fetchjobs session.

Side effects: writes JSON to stdout AND (by default, suppressed with --no-persist)
appends a per-session row to data/skill_section_usage.jsonl powering the 3-run
cold-section tracker. Never makes API calls. Never invents paths, token counts,
or waste classifications. If verification fails, emits an audit_failed marker
and exits — better silent than guessing.

`compute_recent_apply_regressions` reads data/improve_changes.jsonl read-only
to flag recently-applied compactions whose current-run metrics regressed vs.
their pre-apply snapshot; surfaces as `auto_revert_candidates` in the output.

Usage:
  uv run python scripts/audit_run_efficiency.py [--last-fetchjobs] [--verbose] [--no-persist]
  uv run python scripts/audit_run_efficiency.py --session <session-id>

Inputs:
  - data/last_session.json (written by /fetchjobs; see plan Item D)
  - Main session JSONL at the path it points to
  - Subagent JSONL files in the directory it points to
  - data/pruned_history.jsonl (optional; cross-ref for pre_known_dead bucket)
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Schema-drift defense. Bump when CC's JSONL format changes structurally.
# ----------------------------------------------------------------------------
EXPECTED_JSONL_SCHEMA_VERSION = 1
AUDIT_VERSION = "1.0"

# Top-level JSONL keys we expect to see on the bulk of records. If <60% of
# sampled lines have any of these, we treat the file as unrecognized.
SCHEMA_SENTINEL_KEYS = {"type", "message", "usage", "timestamp"}

# ----------------------------------------------------------------------------
# Project paths (resolved at runtime; never hardcoded session ids).
# ----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAST_SESSION_PATH = PROJECT_ROOT / "data" / "last_session.json"
PRUNED_HISTORY_PATH = PROJECT_ROOT / "data" / "pruned_history.jsonl"
RUN_DIAGNOSTICS_PATH = PROJECT_ROOT / "data" / "run_diagnostics.jsonl"
SKILL_SECTION_USAGE_PATH = PROJECT_ROOT / "data" / "skill_section_usage.jsonl"
COLD_SECTIONS_CACHE_PATH = PROJECT_ROOT / "data" / "cold_sections_cache.json"

# Cap how many lines we read from growing JSONL files before slicing to the
# requested window. Prevents unbounded memory growth as history accumulates.
_MAX_DIAGNOSTIC_LINES = 50
_MAX_SECTION_USAGE_LINES = 10

# Window (days) over which an applied compaction is auto-revert eligible. After
# this many days, /improve treats it as "settled" and stops watching for regression.
APPLY_REGRESSION_LOOKBACK_DAYS = 7
# Tolerance ratios for the three regression checks. Symmetric in spirit: tokens
# can grow by up to 1.5x; quality metrics can drop to at most 0.85x of pre.
APPLY_REGRESSION_VALID_JOBS_RATIO = 0.85
APPLY_REGRESSION_PCT_HIGH_SCORE_RATIO = 0.85
APPLY_REGRESSION_TOKENS_PER_VJ_RATIO = 1.5

# Number of prior runs to aggregate for percentile-based pain-point thresholds.
PRIORS_WINDOW = 5
# Minimum priors required before TOKEN_*/MAIN_AGENT_* pain-points may fire.
PRIORS_MIN_FOR_THRESHOLDS = 3
# Within-run subagent outlier threshold: tokens-per-tool-call ≥ this × median.
SUBAGENT_OUTLIER_RATIO = 2.0
# main_tokens floor below which cache_hit_rate signals are too noisy to act on.
CACHE_MISS_MIN_MAIN_TOKENS = 20000

# --- Skill-section hot-section tracker ---
# Window of recent runs over which to roll up section references when deciding
# cold-status. Set to 1 for immediate feedback — token preservation prioritized
# over false-positive protection. Bump to 3 once a stable workflow emerges if
# you want higher-confidence (multi-run-cold) candidates; persistence captures
# the data either way so you can re-roll up at any window without backfilling.
SKILL_SECTION_USAGE_WINDOW = 1
# Floor below which a section is too small to be worth flagging even if cold.
SKILL_SECTION_MIN_BYTES = 500
# Minimum fingerprints required for a section to be measurable. Sections with
# fewer backtick identifiers cannot be tracked reliably and are excluded.
SKILL_SECTION_MIN_FINGERPRINTS = 3
# Reference-count ceiling: a section referenced more than this across the window
# is considered "warm" regardless of its bytes/ref ratio.
SKILL_SECTION_WARM_THRESHOLD_REFS = 1
# Glob patterns for skill/command files to scan. Discoverable, not hardcoded.
SKILL_FILE_GLOBS = [
    ".claude/commands/*.md",
    ".cursor/skills/*/SKILL.md",
    ".cursor/rules/*.mdc",
]


# ----------------------------------------------------------------------------
# Session-marker resolution. Honest about every failure mode.
# ----------------------------------------------------------------------------
def _encoded_cwd_dirname(cwd: Path) -> str:
    """Claude Code stores sessions under ~/.claude/projects/<encoded-cwd>/.
    The encoding rule it uses: replace '/' with '-' on the absolute path."""
    return str(cwd).replace("/", "-")


def resolve_paths_from_session(session_id: str) -> dict:
    """Locate the main JSONL + subagent dir for a given session id by scanning
    the conventional CC locations. Never invent a path; if not found, say so."""
    encoded = _encoded_cwd_dirname(PROJECT_ROOT)
    projects_dir = Path.home() / ".claude" / "projects" / encoded
    main_jsonl = projects_dir / f"{session_id}.jsonl"
    # subagent files live in /private/tmp/claude-<uid>/<encoded>/<session>/tasks/
    # but there are also symlinks in /private/tmp/claude-*/.../tasks/. Scan both.
    candidate_subagent_dirs = []
    tmp_root = Path("/private/tmp")
    if tmp_root.exists():
        for entry in tmp_root.iterdir():
            if not entry.name.startswith("claude-"):
                continue
            cand = entry / encoded / session_id / "tasks"
            if cand.exists():
                candidate_subagent_dirs.append(cand)
    return {
        "session_id": session_id,
        "main_jsonl_path": str(main_jsonl) if main_jsonl.exists() else None,
        "subagent_dir": str(candidate_subagent_dirs[0]) if candidate_subagent_dirs else None,
    }


def load_session_marker(session_override: str | None) -> dict:
    """Returns dict with keys: detected, session_id, main_jsonl_path, subagent_dir,
    and possibly 'reason' if detection failed. Caller must check 'detected'."""
    if session_override:
        info = resolve_paths_from_session(session_override)
        info["detected"] = bool(info["main_jsonl_path"])
        if not info["detected"]:
            info["reason"] = f"no JSONL found for session_id={session_override}"
        return info
    if not LAST_SESSION_PATH.exists():
        return {"detected": False, "reason": "data/last_session.json does not exist"}
    try:
        marker = json.loads(LAST_SESSION_PATH.read_text())
    except Exception as e:
        return {"detected": False, "reason": f"failed to parse last_session.json: {e}"}
    if not marker.get("detected"):
        return {"detected": False, "reason": marker.get("reason", "marker says detected=false")}
    return marker


# ----------------------------------------------------------------------------
# JSONL parsing — deliberately structural, not assuming any specific message body.
# ----------------------------------------------------------------------------
def iter_jsonl(path: Path):
    """Yield only dict records. Some JSONLs contain bare strings/null lines
    (e.g. subagent output streams) — silently skip those rather than crash."""
    with path.open() as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                ob = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ob, dict):
                continue
            yield i, ob


def check_schema(path: Path) -> dict:
    """Returns {ok: bool, sample_top_keys: [...], match_ratio: float}."""
    sample_top_keys = []
    matched = 0
    total = 0
    for _, ob in iter_jsonl(path):
        total += 1
        if total <= 5:
            sample_top_keys.append(sorted(ob.keys())[:10])
        if SCHEMA_SENTINEL_KEYS.intersection(ob.keys()):
            matched += 1
        if total >= 50:
            break
    ratio = matched / total if total else 0.0
    return {"ok": ratio >= 0.6, "sample_top_keys": sample_top_keys, "match_ratio": ratio, "lines_checked": total}


def _sum_turn_tokens(usage: dict) -> int:
    """Per-turn input tokens INCLUDE cached read/creation; those are the real
    bytes paid for inference context. Output tokens are added separately."""
    if not usage:
        return 0
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
    )


def _break_turn_tokens(usage: dict) -> dict:
    """Per-turn breakdown matching the Anthropic API usage block. Useful for
    populating the run-diagnostics token fields without re-parsing the JSONL."""
    if not usage:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    return {
        "input": int(usage.get("input_tokens", 0) or 0),
        "output": int(usage.get("output_tokens", 0) or 0),
        "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation": int(usage.get("cache_creation_input_tokens", 0) or 0),
    }


# ----------------------------------------------------------------------------
# Percentile helpers. Honest about small-N: p90 collapses to max below N=10
# because interpolated percentiles on tiny samples are just noise dressed up.
# ----------------------------------------------------------------------------
def _p50(xs):
    return statistics.median(xs) if xs else None


def _p90(xs):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) >= 10:
        idx = 0.9 * (len(s) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)
    return s[-1]


# ----------------------------------------------------------------------------
# Cross-run priors from run_diagnostics.jsonl. Used to ground TOKEN_*/MAIN_AGENT_*
# pain-points in observed history rather than hand-tuned thresholds.
#
# Strategy: read the last (PRIORS_WINDOW + 1) entries. The most recent entry is
# treated as the CURRENT run and excluded from the prior set — we want priors
# to be what we measure against, not what we baseline from.
# ----------------------------------------------------------------------------
def _load_recent_diagnostics(n: int) -> list[dict]:
    if not RUN_DIAGNOSTICS_PATH.exists():
        return []
    out = []
    try:
        lines = RUN_DIAGNOSTICS_PATH.read_text().splitlines()
        # Cap how many lines we parse to bound memory as the file grows.
        for line in lines[-_MAX_DIAGNOSTIC_LINES:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out[-n:]


def _row_total_tokens(row: dict) -> int:
    return (
        int(row.get("input_tokens", 0) or 0)
        + int(row.get("output_tokens", 0) or 0)
        + int(row.get("cache_tokens", 0) or 0)
    )


def _row_has_token_data(row: dict) -> bool:
    """True iff the row has any of the token fields populated. Older diagnostics
    rows predate token capture and must NOT be treated as 'zero tokens used'."""
    for k in ("input_tokens", "output_tokens", "cache_tokens"):
        if row.get(k) is not None:
            return True
    return False


def _row_tokens_per_valid_job(row: dict):
    vj = int(row.get("valid_jobs", 0) or 0)
    if vj <= 0 or not _row_has_token_data(row):
        return None
    total = _row_total_tokens(row)
    if total <= 0:
        return None
    return total / vj


def _row_cache_utilization(row: dict):
    # Approximate: run_diagnostics persists only aggregate cache_tokens, not the
    # read/creation split. So this is utilization (cache share of total), not
    # the true hit_rate (cache_read / input-side). For trend purposes only.
    if not _row_has_token_data(row):
        return None
    total = _row_total_tokens(row)
    if total <= 0:
        return None
    return int(row.get("cache_tokens", 0) or 0) / total


def _row_pct_high_score(row: dict):
    vj = int(row.get("valid_jobs", 0) or 0)
    if vj <= 0:
        return None
    sd = row.get("score_distribution") or {}
    return int(sd.get("90-100", 0) or 0) / vj


def compute_priors_from_diagnostics(window: int = PRIORS_WINDOW) -> dict:
    """Aggregate efficiency metrics across the last `window` prior runs.

    Excludes the most recent diagnostics row (assumed to be the current run).
    Skips rows with valid_jobs <= 0 from ratio metrics — those are degenerate
    yield events that would distort the per-job baseline.

    Returns priors block ready to drop into the audit output. Always defined;
    `n_priors_used` tells the caller whether thresholds may fire.
    """
    rows = _load_recent_diagnostics(window + 1)
    if len(rows) <= 1:
        return {"n_priors_seen": 0, "n_priors_used": 0, "skip_reason": "fewer than 2 diagnostics rows"}
    priors = rows[:-1][-window:]
    if not priors:
        return {"n_priors_seen": 0, "n_priors_used": 0, "skip_reason": "no priors after excluding current"}

    priors_with_tokens = [r for r in priors if _row_has_token_data(r)]

    tpvj = [v for v in (_row_tokens_per_valid_job(r) for r in priors_with_tokens) if v is not None]
    mt = [_row_total_tokens(r) for r in priors_with_tokens if _row_total_tokens(r) > 0]
    cu = [v for v in (_row_cache_utilization(r) for r in priors_with_tokens) if v is not None]
    vj = [int(r.get("valid_jobs", 0) or 0) for r in priors]
    phs = [v for v in (_row_pct_high_score(r) for r in priors) if v is not None]

    return {
        "n_priors_seen": len(priors),
        # n_priors_used = priors with usable token data. Pain-point triggers
        # MUST key off this field, not n_priors_seen — older rows predate token
        # capture and must not be treated as a zero-token baseline.
        "n_priors_used": len(priors_with_tokens),
        "window": window,
        "tokens_per_valid_job_p50": _p50(tpvj),
        "tokens_per_valid_job_p90": _p90(tpvj),
        "main_tokens_p50": _p50(mt),
        "main_tokens_p90": _p90(mt),
        "cache_utilization_p50": _p50(cu),
        "valid_jobs_p50": _p50(vj),
        "pct_high_score_p50": _p50(phs),
        "_note": (
            "cache_utilization here is cache_tokens / total_tokens from "
            "run_diagnostics (aggregate cache_tokens). True Anthropic cache "
            "hit_rate (cache_read / input-side) is only available on the "
            "current run via the session JSONL's usage block."
        ),
    }


def _extract_tool_result_text(c: dict) -> str:
    """tool_result.content can be str or list[{type:text,text:...}]. Return body."""
    cc = c.get("content")
    if isinstance(cc, str):
        return cc
    if isinstance(cc, list):
        parts = []
        for item in cc:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", "") or "")
        return "\n".join(parts)
    return ""


def parse_main_session(path: Path) -> dict:
    """Walk the main JSONL once. Build:
      - turns: list of {idx, line, ts, tokens, tool_uses, missing_usage}
      - tool_use_index: id → {turn_idx, name, input}
      - tool_results: id → {body, line}
      - anomalies: list[str]
    """
    turns = []
    tool_use_index = {}
    tool_results = {}
    anomalies = []
    first_ts = None
    last_ts = None
    tokens_breakdown = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    for line_no, ob in iter_jsonl(path):
        ts = ob.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        t = ob.get("type")
        msg = ob.get("message") or {}
        content = msg.get("content")

        if t == "assistant":
            usage = msg.get("usage") or {}
            tokens = _sum_turn_tokens(usage)
            for k, v in _break_turn_tokens(usage).items():
                tokens_breakdown[k] += v
            tool_uses_in_turn = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tu = {
                            "id": c.get("id"),
                            "name": c.get("name"),
                            "input": c.get("input") or {},
                        }
                        tool_uses_in_turn.append(tu)
                        tool_use_index[tu["id"]] = {
                            "turn_idx": len(turns),
                            "line": line_no,
                            "name": tu["name"],
                            "input": tu["input"],
                        }
            turn = {
                "idx": len(turns),
                "line": line_no,
                "ts": ts,
                "tokens": tokens,
                "tool_uses": tool_uses_in_turn,
                "missing_usage": not bool(usage),
            }
            if turn["missing_usage"]:
                anomalies.append(f"assistant turn at line {line_no} missing usage block")
            turns.append(turn)

        elif t == "user" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tu_id = c.get("tool_use_id")
                    if tu_id:
                        tool_results[tu_id] = {
                            "body": _extract_tool_result_text(c),
                            "line": line_no,
                        }

    wall_seconds = None
    if first_ts and last_ts:
        try:
            f0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            f1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            wall_seconds = (f1 - f0).total_seconds()
        except Exception as e:
            anomalies.append(f"failed to parse timestamps: {e}")

    return {
        "turns": turns,
        "tool_use_index": tool_use_index,
        "tool_results": tool_results,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "wall_seconds": wall_seconds,
        "anomalies": anomalies,
        "tokens_breakdown": tokens_breakdown,
    }


# ----------------------------------------------------------------------------
# Waste classification heuristics. Honest: if we can't classify, default to 'other'.
# ----------------------------------------------------------------------------
_REDIRECT_RE = re.compile(r"REDIRECT DETECTED|redirects to|Status:\s*30\d", re.IGNORECASE)
_BOARD_OPENINGS_RE = re.compile(r"\b(?:open positions?|openings)\b|Best\s+\w+\s+Jobs\s+in|jobs\s+analysis", re.IGNORECASE)
_TITLE_AT_COMPANY_RE = re.compile(r"^[ \t]*[-*\d.]+\s*\**[A-Z][^\n]{2,80}\s+(?:at|@|—|-)\s+[A-Z][^\n]{1,60}", re.MULTILINE)
_WORKDAY_RE = re.compile(r"myworkdayjobs\.com|wd[15]\.myworkday", re.IGNORECASE)
_HTTP_404_RE = re.compile(r"HTTP\s+(?:404|410|403|5\d\d)\b", re.IGNORECASE)
_EMPTY_PAGE_HINT_RE = re.compile(r"I don't see any web page content|response body was not retrieved|page snippet|content provided|between the dashes (?:is|appears)", re.IGNORECASE)


def _looks_like_board_listing(body: str) -> bool:
    """≥5 'Title at Company' lines without any single sustained description body.
    A description body is operationalized as ≥250 contiguous non-bullet chars."""
    matches = _TITLE_AT_COMPANY_RE.findall(body)
    if len(matches) < 5 and not _BOARD_OPENINGS_RE.search(body):
        return False
    # Crude "has dense description body": longest run of non-bullet chars
    # between blank lines.
    blocks = re.split(r"\n[\s\-\*•]*\n", body)
    longest = max((len(b.strip()) for b in blocks), default=0)
    has_body = longest > 250 and not re.search(r"^\s*[-*]", blocks[0] if blocks else "")
    return _BOARD_OPENINGS_RE.search(body) is not None or (len(matches) >= 5 and not has_body)


def _is_pre_known_dead(url: str, body: str, pruned_links: set[str]) -> bool:
    if not _HTTP_404_RE.search(body) and "404 Not Found" not in body:
        return False
    return url in pruned_links


def classify_webfetch(url: str, body: str, pruned_links: set[str]) -> str:
    """Return one of: redirect_tax | board_returns | js_empty_workday | pre_known_dead | other."""
    if not body:
        return "other"
    # Order matters: pre_known_dead is most specific; redirect_tax is unambiguous.
    if _is_pre_known_dead(url, body, pruned_links):
        return "pre_known_dead"
    if _REDIRECT_RE.search(body):
        return "redirect_tax"
    if _WORKDAY_RE.search(url) and len(body) < 600 and _EMPTY_PAGE_HINT_RE.search(body):
        return "js_empty_workday"
    if _looks_like_board_listing(body):
        return "board_returns"
    return "other"


def load_pruned_links() -> set[str]:
    if not PRUNED_HISTORY_PATH.exists():
        return set()
    out = set()
    try:
        for line in PRUNED_HISTORY_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ob = json.loads(line)
                if isinstance(ob, dict) and ob.get("link"):
                    out.add(ob["link"])
            except json.JSONDecodeError:
                continue
    except Exception:
        return set()
    return out


# ----------------------------------------------------------------------------
# Per-turn token attribution. Honest about granularity.
# ----------------------------------------------------------------------------
def attribute_waste(parsed: dict, pruned_links: set[str]) -> tuple[dict, list[dict]]:
    """For each WebFetch tool_use, classify and aggregate.

    Token attribution is per-TURN: a turn's tokens are split equally across its
    tool calls (rare in practice since multi-tool turns are uncommon in CC),
    then summed into the dominant bucket per call. Better-than-guess approach
    when the API doesn't give per-call usage."""
    turns = parsed["turns"]
    tool_results = parsed["tool_results"]
    tool_use_index = parsed["tool_use_index"]

    buckets = {
        "redirect_tax":     {"count": 0, "tokens_lost": 0, "urls": []},
        "board_returns":    {"count": 0, "tokens_lost": 0, "urls": []},
        "js_empty_workday": {"count": 0, "tokens_lost": 0, "urls": []},
        "pre_known_dead":   {"count": 0, "tokens_lost": 0, "urls": []},
        "other":            {"count": 0, "tokens_lost": 0, "urls": []},
    }
    classifications_per_turn = defaultdict(list)  # turn_idx → list[(name, label, url)]

    for tu_id, info in tool_use_index.items():
        name = info["name"]
        turn_idx = info["turn_idx"]
        inp = info["input"]
        if name == "WebFetch":
            url = (inp or {}).get("url", "") if isinstance(inp, dict) else ""
            result = tool_results.get(tu_id, {})
            body = result.get("body", "")
            label = classify_webfetch(url, body, pruned_links)
            classifications_per_turn[turn_idx].append((name, label, url))

    # Now attribute turn tokens. Per spec: if a turn has 5 parallel calls and
    # 2 are waste, give the WHOLE turn's tokens to the dominant-waste bucket.
    # Productive non-waste contributions also accumulate so verification sums.
    productive_tokens = 0
    for turn in turns:
        idx = turn["idx"]
        tokens = turn["tokens"]
        tool_uses = turn["tool_uses"]
        n_tools = len(tool_uses)
        webfetch_labels = classifications_per_turn.get(idx, [])

        if n_tools == 0:
            # Pure thinking / final-answer turn. Productive.
            productive_tokens += tokens
            continue

        if not webfetch_labels:
            # All tools were non-WebFetch (Bash, Read, WebSearch, Agent, etc.)
            productive_tokens += tokens
            continue

        # At least one WebFetch in this turn. Choose dominant non-'other' bucket
        # if any, else fall through to 'other'. Tokens of the entire turn are
        # attributed to that bucket (granularity caveat documented in output).
        non_other = [lbl for (_, lbl, _) in webfetch_labels if lbl != "other"]
        if non_other:
            from collections import Counter as _C
            label = _C(non_other).most_common(1)[0][0]
        else:
            label = "other"

        # Count every WebFetch call into its own bucket (count = call count, not turn count).
        for _name, _lbl, _url in webfetch_labels:
            buckets[_lbl]["count"] += 1
            buckets[_lbl]["urls"].append(_url)
        # All turn tokens go to the dominant bucket.
        buckets[label]["tokens_lost"] += tokens

    return buckets, [{"productive_tokens": productive_tokens}]


# ----------------------------------------------------------------------------
# Subagent analysis.
# ----------------------------------------------------------------------------
def analyze_subagent_file(path: Path) -> dict:
    """Walk one subagent jsonl, return rollup."""
    total_tokens = 0
    tool_count = 0
    tool_names = []
    first_ts = None
    last_ts = None
    has_webfetch = False
    has_websearch = False

    for _, ob in iter_jsonl(path):
        ts = ob.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if ob.get("type") != "assistant":
            continue
        msg = ob.get("message") or {}
        total_tokens += _sum_turn_tokens(msg.get("usage") or {})
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_count += 1
                    nm = c.get("name", "")
                    tool_names.append(nm)
                    if nm == "WebFetch":
                        has_webfetch = True
                    elif nm == "WebSearch":
                        has_websearch = True

    duration_ms = None
    if first_ts and last_ts:
        try:
            d0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            d1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_ms = int((d1 - d0).total_seconds() * 1000)
        except Exception:
            pass

    # Heuristic baseline: only meaningful for python-only agents.
    if has_webfetch or has_websearch:
        expected = None
        baseline_kind = "unknown"
    else:
        expected = 5000 + 2000 * tool_count
        baseline_kind = "python_only"

    bloat_delta_pct = None
    if expected:
        bloat_delta_pct = round((total_tokens - expected) / expected * 100, 1)

    return {
        "id": path.stem.replace("agent-", ""),
        "tokens": total_tokens,
        "tool_count": tool_count,
        "duration_ms": duration_ms,
        "expected": expected if expected is not None else "unknown",
        "baseline_kind": baseline_kind,
        "bloat_delta_pct": bloat_delta_pct,
    }


def discover_subagent_files(subagent_dir: str | None) -> list[Path]:
    """Subagent task outputs in CC are typically symlinks under
    /private/tmp/claude-<uid>/.../tasks/<id>.output → .../subagents/agent-<id>.jsonl
    We follow symlinks and skip ones that don't resolve to a real JSONL."""
    if not subagent_dir:
        return []
    sd = Path(subagent_dir)
    if not sd.exists():
        return []
    files = []
    for entry in sorted(sd.iterdir()):
        try:
            # Resolve symlinks to the real file
            real = entry.resolve()
            if real.exists() and real.is_file() and real.stat().st_size > 0:
                files.append(real)
        except Exception:
            continue
    return files


# ----------------------------------------------------------------------------
# Within-run subagent baseline. Replaces the hand-tuned 5000+2000*tool_count
# heuristic with a comparison against the run's OWN subagents — interpretable
# ("this subagent used 3× the per-tool tokens of its peers") and free of the
# cold-start calibration problem the legacy baseline suffers from.
# ----------------------------------------------------------------------------
def compute_subagent_run_stats(subagents: list[dict]) -> dict:
    """Tokens-per-tool-call distribution across all subagents in this run.

    Requires at least 2 subagents to be meaningful — a single observation has
    no peers to compare against. Caller MUST treat `n < 2` as "skip outlier
    flagging this run" rather than fabricating a baseline.
    """
    ratios = []
    for s in subagents:
        tc = max(s.get("tool_count", 0), 1)
        ratios.append(s["tokens"] / tc)
    if len(ratios) < 2:
        return {"n": len(ratios), "skip_reason": "need >=2 subagents for run-internal baseline"}
    return {
        "n": len(ratios),
        "median_tokens_per_tool": _p50(ratios),
        "p90_tokens_per_tool": _p90(ratios),
        "outlier_ratio_threshold": SUBAGENT_OUTLIER_RATIO,
    }


def annotate_subagent_outliers(subagents: list[dict], stats: dict) -> list[dict]:
    """Mutate each subagent dict in-place with `relative_to_run_median_ratio`
    and `internal_outlier`. If stats has no median (n<2), set both to None/False
    and skip flagging — no peers, no signal."""
    med = stats.get("median_tokens_per_tool")
    if not med or stats.get("n", 0) < 2:
        for s in subagents:
            s["relative_to_run_median_ratio"] = None
            s["internal_outlier"] = False
        return subagents
    for s in subagents:
        tc = max(s.get("tool_count", 0), 1)
        ratio = (s["tokens"] / tc) / med if med > 0 else None
        s["relative_to_run_median_ratio"] = round(ratio, 2) if ratio is not None else None
        s["internal_outlier"] = ratio is not None and ratio >= SUBAGENT_OUTLIER_RATIO
    return subagents


# ----------------------------------------------------------------------------
# Current-run efficiency block. Joins the session-derived token breakdown with
# the latest run_diagnostics row (which holds valid_jobs / score_distribution).
# ----------------------------------------------------------------------------
def compute_current_efficiency(parsed: dict, subagents: list[dict]) -> dict:
    """Compute the efficiency KPIs the pain-point thresholds key off:
      - main_tokens, valid_jobs, tokens_per_valid_job
      - cache_hit_rate (true Anthropic definition: cache_read / input-side)
      - subagent_token_share (subagent context cost as fraction of main)
      - pct_high_score (carried forward for non-regression checks)

    Any field whose denominator is zero comes back as None — never invent a
    number when the data does not support one.
    """
    tb = parsed["tokens_breakdown"]
    input_t = tb["input"]
    output_t = tb["output"]
    cache_read = tb["cache_read"]
    cache_creation = tb["cache_creation"]

    main_tokens = input_t + output_t + cache_read + cache_creation

    input_side = input_t + cache_read + cache_creation
    cache_hit_rate = (cache_read / input_side) if input_side > 0 else None

    subagent_tokens = sum(s["tokens"] for s in subagents)
    subagent_token_share = (subagent_tokens / main_tokens) if main_tokens > 0 else None

    latest = _load_recent_diagnostics(1)
    valid_jobs = None
    pct_high_score = None
    tpvj = None
    if latest:
        valid_jobs = int(latest[0].get("valid_jobs", 0) or 0)
        if valid_jobs > 0 and main_tokens > 0:
            tpvj = main_tokens / valid_jobs
            sd = latest[0].get("score_distribution") or {}
            pct_high_score = int(sd.get("90-100", 0) or 0) / valid_jobs

    return {
        "main_tokens": main_tokens,
        "valid_jobs": valid_jobs,
        "tokens_per_valid_job": round(tpvj, 1) if tpvj is not None else None,
        "cache_hit_rate": round(cache_hit_rate, 3) if cache_hit_rate is not None else None,
        "subagent_token_share": round(subagent_token_share, 3) if subagent_token_share is not None else None,
        "pct_high_score": round(pct_high_score, 3) if pct_high_score is not None else None,
    }


# ----------------------------------------------------------------------------
# Parallelism candidate detection. Heuristic, advisory only.
# ----------------------------------------------------------------------------
# Parallelism only reportable for *network-bound* tools. Local-state tools
# (Bash, Read, Edit, Write) carry implicit dependencies (cwd, file mtimes,
# transactional edits) that an input-overlap heuristic CANNOT see — flagging
# those would be misleading "advice" the user has to filter through.
_PARALLELIZABLE_TOOLS = {"WebFetch", "WebSearch"}
_PARALLELISM_REPORT_CAP = 5  # advisory only; truncate to keep output compact


def find_parallelism_candidates(parsed: dict) -> list[dict]:
    """Look for pairs of consecutive assistant turns where both have exactly 1
    network tool_use, and turn N's result doesn't appear in turn N+1's input."""
    out = []
    turns = parsed["turns"]
    tool_results = parsed["tool_results"]

    for n in range(len(turns) - 1):
        t1 = turns[n]
        t2 = turns[n + 1]
        if len(t1["tool_uses"]) != 1 or len(t2["tool_uses"]) != 1:
            continue
        tu1 = t1["tool_uses"][0]
        tu2 = t2["tool_uses"][0]
        if tu1["name"] not in _PARALLELIZABLE_TOOLS or tu2["name"] not in _PARALLELIZABLE_TOOLS:
            continue
        r1 = tool_results.get(tu1["id"], {}).get("body", "")
        if not r1:
            continue
        tu2_input_str = json.dumps(tu2["input"]) if tu2["input"] else ""

        # Dependency signal: a URL from turn N's result appears in turn N+1's
        # input. That's the strongest "N+1 was driven by N" signal we can get.
        overlap = False
        urls_in_r1 = set(re.findall(r"https?://[^\s\"'<>)\]]+", r1))
        for u in urls_in_r1:
            if u and u in tu2_input_str:
                overlap = True
                break
        if not overlap:
            # Fallback: any 30+ char alphanumeric chunk from r1 reused in N+1's input.
            tokens_r1 = re.findall(r"[A-Za-z0-9_/-]{30,}", r1)
            for tk in tokens_r1[:50]:
                if tk in tu2_input_str:
                    overlap = True
                    break
        if overlap:
            continue

        out.append({
            "turns": [n, n + 1],
            "tools": [tu1["name"], tu2["name"]],
        })
        if len(out) >= _PARALLELISM_REPORT_CAP:
            break
    return out


# ----------------------------------------------------------------------------
# Hot-section tracker. Identifies skill-file sections whose fingerprints
# (backtick-quoted identifiers) never surface in session activity, indicating
# dead-weight context that can be compressed without losing information.
#
# Approach: parse markdown into ##/### sections, extract distinctive backtick
# identifiers as fingerprints, scan session activity for matches, persist refs
# per run, then roll up the last N runs to find sections cold across history.
# ----------------------------------------------------------------------------
_SECTION_HEADING_RE = re.compile(r'^(#{1,4})\s+(.+?)\s*$')
_BACKTICK_TOKEN_RE = re.compile(r'`([^`\n]{3,80})`')
# Identifiers must contain a separator or be CamelCase — filters out prose
# words like `bytes` / `refs` / `count` that would false-positive on any text.
_FINGERPRINT_SEPARATORS = ("_", "/", ".", ":", "-")
_FINGERPRINT_STOPLIST = {
    "true", "false", "none", "null", "type", "name", "input", "output",
    "value", "list", "dict", "str", "int", "key", "val", "args", "kwargs",
}


def _is_good_fingerprint(s: str) -> bool:
    s = s.strip()
    if len(s) < 4 or len(s) > 80:
        return False
    if s.lower() in _FINGERPRINT_STOPLIST:
        return False
    if any(c in s for c in _FINGERPRINT_SEPARATORS):
        return True
    # Allow CamelCase identifiers (e.g., `WebFetch`, `TodoWrite`).
    has_upper_after_first = any(c.isupper() for c in s[1:])
    has_lower = any(c.islower() for c in s)
    return has_upper_after_first and has_lower


def parse_markdown_sections(text: str) -> list[dict]:
    """Split markdown into sections by heading. A section's body is everything
    from its heading until the next heading at the same or shallower level.

    Returns [{file_relpath, heading, level, bytes, fingerprints[]}]. File path
    is filled in by the caller — this function operates on raw text only.
    """
    sections = []
    current_heading = "(preamble)"
    current_level = 0
    current_body: list[str] = []

    def _flush():
        if not current_body and current_heading == "(preamble)":
            return
        body_text = "\n".join(current_body)
        # Fingerprints: backtick identifiers passing the goodness filter.
        raw = _BACKTICK_TOKEN_RE.findall(body_text)
        fps = sorted({f for f in raw if _is_good_fingerprint(f)})
        sections.append({
            "heading": current_heading,
            "level": current_level,
            "bytes": len(body_text),
            "fingerprints": fps,
        })

    for line in text.splitlines():
        m = _SECTION_HEADING_RE.match(line)
        if m:
            _flush()
            current_level = len(m.group(1))
            current_heading = m.group(2).strip()
            current_body = []
        else:
            current_body.append(line)
    _flush()

    return [s for s in sections if s["heading"] != "(preamble)" or s["bytes"] > 0]


def discover_skill_files() -> list[Path]:
    out: list[Path] = []
    for pattern in SKILL_FILE_GLOBS:
        out.extend(sorted(PROJECT_ROOT.glob(pattern)))
    return out


def _collect_session_text(parsed: dict, main_path: Path) -> str:
    """Concatenate everything the session model 'saw' or 'said' into one string,
    for fingerprint substring search. Includes tool_use inputs (as JSON), all
    tool_result bodies, and assistant content. We re-walk the JSONL because
    the parsed-session dict only retains structural metadata, not raw bodies."""
    chunks: list[str] = []
    for tu_id, info in parsed["tool_use_index"].items():
        inp = info.get("input") or {}
        try:
            chunks.append(json.dumps(inp, default=str))
        except Exception:
            chunks.append(str(inp))
    for tr in parsed["tool_results"].values():
        body = tr.get("body") or ""
        if body:
            chunks.append(body)
    # Assistant content already lives in turn → tool_uses; the text portions
    # are not retained by parse_main_session, so re-read once for completeness.
    for _, ob in iter_jsonl(main_path):
        if ob.get("type") != "assistant":
            continue
        content = (ob.get("message") or {}).get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    t = c.get("text") or ""
                    if t:
                        chunks.append(t)
    return "\n".join(chunks)


def scan_skill_sections(parsed: dict, main_path: Path) -> dict:
    """Per-file section reference counts for the current run.

    Returns: {
      "files": {file_relpath: [{heading, bytes, n_fingerprints, refs}, ...]},
      "totals": {n_files, n_sections, n_measurable_sections, total_skill_bytes},
    }
    A section is "measurable" if it has at least SKILL_SECTION_MIN_FINGERPRINTS.
    Non-measurable sections are still included (so they appear in persisted
    history) but cannot be flagged as cold downstream.

    `total_skill_bytes` is the load-bearing trend metric for the continuous
    compaction loop — a pro user running /fetchjobs heavily should see this
    trend DOWNWARD as cold sections get archived and Tier 1/2 lossless
    transforms get applied. Flat or upward trend means compaction is stalling.
    """
    session_text = _collect_session_text(parsed, main_path)
    files_out: dict = {}
    n_sections = 0
    n_measurable = 0
    total_bytes = 0
    for fp in discover_skill_files():
        try:
            text = fp.read_text()
        except Exception:
            continue
        total_bytes += len(text)
        sections = parse_markdown_sections(text)
        rel = str(fp.relative_to(PROJECT_ROOT))
        rows = []
        for s in sections:
            fps = s["fingerprints"]
            if fps:
                refs = sum(1 for f in fps if f in session_text)
            else:
                refs = 0
            rows.append({
                "heading": s["heading"],
                "bytes": s["bytes"],
                "n_fingerprints": len(fps),
                "refs": refs,
            })
            n_sections += 1
            if len(fps) >= SKILL_SECTION_MIN_FINGERPRINTS:
                n_measurable += 1
        files_out[rel] = rows
    return {
        "files": files_out,
        "totals": {
            "n_files": len(files_out),
            "n_sections": n_sections,
            "n_measurable_sections": n_measurable,
            "total_skill_bytes": total_bytes,
        },
    }


def compute_compaction_trend(window: int = 3) -> dict:
    """Look back at the last `window` rows of skill_section_usage.jsonl and
    return the trend of total_skill_bytes. Pro user contract: this should be
    non-increasing across recent runs. If it is, compaction is working.

    Returns: {
      "window": int,
      "n_runs_seen": int,
      "total_skill_bytes_series": [int, ...],   # oldest → newest
      "delta_bytes_vs_oldest": int,              # negative means shrinking (good)
      "stagnant": bool,                           # no decrease across window
    }
    """
    rows = _load_section_usage_history(window)
    if not rows:
        return {"window": window, "n_runs_seen": 0, "skip_reason": "no history"}
    series = []
    for r in rows:
        t = (r.get("totals") or {}).get("total_skill_bytes")
        if t is None:
            # Backfill from file sections if totals missing (older row format).
            t = sum(s["bytes"] for sects in (r.get("files") or {}).values() for s in sects)
        series.append(int(t or 0))
    if not series:
        return {"window": window, "n_runs_seen": 0, "skip_reason": "no usable totals"}
    delta = series[-1] - series[0]
    stagnant = len(series) >= window and delta >= 0
    return {
        "window": window,
        "n_runs_seen": len(series),
        "total_skill_bytes_series": series,
        "delta_bytes_vs_oldest": delta,
        "stagnant": stagnant,
    }


def persist_skill_section_usage(usage: dict, session_id: str) -> dict:
    """Append a row to data/skill_section_usage.jsonl. If the last row already
    has the same session_id, OVERWRITE it instead (prevents duplicate rows when
    /improve re-runs the audit on the same session)."""
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "files": usage["files"],
        "totals": usage["totals"],
    }
    SKILL_SECTION_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if SKILL_SECTION_USAGE_PATH.exists():
        existing_lines = [
            ln for ln in SKILL_SECTION_USAGE_PATH.read_text().splitlines() if ln.strip()
        ]
        if existing_lines:
            try:
                last = json.loads(existing_lines[-1])
                if last.get("session_id") == session_id:
                    existing_lines = existing_lines[:-1]
            except json.JSONDecodeError:
                pass
    existing_lines.append(json.dumps(row, default=str))
    SKILL_SECTION_USAGE_PATH.write_text("\n".join(existing_lines) + "\n")
    return {"persisted": True, "row_count_after": len(existing_lines)}


def _load_section_usage_history(window: int) -> list[dict]:
    if not SKILL_SECTION_USAGE_PATH.exists():
        return []
    out = []
    try:
        lines = SKILL_SECTION_USAGE_PATH.read_text().splitlines()
        # Cap how many lines we parse to bound memory as the file grows.
        for line in lines[-_MAX_SECTION_USAGE_LINES:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out[-window:]


def _count_jsonl_lines(path: Path) -> int:
    """Count non-empty lines in a JSONL file without loading all content."""
    if not path.exists():
        return 0
    try:
        return sum(1 for ln in path.read_text().splitlines() if ln.strip())
    except Exception:
        return 0


def compute_cold_sections(window: int = SKILL_SECTION_USAGE_WINDOW) -> dict:
    """Roll up the last `window` runs of section usage and emit cold candidates.

    Adaptive threshold: within each file, compute bytes-per-ref across measurable
    sections (where ref = total refs over the window, floored at 0.5 to avoid
    divide-by-zero); flag sections in the file's top quartile of bytes-per-ref,
    provided they ALSO satisfy:
      - bytes >= SKILL_SECTION_MIN_BYTES
      - n_fingerprints >= SKILL_SECTION_MIN_FINGERPRINTS
      - total_refs <= SKILL_SECTION_WARM_THRESHOLD_REFS (a section referenced
        more than once across the window is warm, not cold)

    Result is cached in data/cold_sections_cache.json keyed by the current line
    count of skill_section_usage.jsonl. If no new /fetchjobs run has appended to
    that file since the last compute, return the cached result immediately.
    """
    current_line_count = _count_jsonl_lines(SKILL_SECTION_USAGE_PATH)
    try:
        if COLD_SECTIONS_CACHE_PATH.exists():
            cached = json.loads(COLD_SECTIONS_CACHE_PATH.read_text())
            if (
                isinstance(cached, dict)
                and cached.get("skill_section_usage_line_count") == current_line_count
                and "result" in cached
            ):
                return cached["result"]
    except Exception:
        pass  # corrupt cache → recompute

    rows = _load_section_usage_history(window)
    if len(rows) < window:
        return {
            "n_runs_seen": len(rows),
            "window": window,
            "skip_reason": f"need {window} runs of skill_section_usage history; have {len(rows)}",
            "candidates": [],
        }

    # Aggregate refs per (file, heading) across the window.
    aggregated: dict = {}  # (file, heading) -> {bytes, n_fingerprints, total_refs, runs_seen}
    for row in rows:
        for file_path, sections in (row.get("files") or {}).items():
            for s in sections:
                key = (file_path, s["heading"])
                a = aggregated.setdefault(key, {
                    "bytes": s["bytes"],
                    "n_fingerprints": s["n_fingerprints"],
                    "total_refs": 0,
                    "runs_seen": 0,
                })
                # Bytes and n_fingerprints can drift if the section was edited
                # between runs. Use the latest observed value (later rows win).
                a["bytes"] = s["bytes"]
                a["n_fingerprints"] = s["n_fingerprints"]
                a["total_refs"] += int(s.get("refs", 0) or 0)
                a["runs_seen"] += 1

    # Drop sections we haven't seen in all `window` runs — their heading may
    # have changed mid-window, so the rollup wouldn't be comparable.
    eligible = {k: v for k, v in aggregated.items() if v["runs_seen"] == window}

    # Group by file for per-file adaptive thresholding.
    per_file: dict = {}
    for (fpath, heading), v in eligible.items():
        per_file.setdefault(fpath, []).append((heading, v))

    candidates: list[dict] = []
    for fpath, entries in per_file.items():
        measurable = [
            (h, v) for h, v in entries
            if v["n_fingerprints"] >= SKILL_SECTION_MIN_FINGERPRINTS
            and v["bytes"] >= SKILL_SECTION_MIN_BYTES
        ]
        if len(measurable) < 4:
            # Need at least 4 measurable sections to make "top quartile" meaningful.
            continue
        ratios = [v["bytes"] / max(v["total_refs"], 0.5) for _, v in measurable]
        sorted_ratios = sorted(ratios)
        p75_idx = int(0.75 * (len(sorted_ratios) - 1))
        p75 = sorted_ratios[p75_idx]
        for (heading, v), ratio in zip(measurable, ratios):
            if ratio < p75:
                continue
            if v["total_refs"] > SKILL_SECTION_WARM_THRESHOLD_REFS:
                continue
            candidates.append({
                "file": fpath,
                "heading": heading,
                "bytes": v["bytes"],
                "n_fingerprints": v["n_fingerprints"],
                "total_refs_in_window": v["total_refs"],
                "bytes_per_ref": round(ratio, 1),
                "file_p75_bytes_per_ref": round(p75, 1),
            })

    # Rank globally by bytes (largest cold sections first — biggest savings).
    candidates.sort(key=lambda c: -c["bytes"])

    total_bytes_savings = sum(c["bytes"] for c in candidates)
    result = {
        "n_runs_seen": len(rows),
        "window": window,
        "candidates": candidates,
        "estimated_bytes_savings": total_bytes_savings,
        "_note": (
            "Cold = top-quartile bytes/ref within its file AND <=1 ref across "
            f"{window} runs AND >= {SKILL_SECTION_MIN_FINGERPRINTS} fingerprints "
            f"AND >= {SKILL_SECTION_MIN_BYTES} bytes. Sections with too few "
            "fingerprints (pure prose) cannot be measured and are excluded — "
            "human review still required before deletion."
        ),
    }
    # Persist so the next /improve call can skip recomputation if no new /fetchjobs ran.
    try:
        tmp = str(COLD_SECTIONS_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "skill_section_usage_line_count": current_line_count,
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            }, f, indent=2)
        import os
        os.replace(tmp, COLD_SECTIONS_CACHE_PATH)
    except Exception:
        pass  # cache write failure is non-fatal
    return result


# ----------------------------------------------------------------------------
# Post-apply regression detector. Reads data/improve_changes.jsonl read-only,
# finds recently-applied compactions whose current-run metrics indicate they
# backfired, and surfaces them so /improve can auto-revert.
#
# Comparison contract: pre_metrics is the snapshot taken at apply-time by
# improve_changes.apply_proposal(); current metrics come from the efficiency
# block of THIS audit run. ANY of the three independent checks triggering
# means regression — they don't have to all fail together. Skip a check when
# either side is None (don't fabricate a verdict on missing data).
# ----------------------------------------------------------------------------
def _parse_iso_ts(s: str) -> datetime | None:
    """Tolerant ISO-8601 parser. Returns None on any failure rather than raising
    — caller filters rows whose timestamp can't be parsed."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_improve_changes() -> list[dict]:
    """Read data/improve_changes.jsonl tolerantly. Returns [] if missing.
    Path is resolved from PROJECT_ROOT at call time so tests can monkeypatch it."""
    path = PROJECT_ROOT / "data" / "improve_changes.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ob = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ob, dict):
                out.append(ob)
    except Exception:
        return []
    return out


def _has_usable_pre_metrics(pm: object) -> bool:
    if not isinstance(pm, dict):
        return False
    for k in ("valid_jobs", "pct_high_score", "tokens_per_valid_job"):
        if pm.get(k) is not None:
            return True
    return False


def _check_regressions(pre: dict, current: dict) -> list[str]:
    """Return list of human-readable reason strings; empty means no regression.
    Each reason embeds the threshold so the user understands WHY it tripped."""
    reasons: list[str] = []

    pre_vj = pre.get("valid_jobs")
    cur_vj = current.get("valid_jobs")
    if pre_vj is not None and cur_vj is not None and pre_vj > 0:
        thresh = pre_vj * APPLY_REGRESSION_VALID_JOBS_RATIO
        if cur_vj < thresh:
            pct_drop = round((1 - cur_vj / pre_vj) * 100, 1)
            reasons.append(
                f"valid_jobs dropped {pct_drop}% ({pre_vj} -> {cur_vj}, "
                f"threshold {APPLY_REGRESSION_VALID_JOBS_RATIO}x = {round(thresh, 2)})"
            )

    pre_phs = pre.get("pct_high_score")
    cur_phs = current.get("pct_high_score")
    if pre_phs is not None and cur_phs is not None and pre_phs > 0:
        thresh = pre_phs * APPLY_REGRESSION_PCT_HIGH_SCORE_RATIO
        if cur_phs < thresh:
            pct_drop = round((1 - cur_phs / pre_phs) * 100, 1)
            reasons.append(
                f"pct_high_score dropped {pct_drop}% ({round(pre_phs, 3)} -> "
                f"{round(cur_phs, 3)}, threshold "
                f"{APPLY_REGRESSION_PCT_HIGH_SCORE_RATIO}x = {round(thresh, 3)})"
            )

    pre_tpvj = pre.get("tokens_per_valid_job")
    cur_tpvj = current.get("tokens_per_valid_job")
    if pre_tpvj is not None and cur_tpvj is not None and pre_tpvj > 0:
        thresh = pre_tpvj * APPLY_REGRESSION_TOKENS_PER_VJ_RATIO
        if cur_tpvj > thresh:
            pct_growth = round((cur_tpvj / pre_tpvj - 1) * 100, 1)
            reasons.append(
                f"tokens_per_valid_job grew {pct_growth}% ({round(pre_tpvj, 1)} -> "
                f"{round(cur_tpvj, 1)}, threshold "
                f"{APPLY_REGRESSION_TOKENS_PER_VJ_RATIO}x = {round(thresh, 1)})"
            )

    return reasons


def compute_recent_apply_regressions(
    efficiency: dict,
    current_session_id: str | None = None,
    lookback_days: int = APPLY_REGRESSION_LOOKBACK_DAYS,
) -> dict:
    """Identify recently-applied compaction changes whose next-run metrics
    indicate quality regression.

    Pure analysis — never writes. Tolerates a missing improve_changes.jsonl by
    returning an empty result with an explanatory `_note`.

    `current_session_id` is used to skip rows whose `applied_in_session` matches
    the audit's own session — comparing a change against the same run that
    applied it is uninformative. When the field is absent on a row, we keep the
    row (we can't prove it's the same session).
    """
    rows = _load_improve_changes()
    if not rows:
        return {
            "n_changes_checked": 0,
            "regressions": [],
            "validated": [],
            "_note": "no improve_changes.jsonl yet",
        }

    now = datetime.now(timezone.utc)
    cutoff_seconds = lookback_days * 86400
    eligible: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "applied":
            continue
        ts = _parse_iso_ts(row.get("timestamp", ""))
        if ts is None:
            continue
        if (now - ts).total_seconds() > cutoff_seconds:
            continue
        pre = row.get("pre_metrics")
        if not _has_usable_pre_metrics(pre):
            continue
        # Already settled — don't re-flag.
        if row.get("validated_at") or row.get("reverted_at"):
            continue
        applied_in_session = row.get("applied_in_session")
        if (
            applied_in_session is not None
            and current_session_id is not None
            and applied_in_session == current_session_id
        ):
            continue
        eligible.append(row)

    regressions: list[dict] = []
    validated: list[dict] = []
    # Only the three metrics we actually compare against pre.
    current_for_check = {
        "valid_jobs": efficiency.get("valid_jobs"),
        "pct_high_score": efficiency.get("pct_high_score"),
        "tokens_per_valid_job": efficiency.get("tokens_per_valid_job"),
    }
    for row in eligible:
        pre = row.get("pre_metrics") or {}
        reasons = _check_regressions(pre, current_for_check)
        change_id = row.get("change_id") or "(unknown)"
        applied_at = row.get("timestamp", "")
        applied_in_session = row.get("applied_in_session")
        if reasons:
            regressions.append({
                "change_id": change_id,
                "applied_at": applied_at,
                "applied_in_session": applied_in_session,
                "pre_metrics": pre,
                "current_metrics": current_for_check,
                "regression_reasons": reasons,
            })
        else:
            validated.append({"change_id": change_id, "applied_at": applied_at})

    # Oldest-first: a regression from 5 days ago is more urgent than one from today.
    regressions.sort(key=lambda r: _parse_iso_ts(r["applied_at"]) or now)

    return {
        "n_changes_checked": len(eligible),
        "regressions": regressions,
        "validated": validated,
        "_note": (
            f"Eligible rows = status==applied within last {lookback_days} days, "
            "non-null pre_metrics, no validated_at/reverted_at, and (when "
            "applied_in_session is set) not the current session. A row regresses "
            f"if ANY of: valid_jobs < {APPLY_REGRESSION_VALID_JOBS_RATIO}x pre, "
            f"pct_high_score < {APPLY_REGRESSION_PCT_HIGH_SCORE_RATIO}x pre, or "
            f"tokens_per_valid_job > {APPLY_REGRESSION_TOKENS_PER_VJ_RATIO}x pre. "
            "Individual checks are skipped when either side is None — no verdict "
            "is fabricated from missing data."
        ),
    }


# ----------------------------------------------------------------------------
# Main.
# ----------------------------------------------------------------------------
def build_audit(args) -> dict:
    marker = load_session_marker(args.session)
    if not marker.get("detected"):
        return {"audit_failed": True, "reason": "NO_SESSION_MARKER", "detail": marker.get("reason")}

    main_path_str = marker.get("main_jsonl_path")
    if not main_path_str or not Path(main_path_str).exists():
        return {"audit_failed": True, "reason": "NO_SESSION_MARKER", "detail": f"main_jsonl_path missing or absent: {main_path_str}"}
    main_path = Path(main_path_str)

    # Schema-drift defense.
    schema_check = check_schema(main_path)
    if not schema_check["ok"]:
        return {
            "audit_failed": True,
            "reason": "schema_drift",
            "expected_version": EXPECTED_JSONL_SCHEMA_VERSION,
            "match_ratio": schema_check["match_ratio"],
            "sample_top_keys": schema_check["sample_top_keys"],
        }

    parsed = parse_main_session(main_path)
    pruned_links = load_pruned_links()

    # Totals
    main_tokens = sum(t["tokens"] for t in parsed["turns"])
    websearch_calls = sum(1 for info in parsed["tool_use_index"].values() if info["name"] == "WebSearch")
    webfetch_calls = sum(1 for info in parsed["tool_use_index"].values() if info["name"] == "WebFetch")

    # Subagents
    sub_files = discover_subagent_files(marker.get("subagent_dir"))
    subagents = [analyze_subagent_file(p) for p in sub_files]
    subagent_tokens = sum(s["tokens"] for s in subagents)

    # Within-run subagent baseline (replaces unreliable hardcoded heuristic for
    # SUBAGENT_TOKEN_BLOAT firing). Mutates `subagents` to add outlier flags.
    subagent_run_stats = compute_subagent_run_stats(subagents)
    subagents = annotate_subagent_outliers(subagents, subagent_run_stats)

    # Waste attribution
    waste, extra = attribute_waste(parsed, pruned_links)
    productive_tokens = extra[0]["productive_tokens"]

    # Verification: sum of attributed tokens should match main_tokens exactly.
    per_call_token_sum = productive_tokens + sum(b["tokens_lost"] for b in waste.values())
    session_total = main_tokens
    delta = abs(per_call_token_sum - session_total)
    tolerance = max(50, int(0.02 * session_total))
    verification_ok = delta <= tolerance

    verification = {
        "per_call_token_sum": per_call_token_sum,
        "session_total_tokens": session_total,
        "delta": delta,
        "tolerance": tolerance,
        "ok": verification_ok,
    }
    if not verification_ok:
        # Honest halt — never emit bucket counts that won't reconcile.
        return {
            "audit_failed": True,
            "reason": "verification_mismatch",
            "verification": verification,
        }

    # Parallelism candidates
    para = find_parallelism_candidates(parsed)

    # Efficiency KPIs (current run) + priors (last N runs, current excluded).
    # These power the TOKEN_*/MAIN_AGENT_* pain-points in /improve §3.
    efficiency = compute_current_efficiency(parsed, subagents)
    priors = compute_priors_from_diagnostics(PRIORS_WINDOW)

    # Hot-section tracker: per-run reference scan + 3-run rollup for cold detection.
    # Persistence is on by default; --no-persist suppresses for dry runs / tests.
    skill_section_usage = scan_skill_sections(parsed, main_path)
    if not args.no_persist:
        persist_skill_section_usage(skill_section_usage, marker.get("session_id") or "unknown")
    cold_sections = compute_cold_sections(SKILL_SECTION_USAGE_WINDOW)
    # Compaction trend uses a 3-run window regardless of cold-detection window;
    # we want stagnation signal even when cold-detection runs at window=1.
    compaction_trend = compute_compaction_trend(window=3)

    # Post-apply regression check: did any recently-applied compaction backfire?
    # Pure analysis from data/improve_changes.jsonl; never writes.
    auto_revert_candidates = compute_recent_apply_regressions(
        efficiency, current_session_id=marker.get("session_id")
    )

    out = {
        "audit_version": AUDIT_VERSION,
        "schema_seen_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": marker.get("session_id"),
        "fetchjobs_run_detected": bool(marker.get("fetchjobs_command")),
        "totals": {
            "main_tokens": main_tokens,
            "subagent_tokens": subagent_tokens,
            "wall_seconds": round(parsed["wall_seconds"], 1) if parsed["wall_seconds"] is not None else None,
            "websearch_calls": websearch_calls,
            "webfetch_calls": webfetch_calls,
            "productive_tokens": productive_tokens,
            "input_tokens": parsed["tokens_breakdown"]["input"],
            "output_tokens": parsed["tokens_breakdown"]["output"],
            "cache_read_tokens": parsed["tokens_breakdown"]["cache_read"],
            "cache_creation_tokens": parsed["tokens_breakdown"]["cache_creation"],
            "n_assistant_turns": len(parsed["turns"]),
            "n_subagents": len(subagents),
        },
        "waste": {
            k: ({"count": v["count"], "tokens_lost": v["tokens_lost"]} if not args.verbose
                else {"count": v["count"], "tokens_lost": v["tokens_lost"], "urls": v["urls"]})
            for k, v in waste.items()
        },
        # Non-verbose subagent rows are stripped to the fields most useful for
        # pain-point matching (SUBAGENT_TOKEN_BLOAT). Full detail in --verbose.
        "subagents": (
            subagents if args.verbose
            else [
                {
                    "id": s["id"][:9],
                    "tokens": s["tokens"],
                    "tool_count": s["tool_count"],
                    "bloat_delta_pct": s["bloat_delta_pct"],
                    "relative_to_run_median_ratio": s.get("relative_to_run_median_ratio"),
                    "internal_outlier": s.get("internal_outlier", False),
                }
                for s in subagents
            ]
        ),
        "subagent_run_stats": subagent_run_stats,
        "efficiency": efficiency,
        "priors": priors,
        "skill_section_usage_totals": skill_section_usage["totals"],
        "cold_sections": cold_sections,
        "compaction_trend": compaction_trend,
        "auto_revert_candidates": auto_revert_candidates,
        "parallelism_candidates": para,
        "parallelism_note": "advisory only; no input-overlap detected between consecutive single-network-tool turns",
        "verification": verification,
        "_granularity_note": (
            "Tokens are per-TURN, not per-call. A turn's tokens go to the dominant "
            "WebFetch waste bucket (or 'productive'). Reconciles to session total."
        ),
    }
    # Anomalies only when present — silent absence is honest.
    if parsed["anomalies"]:
        out["_anomalies"] = parsed["anomalies"][:10]
    if args.verbose:
        out["_turn_detail"] = [
            {
                "idx": t["idx"],
                "tokens": t["tokens"],
                "tool_uses": [{"name": tu["name"]} for tu in t["tool_uses"]],
            }
            for t in parsed["turns"]
        ]
    return out


def main():
    ap = argparse.ArgumentParser(description="Audit /fetchjobs run token + latency efficiency.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--last-fetchjobs", action="store_true", help="Use data/last_session.json (default).")
    g.add_argument("--session", type=str, default=None, help="Specific session id to audit.")
    ap.add_argument("--verbose", action="store_true", help="Include URLs and per-turn detail.")
    ap.add_argument(
        "--no-persist",
        action="store_true",
        help="Suppress write to data/skill_section_usage.jsonl (dry-run / test mode).",
    )
    args = ap.parse_args()

    result = build_audit(args)
    # Use compact separators only when not verbose for sub-1.5KB output target.
    if args.verbose:
        sys.stdout.write(json.dumps(result, indent=2, default=str))
    else:
        sys.stdout.write(json.dumps(result, separators=(",", ":"), default=str))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
