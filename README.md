# Career Command Center

Self-sufficient job-search orchestrator. Autonomous agent discovers, scores, and persists high-relevance opportunities; self-tuning loop compresses cost while preserving signal quality. Built for users on pro-tier Claude plans who want to run heavy search without token waste.

---

## Quick start

**First time?** See [How to Run](./HOW_TO_RUN.md) for the 5-minute walkthrough.  
**Already set up?** `uv run streamlit run app.py` then `/fetchjobs` in chat.

---

## Table of Contents

- [Dashboard](#dashboard)
- [Fetch variants: Lean vs Full](#fetch-variants-lean-vs-full)
- [Self-tuning: the /improve loop](#self-tuning-the-improve-loop)
- [Under the hood](#under-the-hood)
- [Troubleshooting](#troubleshooting)

---

## Dashboard

Run `uv run streamlit run app.py` to see your jobs board, feedback history, and pending improvements.

Three pages + sidebar on every page:

| Page | What you get |
| --- | --- |
| **Home** | Left column: Your candidate profile + market intelligence · Right column: Jobs table (filterable by domain, source) · Edit per row: Status, feedback, weight · Save: Click button to persist changes |
| **Analytics** | Token usage per run (Input/Output/Cache/Productive/Lost) · Pending improvements (staged proposals awaiting approval) · Applied change history (with revert buttons) |
| **Historical Runs** | Last 5 searches side-by-side · Job count, tokens used, domain breakdown per run |

**Sidebar (on all pages):**

- **Profile:** Your pitch, moat, stack, level, location
- **Search:** Keywords to find, keywords to avoid, target domains
- **Exclusions:** Companies to skip, themes to skip, company+theme pairs to skip
- **Toggles:** Show/hide wisdom and weight columns
- **Update Profile:** Saves all changes and snapshots the previous version

---

## Fetch variants: Lean vs Full

`/fetchjobs` dispatches based on your plan tier. Both variants share the same DB schema, persistence agent, and diagnostics.

| Aspect | Lean (Sonnet 4.6) | Full (Opus 4.7) |
| --- | --- | --- |
| **Cost** | ~500K tokens | ~20M tokens |
| **Suited for** | Pro plan (fits 5-hour rate-limit window) | Max 5x / Max 20x plan |
| **What's different** | Descriptions externalized to `data/_descriptions/`; background Scoring Subagent reads them in isolation. Main agent stays lean. | Orchestrator + Context/Discovery/Persistence teams. Descriptions kept in main context for scoring. |
| **Outcome** | Same jobs, same DB, cheaper | Same jobs, same DB, richer scoring context |

### Trade-off: description context vs. token cost

Full keeps descriptions in-context during scoring, enabling richer pattern matching. Lean externalizes them to disk, runs scoring in a background subagent, and gives the main agent only a top-3 summary. **Either way, you get the same final jobs list and the same applied feedback on the next run.** Pro users will want Lean by default; if you're on Max and want to spend the tokens, Full gives more nuance.

#### Skipping the prompt

Set `"runtime_mode_override": "lean"` (or `"full"`) in `candidate_info.json` and the dispatcher honors it silently on every run.

#### Direct invocation

Type **`/fetchjobs-pro`** to invoke Lean directly, bypassing the dispatcher. Lives in `.claude/commands/fetchjobs-pro.md`.

### Link validation

Before persisting, `filter_valid_job_links()` checks each candidate URL:

- HTTP status: `2xx` → keep; `404`/`410` → drop (terminal); `403`/`408`/`429`/`5xx` → mark transient (momentary bot-block or rate-limit shouldn't prune a live listing).
- Minimum body size and dead-page phrase match (one strong phrase like *"this position is no longer"* is sufficient; else ≥2 distinct generic phrases).
- Title echo on non-LinkedIn boards.

See `src/job_finder/link_validation.py` for the full logic. The agent can also invoke an MCP stricter validation on ambiguous cases (`.claude/skills/validate-job-links/`); that happens automatically when the agent decides it needs clarification.

---

## Self-tuning: the /improve loop

`/improve` is the meta-skill that closes the loop. Run it after `/fetchjobs` to audit cost, detect pain points, and apply fixes automatically.

### Why this matters

A pro-user contract: you run heavy, the system compresses cost without losing signal. Auto-reverting regressions means you never pay for a change that made things worse. Auto-applying low-risk compaction (hedge cleanup, example externalization, cold-section archive) means your agent-files stay lean. **Pain-point proposals still require human approval** — those change behavior, not just cost, so they stage to Streamlit for review.

### Four modes

| Mode | Trigger | Summary |
| --- | --- | --- |
| `--auto` | Auto-dispatched at end of `/fetchjobs` when `auto_improve_enabled: true` (default for pro users) | Self-heal regressions, auto-apply Tier 1–4 compaction, stage human-gated proposals |
| `--audit-only` | Set `auto_improve_audit_enabled: true` instead | Stage all proposals to Streamlit for review (no auto-apply) |
| `--apply <change_id>` | Streamlit "Apply approved" button | Apply a single staged proposal; commit if tracked, inverse-op if not |
| Manual `/improve` | Type `/improve` in chat | Interactive walkthrough with full evidence for each proposal |

#### `--auto` workflow (default for pro users)

Runs three steps automatically:

1. **Self-heal** — Revert any prior applied change whose metrics regressed: `valid_jobs < 0.85×`, `pct_high_score < 0.85×`, or `tokens_per_valid_job > 1.5×` (vs. baseline `pre_metrics`)
2. **Compaction** — Walk Tier 1–4; auto-apply each with `pre_metrics` recorded so the NEXT run can audit them
3. **Human gate** — Stage PATTERN_*/SCORING_DRIFT_ proposals to Streamlit (those change behavior, not just cost)
4. **Summary** — Print `AUTO_SUMMARY` (reverted N, applied N, bytes reclaimed, trend)

### Compaction tiers

| Tier | What it does | Lossless? | When it activates |
| --- | --- | --- | --- |
| **1** Inline transforms | Hedge-word removal, prose→bullets, dedup | Yes by construction | Always-on |
| **2** Example externalization | Multi-line code blocks move to `.claude/_archive/`; stub stays inline | Yes (artifact preserved + linked) | When skill files reach threshold |
| **3** Cold-section archive | Sections whose backtick fingerprints haven't appeared recently move to archive; stub replaces them | Yes (restorable via `--restore`) | When skill file activity is uneven |
| **4** Cross-file dedup | Cursor `.mdc` / `SKILL.md` mirroring `.claude/commands/*.md` collapses to a pointer | Yes (canonical content reachable) | When duplication detected |

Pain-point proposals (e.g., `SEARCH_TOO_NARROW`, `SCORING_TOO_STRICT`, `DOMAIN_BLIND_SPOT`) activate when `n_priors_used ≥ 1`, with graduated severity (LOW → MEDIUM → HIGH). These are NOT auto-applied and require explicit review.

### The audit pipeline

```bash
uv run python scripts/audit_run_efficiency.py
```

Reads `run_diagnostics.jsonl`, `improve_log.jsonl`, and skill files; emits `/tmp/improve_audit.json` with:

- **efficiency** — current run's `main_tokens`, `valid_jobs`, `tokens_per_valid_job`, `cache_hit_rate`
- **priors** — P50/P90 of those metrics across last 5 runs
- **cold_sections** — candidates for Tier 3 archive
- **compaction_trend** — 3-run `total_skill_bytes` series (stagnant flag)
- **auto_revert_candidates** — recently-applied changes whose post-run metrics regressed

This data powers the proposal generation and regression watchlist.

---

## Under the hood

### Architecture

The system has three layers:

#### 1. Agent orchestrator (`/fetchjobs`)

- Variant dispatcher → Context Team (profile + nudge + resume)
- Discovery Team (WebSearch + WebFetch waves)
- Scoring (in-context or subagent)
- Persistence Agent (link validation + DB write)
- Wisdom synthesis

#### 2. Persistence layer

- SQLite DB (`sovereign_agent.db`)
- Snapshot history (`data/history/`) for safe rollback

#### 3. Self-tuning layer

- Efficiency audit + pain-point detection + compaction walk
- Regression watchlist
- Triggered at end of `/fetchjobs` or on-demand

### Key files

| Path | Purpose |
| --- | --- |
| `orchestrator.py` | Initializes `data/` and creates the jobs DB (one-time). |
| `reset.py` | Clears config/jobs/history; writes fresh empty `candidate_info.json`. |
| `data/candidate_info.json` | Active candidate profile (identity, moat, stack, search keys, exclusions, toggles, plan tier). |
| `data/sovereign_agent.db` | Jobs table — persisted during `/fetchjobs`. |
| `data/history/` | Append-only snapshots for profile/jobs/wisdom (safe rollback). |
| `src/job_finder/config.py` | Load/save profile config with external-edit detection. |
| `src/job_finder/persistence.py` | Persist fetched jobs; preserve existing feedback/weights. |
| `src/job_finder/link_validation.py` | HTTP + content + title checks before persisting. |
| `.claude/commands/fetchjobs.md` | Full variant (Opus 4.7, Context/Discovery/Persistence teams). |
| `.claude/commands/fetchjobs-pro.md` | Lean variant (Sonnet 4.6, Scoring Subagent). |
| `.claude/commands/improve.md` | `/improve` mode dispatcher + compaction walk + audit pipeline. |
| `app.py` | Streamlit UI entry point. |

### Candidate profile schema

**Minimal required keys** (identity + search):

- `core_identity` — what you do
- `scientific_moat` — your research/domain strengths (comma-sep)
- `engineering_stack` — tech areas you know
- `target_seniority` — role level (e.g., Staff, Principal, Lead)
- `target_country` — geographic preference
- `priority_domains` — industries you target
- `golden_keywords` — search terms that work
- `noise_keywords` — filter out these terms
- `excluded_companies` — exact company names to skip
- `excluded_areas` — substring match on job theme
- `excluded_pairs` — `company:area` AND-filters

**Self-tuning keys** (auto-written by `/improve`):

- `inclinations` — patterns you like (discovered from Good/Applied/Won)
- `disinclinations` — patterns you avoid (from NotForMe/Closed)
- `learn_skills` — areas to upskill in

**Behavior toggles**:

- `auto_improve_enabled` — auto-run `/improve --auto` at end of `/fetchjobs` (default: true for pro users)
- `auto_improve_audit_enabled` — legacy; stages all proposals for manual review instead

**Tier + mode keys**:

- `plan_tier` — `"pro"` / `"max5x"` / `"max20x"` (asked once in `/setup`, saved)
- `runtime_mode_override` — `"lean"` / `"full"` (optional; skips the dispatch prompt)

**Note:** Missing keys auto-normalize via `_normalize_config_shape`.

### Feedback synthesis

The system learns from your feedback loop:

**How it works:**

- Each `/fetchjobs` logs jobs you marked `Good`/`Bad` and status changes
- Next `/improve` cycle analyzes patterns across company, skill, domain, problem-type
- Auto-proposes hypotheses (e.g., "you close civil-eng roles at startups — avoid?" or "applied 3 remote DS but no offers — competing signal?")

**Validation:**

- Proposals stage to `data/improve_proposals.jsonl`
- Five adversarial gates validate before forwarding
- Only PASS verdicts → actionable (stage to Streamlit → Analytics → Pending Improvements)

---

## Troubleshooting

### "VIRTUAL_ENV doesn't match"

Benign. Another project's `.venv` is active. Run with `uv run --active …` or `deactivate` the other env first. Job_finder works either way.

### Snapshot review (optional)

Each profile save / feedback save / `/fetchjobs` / wisdom update appends to `data/history/*.db`. To inspect:

```bash
uv run python scripts/snapshot_history.py candidate
uv run python scripts/snapshot_history.py jobs
uv run python scripts/snapshot_history.py intelligence
```

To load a specific snapshot programmatically:

```python
from job_finder.history import get_candidate_snapshot

snapshot = get_candidate_snapshot(snapshot_id)
print(snapshot.keys())
```

### Resetting (start over)

```bash
python3 reset.py       # Clears data/, writes fresh empty candidate_info.json
python3 orchestrator.py # Recreates DB
# Then: /setup → /fetchjobs in chat
```

Resume PDFs are **not** deleted by `reset.py`.

### Debug counters

The persistence layer exposes counters in the chat summary:

- `dropped_by_exclusion` / `exclusion_backstop_failed` — misconfigured exclusions
- `link_validation_transient` — momentary failures marked live (retry on next run)
- `linkedin_discovered` / `linkedin_with_ats` / `linkedin_fallback_only` — discovery path breakdown
- `linkedin_dropped_reason_counts` — why some LinkedIn listings were pruned

If these look off, either a rule needs tuning (e.g., noise keywords too broad) or link validation is catching false positives. The next `/improve` cycle may auto-propose a fix.

---

## Distribution

Before sharing this repo:

1. Run `python3 reset.py` to clear your profile/jobs/history.
2. Ensure `.gitignore` covers `data/*.pdf`, `data/sovereign_agent.db`, `data/candidate_info.json`, and `data/history/`.
3. The recipient starts fresh via `orchestrator.py` → `/setup` → `/fetchjobs`.

---

## For developers

### Key design constraints (from CLAUDE.md)

- **Seam 1:** Typed task interface. Every agent/subagent defines `input` and `output` shapes before coding.
- **Seam 2:** State lives in one explicit object written to disk after every step (not accumulated in conversation history).
- **Seam 3:** Structured error protocol. Every tool handler returns `{type, context, recoverable}`, never plain strings.
- **Seam 4:** Recovery logging. Even before self-healing is built, recovery attempts are logged in machine-readable form for future meta-improvement.

### Running the tests

```bash
uv run pytest tests/
```

### Architecture philosophy

- Parallelism by default when tasks are independent.
- Token preservation always-on: pass filenames not contents; compact before hitting limits.
- No abstractions unless required. Three similar lines beats a premature helper.
- Comments only for *why*, never for *what* (code should read clearly).
