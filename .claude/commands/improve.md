# /improve — God Mode Self-Improving Skill

> **Rules:** Never apply without explicit user approval per change. Never delete skill files. Quote exact current text in every proposal. Ground each proposal in a specific metric. One proposal at a time. /improve must not re-propose pain-points closed within 14 days unless evidence has worsened ≥10% (see §1.8).
>
> **Discovery non-degradation rule (applies to every proposal):** Every TOKEN_/LATENCY_/PATTERN_ proposal MUST include an information-preservation check — show that information available to the scoring step does not shrink, only the cost of getting it shrinks. PATTERN_* proposals modify `inclinations` / `disinclinations` / `learn_skills` in `candidate_info.json`; these are SOFT scoring biases in `/fetchjobs` Step 3, NEVER hard filters like `noise_keywords`. No auto-apply for any TOKEN_/LATENCY_/PATTERN_/SCORING_DRIFT proposal — all require explicit human approval.

---

## 0.5. Modes

`/improve` runs in one of three modes:

| Mode | Trigger | Behavior |
|------|---------|----------|
| Interactive (default) | `/improve` | Original flow: walk through Phases 1–6, ask for approval inline per proposal. |
| `--audit-only` | `/improve --audit-only` (auto-dispatched at end of `/fetchjobs` when `auto_improve_audit_enabled` is true) | Run Phases 1–3 only. Phase 4 writes each proposal to `data/improve_proposals.jsonl` via `job_finder.improve_changes.write_proposal(...)` instead of asking inline. Print a one-line summary: "N proposals staged — review in Streamlit → Analytics." Never applies anything. |
| `--apply <change_id>` | `/improve --apply <change_id>` (dispatched from the Streamlit UI when user clicks "Apply approved") | Look up `change_id` via `job_finder.improve_changes.apply_proposal(change_id)`. Print the result JSON. |

**Why two non-interactive modes:** the original chat-based one-at-a-time approval is fine for ad-hoc work but doesn't scale and provides no transparency surface. Audit-only stages proposals where the user can see all evidence + impact in one Streamlit table. `--apply` is what the UI calls back into when the user green-lights a row.

**Constitutional rule unchanged:** Nothing auto-applies. Audit-only is *advisory*; every approval still requires explicit human action — just routed through the UI instead of chat.

---

## 1. Ingest evidence

```bash
uv run python -c "
import json, pathlib
p = pathlib.Path('data/run_diagnostics.jsonl')
runs = [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()] if p.exists() else []
print(json.dumps(runs, indent=2))
"
```

```bash
uv run python scripts/get_nudge_context.py
```

```bash
uv run python -c "
import json, pathlib
p = pathlib.Path('data/improve_log.jsonl')
entries = [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()] if p.exists() else []
print(json.dumps(entries[-20:], indent=2))
"
```

Read current text of all skill/command files:

- `.claude/commands/setup.md`, `.claude/commands/fetchjobs.md`
- `.cursor/skills/validate-job-links/SKILL.md`, `.cursor/skills/leverage-feedback-and-weights/SKILL.md`
- `.cursor/skills/evaluate-nudge-and-wisdom/SKILL.md`, `.cursor/skills/discover-linkedin-jobs/SKILL.md`
- `.cursor/rules/jobsearch.mdc`, `.cursor/rules/setup_from_resume.mdc`
- `data/candidate_info.json`
- `.claude/commands/improve.md`, `.cursor/skills/godmode-improve/SKILL.md` (self-audit)

If **zero diagnostic runs** exist: print `NO_DIAGNOSTICS_YET` and stop.

---

## 1.5 Run efficiency audit (token + latency)

```bash
uv run python scripts/audit_run_efficiency.py > /tmp/improve_audit.json
cat /tmp/improve_audit.json
```

Reads `data/last_session.json` (written by `/fetchjobs` Step 9e), parses the session JSONL + subagent task outputs, attributes WebFetch calls into waste buckets (`redirect_tax`, `board_returns`, `js_empty_workday`, `pre_known_dead`), and computes parallelism candidates. The `verification` block must report `ok: true` — if not, the audit halts with `audit_failed: true` and you skip the EFFICIENCY pain-points below. **Never invent counts from a failed audit.**

Hand the JSON to Step 3 — drives `WASTED_FETCH_RATE`, `REDIRECT_LATENCY_TAX`, `SUBAGENT_TOKEN_BLOAT`, `LATENCY_CRITICAL_PATH`.

**Subagent baseline caveat:** the current `expected = 5000 + 2000*tool_count` baseline is preliminary. Subagents inherit cached prompt context that inflates real-world tokens; `bloat_delta_pct` will frequently exceed 100% even on legitimate work. Treat `SUBAGENT_TOKEN_BLOAT` proposals as LOW severity until the baseline is recalibrated from 3+ runs of observed data.

---

## 1.7 Nuanced feedback synthesis + cross-run efficacy

```bash
uv run python scripts/synthesize_feedback_patterns.py prepare > /tmp/synth_prep.json
uv run python scripts/audit_feedback_efficacy.py > /tmp/efficacy.json
```

**Synthesizer (`/tmp/synth_prep.json`):** Loads status-disentangled bad-set (`user_feedback='bad'` OR (`weight ≤ 30` AND `status NOT IN {Applied,InProgress,Closed,Won}`) OR `status='NotForMe'`) and good-set (`user_feedback='good'` OR `weight ≥ 70` OR `status IN {Applied,InProgress,Closed,Won}`) from the DB along with full `description` text. The middle clause of bad-set is load-bearing: a Closed-with-low-weight row is "stop showing me THIS specific row," not "I dislike this genre" — applied-intent dominates the genre signal. The same rule applies in the `validate` subcommand's counter-evidence and confound checks. If a set has `sufficient_for_synthesis: true` (count ≥ `min_sample` default 3), the JSON contains an `llm_prompt_template` field — **YOU (the /improve LLM) execute that prompt verbatim** to generate hypothesis proposals across four orthogonal axes (company/skill/domain/problem_type). Write your output to `/tmp/proposals.json` as a JSON list of hypothesis objects matching the schema in `synthesizer_version` 1.0.

Then validate:

```bash
uv run python scripts/synthesize_feedback_patterns.py validate --proposals /tmp/proposals.json > /tmp/validated.json
```

The validator runs five adversarial gates in Python (NOT in your head): evidence-citation substring check, counter-evidence scan against the opposite set, ≥80% confounding check against `priority_domains`, and confidence-downgrade rules. Only hypotheses with `verdict: PASS` and `ready_for_human_approval: true` make it to Step 4 as `PATTERN_INCLINATION_FOUND` / `PATTERN_DISINCLINATION_FOUND` / `PATTERN_LEARN_SKILL_FOUND` proposals.

If both sets are insufficient (typical for cold-start), the synthesizer emits `skip_reason: insufficient_<bad|good>_data` and PATTERN_* pain-points are skipped this run.

**Efficacy report (`/tmp/efficacy.json`):** Computes positive_followup_rate, negative_followup_avoidance_rate, and the user_reaction_matrix from `data/history/jobs_history.db` snapshots. The matrix counts `CORRECT_HIGH`, `MISCALIBRATED_HIGH`, `MISSED_HIGH`, `NO_SIGNAL` PLUS lifecycle buckets `APPLIED_OPEN`, `APPLIED_CLOSED`, `APPLIED_WON`, `EXPLICIT_NOTFORME`. **Critical disentanglement:** rows whose status is in {Applied,InProgress,Closed,Won,NotForMe} are short-circuited into the lifecycle buckets BEFORE scoring-drift logic runs — a high-scored Closed row is NOT MISCALIBRATED_HIGH (system was right to surface; outcome was external). Only `MISCALIBRATED_HIGH > 1` drives `SCORING_DRIFT_DETECTED`. `APPLIED_CLOSED` is informational (life-event), surfaced via `LOW_APPLY_CONVERSION` below. **These metrics are observability only — never auto-tune against them** (the script's `_observability_note` field warns of the reward-hacking risk).

If snapshot DB has < 2 entries, efficacy report emits `audit_failed: true` and you skip the FEEDBACK_EFFICACY pain-points.

---

## 1.8 Idempotency — dedup against improve_log

```bash
uv run python -c "
import json, pathlib, datetime
p = pathlib.Path('data/improve_log.jsonl')
entries = [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()] if p.exists() else []
# Pain-points approved in the last 14 days are considered 'recently closed' and should not re-propose
# unless their evidence metric has materially worsened (≥10% delta from the value recorded in the log).
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)).isoformat()
recent = [e for e in entries if e.get('timestamp', '') >= cutoff]
print(json.dumps({'recent_closed_pain_points': sorted({e['pain_point'] for e in recent}), 'recent_evidence_by_pp': {e['pain_point']: e.get('evidence', '') for e in recent}}, indent=2))
" > /tmp/improve_dedup.json
cat /tmp/improve_dedup.json
```

In Section 3 pain-point detection, BEFORE printing each FOUND pain-point: check the `recent_closed_pain_points` list. If the pain-point id appears there AND the current metric value is within 10% of the logged evidence value (i.e., no material drift), DROP it from the FOUND set and instead include it under a `SUPPRESSED (recently closed):` section. Print the suppressed list at the bottom of PAIN_POINT_REPORT so the user can see what was filtered.

If the user wants to force re-propose anyway, they can pass an explicit override (e.g., `/improve --force <PAIN_POINT_ID>`) — but no auto-resurfacing inside the 14-day window without ≥10% metric worsening.

**Note:** `data/improve_changes.jsonl` (written by `job_finder.improve_changes.apply_proposal`) and `data/improve_log.jsonl` (legacy mirror) are both written on every apply — dedup in this section reads `improve_log.jsonl` for back-compat. Don't migrate dedup off it without a coordinated change.

---

## 2. Compute trend metrics (last 5 runs or all)

| Metric | Formula |
|--------|---------|
| `avg_zero_result_queries` | mean `zero_result_queries` |
| `avg_content_failed_rate` | mean `content_failed / max(candidates_extracted,1)` |
| `avg_board_page_hits` | mean `board_page_hits` |
| `avg_lever_css_fallback` | mean `lever_css_fallback_count` |
| `avg_valid_jobs` | mean `valid_jobs` |
| `pct_high_score` | mean `score_distribution["90-100"] / max(valid_jobs,1)` |
| `linkedin_ats_ratio` | mean `linkedin_with_ats / max(linkedin_discovered,1)` |
| `avg_manual_rescues` | mean `manual_rescues` |
| `domain_coverage_gaps` | domains in `priority_domains` at zero ≥ 3 consecutive runs |
| `max_skill_chars` | `wc -c` on each skill/command file; flag any > 10000 |
| `cross_tool_similarity` | flag pairs: fetchjobs↔jobsearch, setup↔setup_from_resume if char counts within 15% |

---

## 3. Pain point detection

| ID | Trigger | Severity |
|----|---------|---------|
| `SEARCH_TOO_NARROW` | `avg_zero_result_queries > 2 AND (avg_zero_result_queries / max(avg_websearch_calls, 1) > 0.25)` — Rate guard prevents firing when many specialized queries legitimately return zero. | HIGH |
| `LINK_VALIDATION_AGGRESSIVE` | `avg_content_failed_rate > 0.30` AND `avg_manual_rescues > 1` | MEDIUM |
| `BOARD_PAGE_LEAKAGE` | `(avg_board_page_hits / max(avg_valid_jobs,1)) > 0.5 AND avg_board_page_hits >= 4` — ratio-guarded to skip mega-runs where absolute count is dwarfed by valid yield (audit 2026-05-19) | HIGH |
| `LEVER_CSS_FALLBACK_HIGH` | `avg_lever_css_fallback > 3` | MEDIUM |
| `LOW_YIELD` | `avg_valid_jobs < 5` | HIGH |
| `SCORING_TOO_STRICT` | `pct_high_score < 0.10` AND `avg_valid_jobs > 5` | MEDIUM |
| `LINKEDIN_ATS_WEAK` | `linkedin_discovered >= 8 AND (linkedin_with_ats / linkedin_discovered) < 0.25` — denominator-guarded; ≤5 discovered makes ratio meaningless (audit 2026-05-19) | LOW |
| `DOMAIN_BLIND_SPOT` | domain in `priority_domains` with `domain_coverage_gaps` | HIGH |
| `CANDIDATE_PROFILE_DRIFT` | `bad_feedback_title_tokens has ≥ 1 token with frequency ≥ 4 AND token NOT in candidate_info.json['golden_keywords'] AND token NOT in candidate_info.json['noise_keywords']` — Raised from 3 to 4 and excluded golden_keywords to avoid suppressing incidental skills shared across bad-set jobs that differ by company/seniority, not topic. | MEDIUM |
| `WISDOM_STALE` | wisdom in `candidate_info.json` unchanged across 3+ run dates | LOW |
| `SKILL_STALE_REFERENCE` | skill file references removed field, dead path, or hardcoded year | MEDIUM |
| `SKILL_TOKEN_BLOAT` | any skill/command file > 10000 chars, OR a cross-tool pair flagged above | MEDIUM |
| `WASTED_FETCH_RATE` | from `/tmp/improve_audit.json`: `(redirect_tax + board_returns + js_empty_workday + pre_known_dead) / webfetch_calls > 0.30` | MEDIUM |
| `REDIRECT_LATENCY_TAX` | `redirect_tax.count ≥ 3` (one run) OR `≥ 2` for 3 consecutive runs | LOW |
| `SUBAGENT_TOKEN_BLOAT` | any subagent `tokens > 10000` AND `bloat_delta_pct > 50` — **LOW until baseline recalibrated from 3+ runs** | LOW |
| `LATENCY_CRITICAL_PATH` | any single tool call's `duration_ms > 0.4 × wall_seconds` | LOW |
| `PATTERN_INCLINATION_FOUND` | from `/tmp/validated.json`: at least one inclination hypothesis with `verdict: PASS` AND `ready_for_human_approval: true` | MEDIUM |
| `PATTERN_DISINCLINATION_FOUND` | same — disinclination direction | MEDIUM |
| `PATTERN_LEARN_SKILL_FOUND` | same — learn_skill direction | LOW |
| `SCORING_DRIFT_DETECTED` | from `/tmp/efficacy.json`: `user_reaction_matrix.MISCALIBRATED_HIGH ≥ 2` (system surfaced ≥2 high-scored jobs the user EXPLICITLY downvoted — Applied/Closed/Won rows are NOT counted; they're life-events, not drift) | HIGH |
| `LOW_APPLY_CONVERSION` | from `/tmp/efficacy.json`: `APPLIED_CLOSED / max(APPLIED_OPEN + APPLIED_CLOSED + APPLIED_WON, 1) > 0.85 AND APPLIED_OPEN + APPLIED_CLOSED + APPLIED_WON ≥ 8` — Raised from 0.7 to 0.85 (closure ≤ 70% is normal life-event noise) and sample floor from 5 to 8 (need real sample before warning). (informational — many applications closing without offers; may indicate level/genre mismatch or pipeline issues outside system control) | LOW |
| `LIFECYCLE_DEDUP_LEAK` | from `/fetchjobs` run diagnostics: any `dropped_terminal_status` count > 0 across last 3 runs but the same link re-appears in scored output (means the Step 3 lifecycle-status dedup is being bypassed) | MEDIUM |
| `NEGATIVE_FOLLOWUP_AVOIDANCE_LOW` | `negative_followup_avoidance_rate < 0.7` AND `negative_set_size ≥ 3` | MEDIUM |
| `POSITIVE_FOLLOWUP_RATE_LOW` | `positive_followup_rate < 0.3` AND `positive_set_size >= 15` — n≥15 for Wilson 95% CI half-width ≤0.21 at r=0.3 (audit 2026-05-19) | LOW |
| `PRUNER_FPR_ALERT` | most recent run diagnostics has `pruner_fpr_alert: true` (>5% of prior-pruned links re-checked alive) | HIGH |

**Print:**

```text
PAIN_POINT_REPORT (N runs, <date> → <date>)
FOUND:  [SEVERITY] ID  — metric = value
CLEAR:  ID  ✓  metric = value
```

If nothing found: `SYSTEM_HEALTHY` and stop.

---

## 4. Proposals (highest severity first)

For each pain point, present:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROPOSAL N/TOTAL  |  <ID> [SEVERITY]
Evidence: <metric = value>
File: <path>  |  Section: <heading>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT:
"""<exact text>"""

PROPOSED:
"""<replacement>"""

<one sentence: what this fixes and why>
Approve? [yes / no / modify: …]
```

### 4.1 Audit-only serialization

When invoked as `--audit-only`, do **not** print the CURRENT/PROPOSED block. Instead, for each pain point that survived dedup, construct a proposal dict and call `job_finder.improve_changes.write_proposal(...)`:

```python
import json, subprocess
# Example for SEARCH_TOO_NARROW (text_replace on fetchjobs.md):
proposal = {
    "pain_point": "SEARCH_TOO_NARROW",
    "severity": "HIGH",
    "summary": "<one-line summary of what the change does and why>",
    "evidence": {"metric": "avg_zero_result_queries", "value": <value>, "context": "<one sentence>"},
    "file_changed": ".claude/commands/fetchjobs.md",
    "patch": {
        "type": "text_replace",
        "old_string": "<exact current text>",
        "new_string": "<exact replacement>",
    },
}
result = subprocess.run(
    ["uv", "run", "python", "-c",
     f"from job_finder import improve_changes; print(improve_changes.write_proposal({json.dumps(proposal)}))"],
    capture_output=True, text=True, check=True,
)
print("staged:", result.stdout.strip())
```

For JSON proposals (PATTERN_*, DOMAIN_BLIND_SPOT, LOW_YIELD, CANDIDATE_PROFILE_DRIFT, POSITIVE_FOLLOWUP_RATE_LOW), use `patch.type = "json_append"` with `key_path` (e.g. `["inclinations"]`) and `appended_items` (a list of items to add). For `json_set`, use `key_path`, `old_value`, `new_value`.

Stage every detected pain point this way. End the run by printing:

```text
AUDIT_SUMMARY
Proposals staged: N
Review them in Streamlit → Analytics → Pending Improvements.
```

Do NOT print individual CURRENT/PROPOSED blocks in this mode — the UI shows them.

**Pain-point → file mapping:**

| ID | Target | Action |
|----|--------|--------|
| `SEARCH_TOO_NARROW` | `fetchjobs.md` §Query building | Add 2-keyword fallback query strategy; cite `zero_result_query_strings` from diagnostics to name the specific failing queries |
| `LINK_VALIDATION_AGGRESSIVE` | `validate-job-links/SKILL.md` | Narrow dead-phrase list |
| `BOARD_PAGE_LEAKAGE` | `fetchjobs.md` §board-index detection | Expand URL signal list |
| `LEVER_CSS_FALLBACK_HIGH` | `fetchjobs.md` §Lever/Ashby | Add aggregator fallback targets |
| `LOW_YIELD` | `candidate_info.json` | Add board to `search_targets` or broaden `golden_keywords` |
| `SCORING_TOO_STRICT` | `fetchjobs.md` §scoring | Lower moat-match threshold |
| `LINKEDIN_ATS_WEAK` | `fetchjobs.md` §ATS backfill | Add `site:ashbyhq.com` to backfill queries |
| `DOMAIN_BLIND_SPOT` | `candidate_info.json` | Add domain-specific board to `search_targets` |
| `CANDIDATE_PROFILE_DRIFT` | `candidate_info.json` | Append pattern to `noise_keywords` |
| `WISDOM_STALE` | `fetchjobs.md` §Wisdom | Add diff-vs-prior instruction |
| `SKILL_STALE_REFERENCE` | Affected skill file | Replace stale field/path |
| `SKILL_TOKEN_BLOAT` | Bloated file | Apply compression: prose→bullets, remove hedge words ("you should", "make sure to", "it is important that"), remove "Why this is needed" sections, convert multi-paragraph explanations to a single line, fold constraint blocks inline. For cross-tool duplicate pairs, propose making the Cursor file a thin wrapper: frontmatter + one sentence delegating to the canonical `.claude/commands/` file + any Cursor-specific delta only. |
| `WASTED_FETCH_RATE` | `fetchjobs.md` §Wave 1/2 | Propose URL-rewrite or skip rules grounded in the actual offending URLs from `/tmp/improve_audit.json.waste`. **MUST include counter-metric check**: cite that `valid_jobs` in prior runs would not have dropped under the proposed rule. |
| `REDIRECT_LATENCY_TAX` | `fetchjobs.md` §Wave 2 | Add pre-flight URL canonicalization (e.g. `boards.greenhouse.io` → `job-boards.greenhouse.io` before WebFetch). Cite the specific redirect chains observed. |
| `SUBAGENT_TOKEN_BLOAT` | `fetchjobs.md` §Persistence Agent prompt | Propose moving inline payloads into files the subagent reads by path (mirrors the existing `/tmp/fetchjobs_scored.json` pattern). LOW severity until baseline recalibrated. |
| `LATENCY_CRITICAL_PATH` | `fetchjobs.md` §Discovery Team | Surface the slow tool call (URL/operation) for human review. Auto-rewrites are NOT proposed — data-dependency analysis is unreliable. |
| `PATTERN_INCLINATION_FOUND` | `candidate_info.json` `inclinations` | Append PASS-verdict hypothesis via `uv run python scripts/synthesize_feedback_patterns.py apply --validated /tmp/validated.json --to inclinations`. Soft scoring bias only — discovery untouched. |
| `PATTERN_DISINCLINATION_FOUND` | `candidate_info.json` `disinclinations` | Same — `--to disinclinations`. **Must NOT propose adding pattern tokens to `noise_keywords`** (that's the foot-gun Item A fixed). |
| `PATTERN_LEARN_SKILL_FOUND` | `candidate_info.json` `learn_skills` | Same — `--to learn_skills`. |
| `SCORING_DRIFT_DETECTED` | `fetchjobs.md` §Scientific Moat Evaluation | Show the MISCALIBRATED_HIGH examples from `/tmp/efficacy.json.user_reaction_matrix.miscalibrated_examples`. Propose tightening the moat-match criteria or revising the 90-100/70-89 thresholds based on the actual pattern. **Do NOT touch the discovery layer.** |
| `LOW_APPLY_CONVERSION` | (informational — no automatic file edit) | Surface the APPLIED_CLOSED / APPLIED_OPEN / APPLIED_WON breakdown and `applied_closed_examples` from `/tmp/efficacy.json` for human review. **Do NOT propose narrowing discovery or scoring** — applied-then-closed is overwhelmingly a life-event signal (level mismatch, pipeline timing, market regime) and tightening the system rewards reward-hacking on observability. The only valid auto-action is a one-line note to the user; leave any moat/discovery edits to the human. |
| `NEGATIVE_FOLLOWUP_AVOIDANCE_LOW` | `fetchjobs.md` §Query building | Propose adding excluded-terms / `-keyword` patterns derived from the negative set's actual contents. Validate that excluded terms don't appear in any `weight ≥ 70` job. |
| `POSITIVE_FOLLOWUP_RATE_LOW` | `fetchjobs.md` §peer_companies organic search | Propose adding companies/themes from the positive set to `peer_companies` (writes to `candidate_info.json`). |
| `PRUNER_FPR_ALERT` | `src/job_finder/link_validation.py` | Surface the resurrected links from `data/pruned_history.jsonl` re-check. Propose narrowing the most-aggressive heuristic that produced false positives (e.g. raise `MIN_BODY_CHARS`, narrow `DEAD_PAGE_PHRASES`). HIGH severity. |

**Profile edits:** Show full proposed JSON value. Write via Python preserving all other keys:

```bash
uv run python -c "import json,pathlib; p=pathlib.Path('data/candidate_info.json'); cfg=json.loads(p.read_text()); cfg['<key>']= <value>; p.write_text(json.dumps(cfg,indent=2))"
```

---

## 5. Apply approved changes (interactive mode only)

> In `--audit-only` mode, skip this entire section — proposals were serialized in §4.1 and approvals happen in Streamlit → Analytics → Pending Improvements. In `--apply <change_id>` mode, call `job_finder.improve_changes.apply_proposal(change_id)` and exit; the module handles edit + commit + log atomically. The flow below applies only to the original interactive `/improve` without flags.

Per approved proposal:

1. Edit the file (Edit tool for `.md`/`.mdc`, Python for `.json`).
2. Read changed section back to verify.
3. Append to `data/improve_log.jsonl`:

```bash
uv run python -c "
import json,pathlib,datetime
entry={'timestamp':datetime.datetime.utcnow().isoformat(),'pain_point':'<ID>','severity':'<S>','file_changed':'<path>','section':'<h>','evidence':'<metric=val>','approved_by':'user','summary':'<1 sentence>'}
pathlib.Path('data/improve_log.jsonl').open('a').write(json.dumps(entry)+'\n')
"
```

4. Print `✓ Applied: <ID> → <file>`

If user modified the proposal text, apply verbatim and log `"approved_by":"user_modified"`.

---

## 6. Summary

```text
IMPROVE SUMMARY
Proposals: N  |  Applied: N  |  Rejected: N
Files changed: <list>
Expected improvement: <metric> <old> → <target>
Run /fetchjobs to validate.
```

**Self-improvement:** This file and `.cursor/skills/godmode-improve/SKILL.md` are subject to the same process. Propose edits to them if a pain point applies. The skill improves itself.
