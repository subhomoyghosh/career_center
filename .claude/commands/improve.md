# /improve — God Mode Self-Improving Skill

> **Rules:** Never delete skill files. Quote exact current text in every proposal. Ground each proposal in a specific metric. /improve must not re-propose pain-points closed within 14 days unless evidence has worsened ≥10% (see §1.8).
>
> **Auto-apply with auto-revert safety net (revised constitutional rule):** Tier 1 lossless equivalence transforms (hedge removal, prose→bullets, paragraph dedup) auto-apply by construction — they preserve semantics. Tier 2/3/4 (archive moves, cross-file dedup) auto-apply BUT every applied change writes `pre_metrics` (`valid_jobs`, `pct_high_score`, `tokens_per_valid_job`, `main_tokens`) onto its row in `data/improve_changes.jsonl`. On the NEXT /fetchjobs → /improve cycle, the audit's `auto_revert_candidates` block flags any applied change whose post-run metrics regressed (`valid_jobs < pre × 0.85` OR `pct_high_score < pre × 0.85` OR `tokens_per_valid_job > pre × 1.5`); the `--auto` orchestrator calls `revert_change(change_id)` on each such change without asking. This is what makes aggressive compaction safe — the user does not gate every change, the system does, by reading the next run's metrics. Quality regression triggers automatic, atomic, audited rollback via the same `revert_change` machinery that powers manual `/improve --restore`.
>
> **PATTERN_* and SCORING_DRIFT still require human approval:** these modify *behavior* not *cost*, so the regression watchlist (cost metrics) doesn't safely cover them. `PATTERN_*` proposals modify `inclinations` / `disinclinations` / `learn_skills` in `candidate_info.json`; these are SOFT scoring biases in `/fetchjobs` Step 3, NEVER hard filters like `noise_keywords`. `SCORING_DRIFT_DETECTED` proposals modify scoring thresholds. Both stage to Streamlit for human review — auto-apply is OFF for them.
>
> **Quality regression watchlist for TOKEN_*/MAIN_AGENT_* proposals (mandatory):** Every proposal that cuts tokens MUST cite, in the evidence block, the priors P50 of `valid_jobs` and `pct_high_score` from `/tmp/improve_audit.json.priors` AND state the expected post-apply `tokens_per_valid_job` target. The user reads these as the regression contract: "if `valid_jobs` drops below P50 × 0.85 OR `pct_high_score` drops below P50 × 0.85 on the next run, the change failed." Skipping these citations = proposal is incomplete; do not present.
>
> **Continuous compaction is default behavior (not a triggered pain-point):** every `/improve` cycle MUST scan the four compaction tiers (§0.7) and emit a `SKILL_COMPACTION_PLAN` proposal if ANY reclaimable bytes exist — there is no threshold gate. The contract: a pro user running `/fetchjobs` heavily sees `efficiency.main_tokens` and `total_skill_bytes` trend DOWNWARD across consecutive runs, not upward, until compaction headroom is exhausted. Information is never deleted — Tier 3 sections move to `.claude/_archive/` with a 1-line stub, restorable via `/improve --restore <change_id>`. Tier 1 (lossless equivalence transforms) preserves semantics by construction; Tier 2 (example externalization) keeps the rule inline and stubs the example; Tier 4 (cross-file dedup) turns duplicates into thin wrappers pointing at the canonical source.

---

## 0.5. Modes

`/improve` runs in one of three modes:

| Mode | Trigger | Behavior |
|------|---------|----------|
| Interactive (default) | `/improve` | Original flow: walk through Phases 1–6, ask for approval inline per proposal. |
| `--audit-only` | `/improve --audit-only` (auto-dispatched at end of `/fetchjobs` when `auto_improve_audit_enabled` is true) | Run Phases 1–3 only. Phase 4 writes each proposal to `data/improve_proposals.jsonl` via `job_finder.improve_changes.write_proposal(...)` instead of asking inline. Print a one-line summary: "N proposals staged — review in Streamlit → Analytics." Never applies anything. |
| `--apply <change_id>` | `/improve --apply <change_id>` (dispatched from the Streamlit UI when user clicks "Apply approved") | Look up `change_id` via `job_finder.improve_changes.apply_proposal(change_id)`. Print the result JSON. |
| `--restore <change_id>` | `/improve --restore <change_id>` (Streamlit UI offers this on any applied compaction row, OR when the next-run regression check trips the line-5 watchlist) | Call `job_finder.improve_changes.revert_change(change_id, reverted_by="ui_user")`. The module handles edit + commit + log atomically; for `archive_section` patches it reads `semantic_diff.captured_content` and `semantic_diff.original_level` from the change row to reconstitute the section, then unlinks the archive file. Print result JSON. |
| `--auto` | `/improve --auto` (auto-dispatched at end of `/fetchjobs` when `auto_improve_enabled` is true — the new default) | The autonomous loop. (1) Run the audit. (2) Walk `auto_revert_candidates.regressions` and call `revert_change(change_id)` on each — silent self-healing. (3) Walk Tier 1/2/3/4 (§0.7) and auto-apply via `apply_proposal(change_id, approved_by="auto", pre_metrics=<from current audit's efficiency block>)`. (4) Print the brief Auto Summary (§6). PATTERN_* and SCORING_DRIFT_DETECTED proposals are NOT auto-applied — they're staged for human review like `--audit-only` does. |

**Why four non-interactive modes:** Interactive is fine for ad-hoc work but doesn't scale. `--audit-only` stages proposals to Streamlit for human review of high-stakes (PATTERN_/SCORING_DRIFT_) changes. `--apply` is what the UI calls back into when the user green-lights a row. `--restore` is the safety net that makes compaction reversible. `--auto` is the autonomous default for cost-only changes — the user runs `/fetchjobs` and the system silently keeps token cost in check, self-healing on regressions.

**Constitutional rule (revised):** Tier 1 lossless transforms and Tier 2/3/4 compaction auto-apply with the auto-revert safety net (see line 5). PATTERN_* and SCORING_DRIFT_ proposals still require explicit human approval — they change behavior, not just cost, so the cost-metric watchlist doesn't safely cover them.

---

## 0.7. Continuous compaction tiers

Default behavior every `/improve` cycle (see constitutional rule at line 5). The agent walks all four tiers, bundles findings into ONE `SKILL_COMPACTION_PLAN` proposal, and stages it for human review. There is no severity threshold to clear — if any reclaimable bytes exist, the plan ships.

| Tier | What it does | Lossless? | Detected by |
|------|--------------|-----------|-------------|
| **1 — Inline equivalence transforms** | Remove hedge words (`you should`, `please`, `make sure to`, `it is important that`); collapse prose paragraphs of 3+ rules into bullets; drop "Why this is needed" sections that just restate the rule above; dedup identical paragraphs within a file. | Yes (by construction — semantic equivalence) | LLM agent reads file contents directly during `/improve` Phase 1; script does not pre-emit Tier-1 candidates |
| **2 — Example externalization** | Move worked examples (multi-line code blocks introduced by `Example:`, `Sample:`, fenced blocks > 15 lines under a rule) to `.claude/_archive/<file-stem>__<heading-slug>__example.md`. Replace inline with a 1-line stub pointing at the archive. Rule itself stays inline. | Yes — example is preserved + referenced | LLM agent during Phase 1; archive operation is a `patch.type = "extract_example"` sub-patch |
| **3 — Cold section archive** | Move whole sections whose backtick fingerprints did not appear in recent session activity to `.claude/_archive/<file-stem>__<heading-slug>.md`. Replace with stub `### <Heading> *(archived: see <path>; restore via /improve --restore <change_id>)*`. | Yes — full content preserved in archive; restorable in one command | Script: `/tmp/improve_audit.json.cold_sections.candidates` (`SKILL_COLD_SECTION_FOUND` pain-point) |
| **4 — Cross-file dedup → thin wrapper** | When a `.cursor/skills/*/SKILL.md` or `.cursor/rules/*.mdc` file mirrors `.claude/commands/*.md` content (≥ 70% line-overlap), collapse the Cursor file to frontmatter + one sentence delegating to the canonical `.claude/` file + any Cursor-specific delta only. | Yes — canonical content is reachable; Cursor file becomes pointer | LLM agent during Phase 1; or the existing `SKILL_TOKEN_BLOAT` cross-tool-pair signal from `cross_tool_similarity` in §2 |

**Pro user contract:** `efficiency.main_tokens` and `total_skill_bytes` should be non-increasing across consecutive `/fetchjobs` cycles, until compaction headroom is exhausted. The `compaction_trend` block in the audit output (`/tmp/improve_audit.json.compaction_trend`) tracks the 3-run series; `stagnant: true` AND cold-candidates-exist means the user has stopped approving compaction proposals, and the LOW-severity `COMPACTION_STAGNATION` pain-point (§3) fires as a friendly nudge.

**Restore is the safety net:** every Tier 2/3/4 apply writes a `restore_manifest` to `data/improve_changes.jsonl`. If next-run `valid_jobs` or `pct_high_score` drops below P50 × 0.85, /improve emits a `REGRESSION_DETECTED` pain-point pointing at the offending `change_id`; one-command restore. This is what makes "aggressive compaction" safe — you can always go back.

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

**Subagent baseline (recalibrated):** the audit now computes a **within-run** baseline — `subagent_run_stats.median_tokens_per_tool` across all subagents in the same run — and annotates each subagent with `relative_to_run_median_ratio` and `internal_outlier` (true when ratio ≥ 2.0). `SUBAGENT_TOKEN_BLOAT` fires off `internal_outlier`, not the legacy hardcoded `bloat_delta_pct` (which remains in output for back-compat only). Severity is MEDIUM when `subagent_run_stats.n ≥ 2` (peer comparison meaningful), LOW otherwise (single-subagent runs have no peers — no flag). The cross-run empirical baseline is deferred until per-subagent token detail is persisted to `run_diagnostics.jsonl` — the within-run baseline is the load-bearing signal in the meantime.

**New efficiency + priors blocks:** the audit JSON now carries an `efficiency` block (current run: `main_tokens`, `valid_jobs`, `tokens_per_valid_job`, `cache_hit_rate`, `subagent_token_share`, `pct_high_score`) and a `priors` block (last 5 runs, current excluded: P50/P90 of `tokens_per_valid_job`, `main_tokens`, plus `cache_utilization_p50`, `valid_jobs_p50`, `pct_high_score_p50`). The priors block separates `n_priors_seen` (sample size) from `n_priors_used` (priors with usable token data). Pain-point thresholds key off `n_priors_used ≥ 3` — older diagnostics rows predate token capture and are NOT counted as a zero-token baseline.

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
| `SUBAGENT_TOKEN_BLOAT` | any subagent with `internal_outlier: true` (tokens-per-tool-call ≥ 2× run median) AND `tokens > 10000` — keys off the within-run baseline, not the legacy hardcoded heuristic | MEDIUM (LOW if `subagent_run_stats.n < 2` — no peers to compare against) |
| `TOKENS_PER_VALID_JOB_HIGH` | from `/tmp/improve_audit.json`: `priors.n_priors_used ≥ 1` AND `efficiency.valid_jobs ≥ 5` AND ratio test depending on n: **n=1**: `efficiency.tokens_per_valid_job > 2.0 × priors.tokens_per_valid_job_p50` (conservative — single prior is noisy). **n=2**: `> 1.75 ×`. **n ≥ 3**: `> 1.5 ×` (calibrated). Regression-proof: denominator (`valid_jobs`) must hold while numerator (tokens) drops | n=1: LOW · n=2: MEDIUM · n≥3: HIGH |
| `MAIN_AGENT_CONTEXT_BLOAT` | `priors.n_priors_used ≥ 1` AND threshold by n: **n=1**: `efficiency.main_tokens > 1.5 × priors.main_tokens_p50` (single-prior comparison). **n=2**: `> 1.3 × p50`. **n ≥ 3**: `> priors.main_tokens_p90` (calibrated). Where the user's "huge token spend" lives, since subagents are typically <10% share | n=1: LOW · n=2: MEDIUM · n≥3: MEDIUM |
| `MAIN_AGENT_CACHE_MISS_HIGH` | `efficiency.cache_hit_rate < 0.5` AND `efficiency.main_tokens > 20000` AND `priors.n_priors_used ≥ 1` AND (when n=1: `priors.cache_utilization_p50 > 0.7` — we need evidence prior runs cached well; without that signal one run of low cache could just be the cold-start of caching). main_tokens floor prevents firing on tiny no-op runs | n=1: LOW · n=2: MEDIUM · n≥3: MEDIUM |
| `SKILL_COLD_SECTION_FOUND` | from `/tmp/improve_audit.json.cold_sections.candidates`: at least one entry AND `n_runs_seen ≥ 1`. Individual section-level proposal; primarily surfaced as a sub-item of `SKILL_COMPACTION_PLAN`. Archive-not-delete (see §0.7 Tier 3). | MEDIUM |
| `SKILL_COMPACTION_PLAN` | **Always-on (no threshold gate).** Fires every run if ANY of the four §0.7 tiers has ≥ 1 candidate. Aggregates Tier 1 (inline transforms found by LLM during Phase 1), Tier 2 (example externalizations), Tier 3 (`cold_sections.candidates` from audit), and Tier 4 (cross-file dedup candidates from §2 `cross_tool_similarity`) into ONE auditable proposal (single approval row in Streamlit). Apply is one transactional operation writing (a) all archive files, (b) all stub replacements, (c) one `change_id` with a `restore_manifest`. The line-5 regression watchlist applies. | HIGH |
| `COMPACTION_STAGNATION` | from `/tmp/improve_audit.json.compaction_trend`: `stagnant == true` AND `n_runs_seen == 3` AND `cold_sections.candidates` is non-empty — `total_skill_bytes` has not decreased across 3 consecutive runs despite cold candidates existing. Means the user stopped approving compaction proposals (or the agent stopped emitting them). Friendly nudge; surfaces the bytes-not-saved + the change_ids of pending compaction proposals so the user can see what's queued. | LOW |
| `REGRESSION_DETECTED` | from `data/improve_changes.jsonl` cross-referenced with `/tmp/improve_audit.json.efficiency` AND `priors`: the most recent applied compaction change has `next_run_valid_jobs < priors.valid_jobs_p50 × 0.85` OR `next_run_pct_high_score < priors.pct_high_score_p50 × 0.85` — the line-5 watchlist tripped. Surface the offending `change_id`, the watchlist deltas, and the `/improve --restore <change_id>` command. Do NOT auto-restore; surface as the user's call. | HIGH |
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
| `SUBAGENT_TOKEN_BLOAT` | `fetchjobs.md` §Persistence Agent prompt (or whichever subagent fired) | Propose moving inline payloads into files the subagent reads by path (mirrors the existing `/tmp/fetchjobs_scored.json` pattern). Cite the offending subagent's `relative_to_run_median_ratio` and `tool_count` from the audit, and the specific tool names that dominate its token spend (use `--verbose` for per-turn detail). |
| `TOKENS_PER_VALID_JOB_HIGH` | Largest contributor identified from audit: a specific waste bucket in `fetchjobs.md` or a specific `internal_outlier` subagent's prompt | Rank waste buckets + `internal_outlier` subagents by token contribution; propose the single biggest contributor for externalization or trimming. The proposal MUST cite (a) current `tokens_per_valid_job`, (b) `priors.tokens_per_valid_job_p50`, (c) the ratio, (d) target post-value, AND (e) priors P50 of `valid_jobs` + `pct_high_score` as the regression watchlist (mandatory per line 5 rule). Counter-metric check is non-negotiable: name the change and explain why `valid_jobs` would not drop. |
| `MAIN_AGENT_CONTEXT_BLOAT` | `fetchjobs.md` and skill files referenced via `cursor/skills/*` | Surface the gap between `efficiency.main_tokens` and `productive_tokens` (= context that did not produce useful output). Propose specific sections to compress. Rank candidates by: sections with low referenced-output-token contribution but high static file size. **Do NOT propose removing functional steps** — only narrative scaffolding, redundant rephrasing, and worked examples that duplicate the rules above them. Include the line 5 regression-watchlist citation. |
| `MAIN_AGENT_CACHE_MISS_HIGH` | `fetchjobs.md` §system prompt + tool/skill loading order | Diagnose first, do not auto-propose: re-run `audit_run_efficiency.py --verbose` and inspect `_turn_detail` for which turn's `cache_creation_tokens` spiked. The cause is almost always a non-stable prefix — `data/candidate_info.json`, today's date, or `data/last_session.json` content being injected BEFORE the invariant skill bodies, breaking cache hits across turns. Propose reordering to move volatile context AFTER stable headers. If the cause is unclear from `_turn_detail`, the proposal MUST surface "needs human inspection" rather than guess. |
| `LATENCY_CRITICAL_PATH` | `fetchjobs.md` §Discovery Team | Surface the slow tool call (URL/operation) for human review. Auto-rewrites are NOT proposed — data-dependency analysis is unreliable. |
| `PATTERN_INCLINATION_FOUND` | `candidate_info.json` `inclinations` | Append PASS-verdict hypothesis via `uv run python scripts/synthesize_feedback_patterns.py apply --validated /tmp/validated.json --to inclinations`. Soft scoring bias only — discovery untouched. |
| `PATTERN_DISINCLINATION_FOUND` | `candidate_info.json` `disinclinations` | Same — `--to disinclinations`. **Must NOT propose adding pattern tokens to `noise_keywords`** (that's the foot-gun Item A fixed). |
| `PATTERN_LEARN_SKILL_FOUND` | `candidate_info.json` `learn_skills` | Same — `--to learn_skills`. |
| `SCORING_DRIFT_DETECTED` | `fetchjobs.md` §Scientific Moat Evaluation | Show the MISCALIBRATED_HIGH examples from `/tmp/efficacy.json.user_reaction_matrix.miscalibrated_examples`. Propose tightening the moat-match criteria or revising the 90-100/70-89 thresholds based on the actual pattern. **Do NOT touch the discovery layer.** |
| `LOW_APPLY_CONVERSION` | (informational — no automatic file edit) | Surface the APPLIED_CLOSED / APPLIED_OPEN / APPLIED_WON breakdown and `applied_closed_examples` from `/tmp/efficacy.json` for human review. **Do NOT propose narrowing discovery or scoring** — applied-then-closed is overwhelmingly a life-event signal (level mismatch, pipeline timing, market regime) and tightening the system rewards reward-hacking on observability. The only valid auto-action is a one-line note to the user; leave any moat/discovery edits to the human. |
| `NEGATIVE_FOLLOWUP_AVOIDANCE_LOW` | `fetchjobs.md` §Query building | Propose adding excluded-terms / `-keyword` patterns derived from the negative set's actual contents. Validate that excluded terms don't appear in any `weight ≥ 70` job. |
| `POSITIVE_FOLLOWUP_RATE_LOW` | `fetchjobs.md` §peer_companies organic search | Propose adding companies/themes from the positive set to `peer_companies` (writes to `candidate_info.json`). |
| `PRUNER_FPR_ALERT` | `src/job_finder/link_validation.py` | Surface the resurrected links from `data/pruned_history.jsonl` re-check. Propose narrowing the most-aggressive heuristic that produced false positives (e.g. raise `MIN_BODY_CHARS`, narrow `DEAD_PAGE_PHRASES`). HIGH severity. |
| `SKILL_COLD_SECTION_FOUND` | the source file at the cold section | Sub-item of `SKILL_COMPACTION_PLAN`; rarely surfaced standalone. Per Tier 3 in §0.7: archive the section to `.claude/_archive/<file-stem>__<heading-slug>.md`; replace inline with stub `### <Heading> *(archived: see <archive_path>; restore via /improve --restore <change_id>)*`. Proposal evidence MUST cite: bytes, n_fingerprints, total_refs_in_window, file_p75_bytes_per_ref, and the §0.7 Tier 3 contract (information preserved in archive). |
| `SKILL_COMPACTION_PLAN` | aggregate across all skill/command files | **Always-on.** Every `/improve` cycle, walk §0.7 Tiers 1–4 and bundle ALL findings into ONE proposal. Tier 1 candidates come from the LLM agent's Phase-1 read of file contents (hedge-word scan, prose→bullet collapse, paragraph dedup); Tier 2 from the same read (worked-example detection — fenced blocks > 15 lines under a rule or sections introduced by `Example:` / `Sample:`); Tier 3 from `cold_sections.candidates`; Tier 4 from §2 `cross_tool_similarity`. Apply writes (a) all archive files for Tiers 2–3, (b) all in-place edits for Tiers 1+4, (c) one `change_id` in `data/improve_changes.jsonl` carrying a `restore_manifest` listing every (source_path, archive_path, original_heading, original_text) tuple. The proposal MUST cite total bytes reclaimed AND the line-5 regression watchlist. Severity HIGH because this is the load-bearing token-saving mechanism. |
| `COMPACTION_STAGNATION` | informational (no file edit) | Surface `total_skill_bytes_series` from `compaction_trend`, the count of `cold_sections.candidates` that have been staged but not approved, and the change_ids of pending compaction proposals in `data/improve_proposals.jsonl`. Friendly nudge: "you have N bytes of reclaimable context queued; approve via Streamlit → Analytics → Pending Improvements." Never auto-applies. |
| `REGRESSION_DETECTED` | informational (no file edit) | Surface (a) the offending `change_id`, (b) the watchlist deltas (`valid_jobs` and `pct_high_score` pre/post), (c) the exact `restore_manifest` of files that would be reverted, (d) the one-command undo: `/improve --restore <change_id>`. The user decides — do not auto-restore. If user does restore, the next /improve cycle dedup will treat the restored section as "approved-not-cold" for the 14-day window to prevent re-proposing the same archive. |

**Profile edits:** Show full proposed JSON value. Write via Python preserving all other keys:

```bash
uv run python -c "import json,pathlib; p=pathlib.Path('data/candidate_info.json'); cfg=json.loads(p.read_text()); cfg['<key>']= <value>; p.write_text(json.dumps(cfg,indent=2))"
```

---

## 5. Apply approved changes (interactive mode only)

> Mode dispatch: `--audit-only` → skip this section (proposals serialized in §4.1 for Streamlit approval). `--apply <change_id>` → call `apply_proposal(change_id)` and exit. `--restore <change_id>` → call `revert_change(change_id, reverted_by="ui_user")` and exit. `--auto` → run the auto-orchestration loop (§5.5). The numbered steps below apply only to the interactive default.

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

---

## 5.5 `--auto` orchestration (autonomous mode)

Triggered automatically at the end of every `/fetchjobs` run when `auto_improve_enabled` is true (the default for pro users). The orchestrator is the /improve LLM itself; the script provides the data, the LLM does the dispatch. No human gate for cost-only changes — but every change is regression-checked on the next run and silently reverted if quality drops.

**Sequence:**

1. **Audit.** `uv run python scripts/audit_run_efficiency.py > /tmp/improve_audit.json`. If `audit_failed: true`, abort the cycle and print one line: `AUTO_ABORTED: audit_failed (<reason>)`. Do not apply or revert anything.

2. **Self-heal (auto-revert).** Walk `auto_revert_candidates.regressions` from the audit. For each entry:
   ```bash
   uv run python -c "from job_finder import improve_changes; print(improve_changes.revert_change('<change_id>', reverted_by='auto_regression_guard'))"
   ```
   Capture (a) change_id, (b) regression_reasons (already human-readable, include the threshold), (c) revert result. If revert fails (e.g., source file no longer in expected state), log the failure but continue — do NOT halt the whole cycle.

3. **Mark validated.** Walk `auto_revert_candidates.validated`. For each, append `{"change_id": "...", "validated_at": "<iso>"}` to a `validations` log so future audits don't re-check the same row. (Schema: extend `data/improve_changes.jsonl` rows by appending a new line with `{"change_id", "validated_at"}` keyed to the original.)

4. **Walk compaction tiers.** For each Tier 1–4 candidate (§0.7), generate a proposal dict using the same shape as `--audit-only` (§4.1), then immediately apply:
   ```bash
   uv run python -c "from job_finder import improve_changes; print(improve_changes.apply_proposal('<change_id>', approved_by='auto', pre_metrics=<from audit.efficiency>))"
   ```
   - `pre_metrics` MUST be the JSON-serialized current `efficiency` block from the audit. This is what powers the next-run regression check.
   - For Tier 3 (`archive_section`), the proposal's `archive_path` must resolve under `.claude/_archive/`; `apply_proposal` validates this and refuses otherwise.
   - Track `total_bytes_reclaimed` += (source section bytes − stub bytes) for the summary.

5. **Stage human-gated proposals.** PATTERN_*, SCORING_DRIFT_DETECTED, REGRESSION_DETECTED, COMPACTION_STAGNATION, LOW_APPLY_CONVERSION, LATENCY_CRITICAL_PATH, PRUNER_FPR_ALERT → call `write_proposal()` to stage to Streamlit; do NOT auto-apply.

6. **Brief summary.** Print the Auto Summary block (§6 below). Keep it under 12 lines — the user reads it at the end of every /fetchjobs cycle.

**Safety invariants the orchestrator must NOT violate:**

- Never auto-apply a Tier 3 archive if the section's `n_fingerprints == 0` (unmeasurable — the absence of refs is meaningless).
- Never auto-apply if `efficiency.valid_jobs == 0` for the current run (the run failed; don't compact based on a broken signal).
- Never auto-apply more than 10 Tier 3 archives in a single cycle (cap per-run churn; if there are more candidates, the rest stage to Streamlit for batch review).
- If `auto_revert_candidates.regressions` is non-empty, SKIP step 4 entirely for this cycle — first prove quality holds at current bytes before cutting further.

---

## 6. Summary

For `--auto` mode (the default), print this brief Auto Summary instead of the legacy block:

```text
AUTO_SUMMARY  ·  session=<short_id>  ·  <wall_seconds>s
Reverted: N change_ids (reasons: <comma list of regression types>) — <revert details>
Applied:  N changes  ·  bytes reclaimed: <K>KB  ·  files touched: <count>
  Tier 1 (inline transforms):   <count>  →  <K>KB
  Tier 2 (example externalize): <count>  →  <K>KB
  Tier 3 (cold-section archive):<count>  →  <K>KB  (archive dir: .claude/_archive/)
  Tier 4 (cross-file dedup):    <count>  →  <K>KB
Staged (human review):  N  ·  open in Streamlit → Analytics → Pending Improvements
Trend: total_skill_bytes <prev> → <now>  (Δ <signed K>KB across last <n> runs)
Watching next run: change_ids <list>  (regression check fires automatically)
```

For interactive mode, the legacy block:

```text
IMPROVE SUMMARY
Proposals: N  |  Applied: N  |  Rejected: N
Files changed: <list>
Expected improvement: <metric> <old> → <target>
Run /fetchjobs to validate.
```

**Self-improvement:** This file and `.cursor/skills/godmode-improve/SKILL.md` are subject to the same process. Propose edits to them if a pain point applies. The skill improves itself.
