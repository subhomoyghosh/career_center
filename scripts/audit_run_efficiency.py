#!/usr/bin/env python3
"""
Audit token + latency efficiency of the most recent /fetchjobs session.

Read-only. Zero side effects except writing JSON to stdout. Never makes API calls.
Never invents paths, token counts, or waste classifications. If verification fails,
emits an audit_failed marker and exits — better silent than guessing.

Usage:
  uv run python scripts/audit_run_efficiency.py [--last-fetchjobs] [--verbose]
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
                }
                for s in subagents
            ]
        ),
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
