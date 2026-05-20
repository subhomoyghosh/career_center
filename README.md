# Career Command Center

Standalone, self-sufficient autonomous job-search package. Follow the steps below. Use **terminal** for the orchestrator and app; use **AI Chat** (Claude Code) for setup and job search.

🎙️ **[Listen to the 90-second intro](assets/command_center_voice_over.mp3)** — what this is and how it works.

---

## Table of Contents

- [Workflow: How To Run?](#workflow)
- [Streamlit UI](#streamlit-ui)
- [/improve — self-tuning loop](#improve-loop)
- [Project Layout: Key Components](#project-layout)
  - [Key components](#project-layout)
  - [Candidate profile JSON](#candidate-profile-json)
  - [Sequence](#flow-sequence)
- [Maintenance/Troubleshooting: after-setup + snapshot history + resetting](#maintenance)
  - [Maintenance/Troubleshooting: After setup](#after-setup)
  - [Maintenance/Troubleshooting: Snapshot history](#snapshot-history)
  - [Maintenance/Troubleshooting: Resetting](#resetting)
- [How to distribute](#how-to-distribute-this-repo)

## <a id="workflow"></a>Workflow: How To Run?

Quick clickable guide: [How to Run](./HOW_TO_RUN.md)

### 1. Initialize the environment (terminal)

In the **system terminal** (not the AI chat window), from the project root run:

```bash
python3 orchestrator.py
```

Or with **uv**:

```bash
uv run python orchestrator.py
```

This creates `data/`, an empty **`data/candidate_info.json`** template (only if no profile file exists yet), and the jobs database. **Safe to run multiple times:** existing profile JSON and DB data are not overwritten.

### 2. Drop resume

Place the resume PDF in the `data/` folder. Use any filename that **contains "resume"** (e.g. `data/resume.pdf`, `data/My_Resume.pdf`). Only PDFs whose name includes "resume" are used for setup.

### 3. Run one-time Setup (infer profile) — AI Chat

**In Cursor:** Open Cursor Chat (Cmd+L / Ctrl+L) and type **`/setup`**. The agent will read the resume, infer the profile, and show proposed JSON for approval. Save to `data/candidate_info.json` (preferred).

**In Claude Code:** Type **`/setup`** in the chat. The agent will read the resume, infer the profile, and show proposed JSON for approval. Save to `data/candidate_info.json`. Do not run the job search until the user has confirmed the config.

**Alternative:** Skip LLM setup and **copy the JSON** into `data/candidate_info.json` (see schema in the setup rule).

### 4. Run the search — AI Chat

**In Cursor:** Type **`/fetchjobs`**.

**In Claude Code:** Type **`/fetchjobs`** in the chat.

#### Variant dispatch (Lean vs Full) — asked at the top of every run

`/fetchjobs` opens with a tier-aware dispatcher that lets you pick a variant for THIS run:

| Variant | Model | Per-run cost | Best for |
|---------|-------|--------------|----------|
| **Lean** | Sonnet 4.6 | ~10 turns, ~500K tokens | Pro plan (fits a 5-hour rate-limit window). Also good for Max users who want a cheap quick run. |
| **Full** | Opus 4.7 | ~150 turns, ~20M tokens | Max 5x / Max 20x plan. Verbose chat, full description corpus loaded for scoring + wisdom. |

The dispatcher asks once for `plan_tier` (`Pro` / `Max 5x` / `Max 20x`) and saves it to `candidate_info.json`. On every subsequent `/fetchjobs`, it asks which variant to run, with the *recommended* option (Lean for Pro, Full for Max) listed first. Both variants share the same DB schema, Persistence Agent, diagnostics, and Step 9.f token backfill — only the discovery and scoring path differs.

**Skip the prompt:** set `"runtime_mode_override": "lean"` (or `"full"`) in `candidate_info.json` and the dispatcher honors it silently every run.

**Direct invocation:** anyone can type **`/fetchjobs-pro`** to invoke the Lean variant directly, bypassing the dispatcher. The Lean variant lives in `.claude/commands/fetchjobs-pro.md` and externalizes every fetched job description to `data/_descriptions/{idx}.txt`, then hands a Scoring Subagent (background, Python-only) the candidate list — the main agent never re-reads the description corpus, which is what makes Lean so much cheaper.

#### Under the hood — the unchanged "orchestrator + agent teams" model

`/fetchjobs` (Full) runs as an orchestrator + agent teams for speed:

- **Context Team** — profile, nudge context, and resume PDF loaded as 3 parallel tool calls.
- **Discovery Team** — all WebSearch and WebFetch calls fire in parallel waves (Search Wave → Fetch Wave → Backfill Wave). WebSearch/WebFetch cannot be delegated to subagents and always run in the main turn.
- **Persistence Agent** (background subagent) — after scoring, validates links, persists jobs, and emits diagnostics while the main orchestrator writes wisdom in parallel.

`/fetchjobs-pro` (Lean) adds two extra agents in the same model: description externalization in the main turn (descriptions written to `data/_descriptions/`), then a **Scoring Subagent** (background, Python only) that reads those files in isolation and writes `data/_fetchjobs_scored.json`. The main agent receives only a top-3 summary — keeping its context lean across the scoring/wisdom turns where the Full variant burns ~17M tokens re-reading descriptions.

Both variants log:
- `linkedin_discovered`
- `linkedin_with_ats`
- `linkedin_fallback_only`
- `linkedin_dropped_reason_counts`

### 4b. Listing URL validation (trust model)

- **Automatic (happens during `/fetchjobs`, no extra user action):** `filter_valid_job_links()` checks each candidate URL immediately before `persist_jobs()`. Checks: HTTP status (`2xx` ok; `404`/`410` terminal-drop; `403`/`408`/`425`/`429`/`5xx` treated as **transient** and the row is kept alive with a `link_validation_transient` marker so a momentary bot-block or rate-limit doesn't prune a live listing), minimum body size, two-tier dead-page phrase match (one **strong** phrase like *"this position is no longer"* is sufficient; otherwise ≥2 distinct **generic** phrases are required), and title echo on non-LinkedIn boards. See **`src/job_finder/link_validation.py`**.
- **Automatic (agent audit when needed):** **`.cursor/skills/validate-job-links/SKILL.md`** — MCP web fetch + redirect/title checks for ambiguous cases. The user does not run this manually; the agent uses it internally when it decides it needs stricter verification.
- **Automatic LinkedIn retrieval path:** **`.cursor/skills/discover-linkedin-jobs/SKILL.md`** — dedicated high-precision query templates, strict LinkedIn URL filtering, login-wall/interstitial handling, confidence tiers, and ATS backfill (company+title → direct Lever/Greenhouse/Ashby/Workday link).

### 5. View and edit (terminal)

```bash
uv run streamlit run app.py
```

See **[Streamlit UI](#streamlit-ui)** below for what the app shows and what to click.

### 6. Self-tune the system

For pro users, enable `auto_improve_enabled` in the profile JSON (default for new
profiles) — every `/fetchjobs` ends with an automatic `/improve --auto` that runs the
audit, auto-applies Tier 1–4 compaction (hedge cleanup, example externalization,
cold-section archive, cross-file dedup), and **auto-reverts any prior change whose
next-run metrics regressed**. Pain-point thresholds activate at `n_priors_used ≥ 1`
with graduated severity (LOW → MEDIUM → HIGH as priors accumulate). PATTERN_*/SCORING_DRIFT
proposals still stage to Streamlit for human approval — they change behavior, not just
cost, so the cost-metric watchlist doesn't safely cover them. See
**[/improve — self-tuning loop](#improve-loop)**.

---

## <a id="streamlit-ui"></a>Streamlit UI

The app is a three-page Streamlit dashboard wired to `data/sovereign_agent.db` and
`data/run_diagnostics.jsonl`.

**Performance:** the jobs DB and config JSON are cached via `@st.cache_data`
(`src/job_finder/ui_data.py`). The cache key is an `(mtime, size)` fingerprint
of `data/sovereign_agent.db` plus the mtime of the active config — using the size
in addition to mtime defends against sub-second writes that share an mtime tick.
Typing into the sidebar no longer re-reads the DB; the profile is in an `st.form`
so a full rerun fires only when you click **Update Profile**. The
`run_diagnostics.jsonl` reads are also mtime-cached. Saving feedback writes only
**changed** rows (diff against the cached frame), so a 500-row board still saves
in one round-trip.

**Concurrency:** `persist_jobs` wraps each per-row read-modify-write in an
explicit `BEGIN IMMEDIATE` SQLite transaction (`isolation_level=None`) so a
`/fetchjobs` run that lands at the same instant as a UI edit blocks at the
SELECT step instead of clobbering `user_feedback` / `user_weight` / `status`
on its REPLACE. The connection also opens with `timeout=30.0` so brief lock
contention waits rather than erroring out.

### Pages

| Page | What's there |
|------|--------------|
| **Home** | Filter (domain, source) → job board (`st.data_editor`) → **Save feedback & status** button. |
| **Analytics** | Totals, high-moat count, average score, Match Quality histogram, per-run **Token Usage** table (Input/Output/Cache/Productive/Lost), latest `/improve` actions. |
| **Historical Runs** | Last 5 `/fetchjobs` runs (`valid_jobs`, `webfetch_calls`, `calls_per_job`, token columns), Domain Distribution bar, column glossary. |

### Sidebar

- Strategic Pitch, Scientific Moat (comma-sep), Engineering Stack, Target Seniority, Target Country.
- Search Keywords (`golden_keywords`), Noise Keywords, Priority Domains, Search Targets / ATS sites.
- **Exclusions** expander: `excluded_companies` (exact), `excluded_areas` (substring on theme), `excluded_pairs` (`company:area` AND-match). Enforced both at `/fetchjobs` Step 3 and as a backstop in `persist_jobs`.
- **Update Profile** (writes to active `candidate_info.json` via `save_config`, snapshots, invalidates the data cache).
- **Market Intelligence** toggle and **Weight column** toggle.

### Job board columns

| Column | Edit? | Purpose |
|--------|-------|---------|
| `company`, `title`, `score`, `theme`, `rationale` | no | discovery output |
| `source` | no | derived from `link` |
| `link` | no | clickable listing URL |
| **`status`** | yes | application lifecycle — `New` / `Applied` / `InProgress` / `Closed` / `Won` / `NotForMe`. Applied/InProgress/Closed/Won stay POSITIVE-GENRE (next `/fetchjobs` keeps nudging toward similar roles). `NotForMe` is NEGATIVE-GENRE. |
| **`user_feedback`** | yes | `Good` / `Bad` / `—` — genre preference, independent of lifecycle. |
| **`user_weight`** | yes (toggleable) | 0–100 per-row weight used by the next run. |

Click **Save feedback & status** — the diff is persisted via
`update_jobs_feedback_batch` + `update_jobs_status`, a jobs snapshot is appended, and
the data cache is invalidated so the page re-renders fresh state.

### Streamlit UI sequence

```mermaid
sequenceDiagram
    participant U as User (browser)
    participant App as app.py / ui_app.render_app
    participant Data as ui_data.load_data
    participant Sidebar as ui_profile_editor.render_profile_sidebar
    participant Home as ui_job_board.render_home_board
    participant Ana as ui_job_board.render_analytics
    participant Hist as ui_job_board.render_historical_runs
    participant IC as job_finder.improve_changes
    participant P as job_finder.persistence
    participant D as data/

    U->>App: streamlit run app.py
    App->>Data: load_data() — cache key (config_path, config_mtime, (db_mtime, db_size), diag_mtime)
    Data->>D: read candidate_info.json + sovereign_agent.db (REPLACE-safe via BEGIN IMMEDIATE)
    Data-->>App: (config, jobs DataFrame)
    App->>App: st.navigation([Home (default), Analytics, Historical Runs])
    App->>Sidebar: render_profile_sidebar(profile, config)
    Sidebar->>U: identity / moat / stack / exclusions / toggles inside st.form

    opt user submits "Update Profile"
        Sidebar->>D: save_config(payload) → candidate_info.json + history snapshot
        Sidebar->>Data: invalidate_data_cache + st.rerun
    end

    Sidebar-->>App: (show_wisdom, show_weight_column)

    alt Home page (default)
        App->>Home: render_home_board(profile, jobs, toggles)
        Home->>U: domain + source filters, job-board st.data_editor
        U->>Home: edit status / user_feedback / user_weight, click "Save feedback & status"
        Home->>Home: diff edited vs original by row id
        opt feedback / weight changes
            Home->>P: update_jobs_feedback_batch(fb_updates)
        end
        opt status changes
            Home->>P: update_jobs_status(status_updates)
        end
        Home->>P: record_jobs_snapshot_from_db() → data/history/jobs_history.db
        Home->>Data: invalidate_data_cache + st.rerun
    else Analytics page
        App->>Ana: render_analytics(jobs)
        Ana->>U: metric row + Match Quality histogram + Token Usage table
        Ana->>D: merge run_diagnostics.jsonl rows + /tmp/improve_audit.json "last audit" col

        note over Ana,IC: Pending Improvements section
        Ana->>IC: list_pending_proposals()
        IC->>D: read improve_proposals.jsonl
        IC-->>Ana: pending proposals
        Ana->>U: data_editor w/ Approve? + Dismiss? checkboxes
        alt Apply approved
            Ana->>IC: apply_proposal(change_id) per ticked row
            IC->>D: write file edit + improve_changes.jsonl + improve_log.jsonl ([improve] commit for tracked)
        else Dismiss selected
            Ana->>IC: dismiss_proposal(change_id) per ticked row
            IC->>D: mark proposal dismissed in improve_proposals.jsonl
        end

        note over Ana,IC: Improvement History section
        Ana->>IC: list_applied_changes(limit, include_reverted=True)
        IC-->>Ana: rows w/ status (applied/reverted), commit sha, mode
        Ana->>U: data_editor w/ Revert? checkbox + "Apply reverts" button
        opt Apply reverts
            Ana->>IC: revert_change(change_id) per ticked row
            alt tracked file
                IC->>D: git revert [commit_sha]
            else gitignored (candidate_info.json)
                IC->>D: structured inverse-op (no commit)
            end
        end

        opt improvement history empty
            Ana->>U: expander — legacy improve_log.jsonl (last 5)
        end
        Ana->>Data: invalidate_data_cache + st.rerun (after any apply/dismiss/revert)
    else Historical Runs page
        App->>Hist: render_historical_runs(jobs)
        Hist->>D: read last 200 rows of run_diagnostics.jsonl (mtime-cached)
        Hist->>U: last-5 runs table (valid_jobs, webfetch_calls, calls_per_job, tokens), Domain Distribution bar, column glossary
    end
```

---

## <a id="improve-loop"></a>/improve — self-tuning loop

`/improve` is the meta-skill that closes the loop on the rest of the system. **Pro-user contract:** run `/fetchjobs` heavily and still have tokens left — the system aggressively compacts cost without ever losing information, and self-heals on the next run if quality regressed.

### Four modes

| Mode | Trigger | Behavior |
|------|---------|----------|
| `--auto` (default for pro users) | auto-dispatched at end of `/fetchjobs` when `auto_improve_enabled` is true | (1) Audit. (2) Self-heal: auto-revert any prior applied change whose next-run metrics regressed (`valid_jobs < 0.85×`, `pct_high_score < 0.85×`, or `tokens_per_valid_job > 1.5×` vs. the change's `pre_metrics`). (3) Walk Tier 1–4 compaction (§0.7 of `.claude/commands/improve.md`); auto-apply each via `apply_proposal(..., pre_metrics=…)` so the *next* run can audit it. (4) Stage PATTERN_*/SCORING_DRIFT_ proposals to Streamlit (those still need human review). (5) Print brief `AUTO_SUMMARY`. |
| `--audit-only` | `/improve --audit-only` (or auto from `/fetchjobs` when `auto_improve_audit_enabled` is true) | Stages every proposal to `data/improve_proposals.jsonl`; nothing is applied. Review in Streamlit → Analytics → Pending Improvements. |
| `--apply <change_id>` | Streamlit "Apply approved" button | `apply_proposal(change_id)` — git-tracked files get an `[improve]` commit; gitignored uses structured inverse-op. |
| `--restore <change_id>` | Streamlit on any applied row, or auto when next-run regression detected | `revert_change(change_id)` — for `archive_section` patches, reconstitutes the section from the change row's `semantic_diff.captured_content` and unlinks the archive. |

### Compaction tiers (§0.7 of `.claude/commands/improve.md`)

| Tier | What it does | Lossless? |
|------|--------------|-----------|
| **1** Inline equivalence transforms | Hedge-word removal, prose→bullets, paragraph dedup, "Why this is needed" collapse | Yes by construction (semantic equivalence) |
| **2** Example externalization | Worked examples (multi-line code blocks > 15 lines) move to `.claude/_archive/`; rule stays inline | Yes — example preserved + stub references it |
| **3** Cold-section archive | Sections whose backtick fingerprints didn't appear in recent session activity move to `.claude/_archive/<file-stem>__<heading-slug>.md`; 1-line stub replaces them in source | Yes — full content in archive; restorable via `--restore` |
| **4** Cross-file dedup → thin wrapper | Cursor `.mdc` / `SKILL.md` mirroring `.claude/commands/*.md` collapses to a delegation pointer | Yes — canonical content reachable |

The audit (`scripts/audit_run_efficiency.py`) emits the data backing Tier 3 (`cold_sections.candidates` from a 1-run rollup of `data/skill_section_usage.jsonl` with adaptive top-quartile threshold) and the regression watchlist (`auto_revert_candidates`).

### Phases (interactive mode walks them; `--auto` orchestrates them)

1. **Ingest evidence** — `data/run_diagnostics.jsonl` (per-run telemetry), `data/improve_log.jsonl` (history), `data/improve_changes.jsonl` (applied changes + `pre_metrics`), `data/last_session.json` (Claude Code transcript pointer).
2. **Run efficiency audit** — `uv run python scripts/audit_run_efficiency.py` emits: `efficiency` (current run's `main_tokens`, `valid_jobs`, `tokens_per_valid_job`, `cache_hit_rate`, `subagent_token_share`, `pct_high_score`); `priors` (P50/P90 of those metrics across last 5 runs); `subagent_run_stats` (within-run baseline + `internal_outlier` flagging); `cold_sections` (Tier 3 candidates); `compaction_trend` (3-run `total_skill_bytes` series with `stagnant` flag); `auto_revert_candidates` (recently-applied changes whose post-run metrics regressed); waste buckets and parallelism candidates.
3. **Feedback synthesis** — `scripts/synthesize_feedback_patterns.py prepare` builds a status-disentangled good/bad set (Applied-then-Closed stays POSITIVE-GENRE), the LLM proposes hypotheses across company/skill/domain/problem_type axes, then `validate` runs five adversarial gates.
4. **Idempotency** — pain points approved in the last 14 days are suppressed unless evidence has worsened ≥10% (`data/improve_log.jsonl`).
5. **Proposals + apply** — pain-point IDs include the classic structural ones (`SEARCH_TOO_NARROW`, `LINK_VALIDATION_AGGRESSIVE`, `BOARD_PAGE_LEAKAGE`, `SCORING_TOO_STRICT`, `DOMAIN_BLIND_SPOT`, `WASTED_FETCH_RATE`, `REDIRECT_LATENCY_TAX`, `LATENCY_CRITICAL_PATH`, `PRUNER_FPR_ALERT`) plus token/compaction ones added in 2026-05 (`TOKENS_PER_VALID_JOB_HIGH`, `MAIN_AGENT_CONTEXT_BLOAT`, `MAIN_AGENT_CACHE_MISS_HIGH`, `SUBAGENT_TOKEN_BLOAT` recalibrated to `internal_outlier`, `SKILL_COLD_SECTION_FOUND`, `SKILL_COMPACTION_PLAN` always-on, `COMPACTION_STAGNATION`, `REGRESSION_DETECTED`) plus feedback ones (`PATTERN_INCLINATION_FOUND`, `PATTERN_DISINCLINATION_FOUND`, `PATTERN_LEARN_SKILL_FOUND`, `SCORING_DRIFT_DETECTED`, `LOW_APPLY_CONVERSION`, `NEGATIVE_FOLLOWUP_AVOIDANCE_LOW`, `POSITIVE_FOLLOWUP_RATE_LOW`). Cost-metric pain-points activate at `n_priors_used ≥ 1` with graduated severity.
6. **Log** — applied changes go to `data/improve_changes.jsonl` (with `pre_metrics` for regression watch) + `data/improve_log.jsonl` (legacy mirror).

### Constitutional rule (revised)

Tier 1 transforms are lossless by construction. Tier 2/3/4 auto-apply BUT every applied change records `pre_metrics` (current run's `valid_jobs`, `pct_high_score`, `tokens_per_valid_job`, `main_tokens`); the NEXT `/improve --auto` cycle's `auto_revert_candidates` block flags regressions and calls `revert_change(change_id)` silently. **The user does not gate every change — the regression watchlist does, computationally, by reading the next run's metrics.** PATTERN_*/SCORING_DRIFT_ proposals still require explicit human approval — they change behavior, not just cost, so the cost-metric watchlist doesn't safely cover them.

The full spec lives in `.claude/commands/improve.md`. The Analytics page surfaces the most recent entries from `data/improve_log.jsonl` plus the live `data/improve_changes.jsonl` rows (status applied / reverted / validated).

### /improve sequence

```mermaid
sequenceDiagram
    participant A as User (AI Chat)
    participant I as /improve
    participant Aud as audit_run_efficiency.py
    participant Syn as synthesize_feedback_patterns.py
    participant Eff as audit_feedback_efficacy.py
    participant D as data/
    participant U as User (Streamlit)
    participant App as streamlit app.py
    participant IC as improve_changes.py

    note over A,I: Mode selection (one of four)
    alt Auto (from /fetchjobs when auto_improve_enabled — default for pro users)
        A->>I: /improve --auto
    else Interactive (typed /improve)
        A->>I: /improve
    else Audit-only (from /fetchjobs when auto_improve_audit_enabled)
        A->>I: /improve --audit-only
    else UI apply / restore (from Streamlit)
        U->>App: tick Approve, click Apply approved (or Revert)
        App->>I: /improve --apply [change_id]  / --restore [change_id]
    end

    note over I,D: Phase 1 — Ingest evidence (all modes)
    I->>D: read run_diagnostics.jsonl, improve_log.jsonl, last_session.json
    I->>D: read improve_changes.jsonl (applied changes with pre_metrics)
    I->>D: read skill/command files + candidate_info.json + skill_section_usage.jsonl
    I->>D: read recent_closed_pain_points (14-day dedup)

    note over I,Aud: Phase 1.5 — Run efficiency audit
    I->>Aud: uv run python scripts/audit_run_efficiency.py
    Aud->>D: read last_session.json + session JSONL + subagent task outputs
    Aud->>D: read skill files (parse sections, extract fingerprints, scan session refs)
    Aud->>D: append skill_section_usage.jsonl row (current run's section refs + total_skill_bytes)
    Aud->>D: read improve_changes.jsonl (cross-ref pre_metrics for regression watch)
    Aud-->>I: /tmp/improve_audit.json — efficiency, priors, subagent_run_stats,<br/>cold_sections, compaction_trend, auto_revert_candidates,<br/>waste buckets, parallelism candidates

    note over I,Syn: Phase 1.7 — Feedback synthesis + efficacy
    I->>Syn: synthesize_feedback_patterns.py prepare
    Syn->>D: read DB good/bad sets (status-disentangled) + full descriptions
    Syn-->>I: /tmp/synth_prep.json (with llm_prompt_template if sample sufficient)
    I->>I: execute llm_prompt_template → /tmp/proposals.json (4 axes)
    I->>Syn: synthesize_feedback_patterns.py validate
    Syn-->>I: /tmp/validated.json (5 adversarial gates, only PASS verdicts forward)
    I->>Eff: audit_feedback_efficacy.py
    Eff->>D: read jobs_history.db snapshots
    Eff-->>I: /tmp/efficacy.json (user_reaction_matrix, lifecycle buckets)

    note over I: Phase 2–3 — Pain-point detection (dedup via improve_log)

    alt mode = --auto (default for pro users)
        note over I,IC: Step 1 — Self-heal (auto-revert prior regressions)
        loop each change_id in auto_revert_candidates.regressions
            I->>IC: revert_change(change_id, reverted_by="auto_regression_guard")
            alt archive_section patch
                IC->>D: reconstitute section from semantic_diff.captured_content
                IC->>D: unlink archive_path
            else text_replace / json_*
                IC->>D: structured inverse-op or git revert commit_sha
            end
            IC-->>I: ok / failed (continue on failure; don't halt cycle)
        end
        loop each change_id in auto_revert_candidates.validated
            I->>D: append {change_id, validated_at} to improve_changes.jsonl
        end

        note over I,IC: Step 2 — Walk Tier 1–4 compaction; auto-apply with pre_metrics
        loop each Tier 1/2/3/4 candidate (cap 10 Tier-3 per cycle)
            I->>IC: write_proposal(...) → change_id
            I->>IC: apply_proposal(change_id, approved_by="auto",<br/>pre_metrics=&lt;current efficiency&gt;)
            alt Tier 1 (text_replace) or Tier 4 (text_replace)
                IC->>D: edit source; [improve] commit if tracked
            else Tier 2 / Tier 3 (archive_section)
                IC->>D: mkdir .claude/_archive; write archive file
                IC->>D: replace section in source with stub_text
                IC->>D: append change row w/ semantic_diff.captured_content + pre_metrics
            end
            IC-->>I: ok / blocked_by_dirty_tree / stale (treat non-ok as soft skip)
        end

        note over I,A: Step 3 — Stage human-gated proposals to Streamlit
        loop PATTERN_*, SCORING_DRIFT_, REGRESSION_DETECTED, COMPACTION_STAGNATION
            I->>IC: write_proposal(...)
            IC->>D: append improve_proposals.jsonl
        end

        I->>A: AUTO_SUMMARY — reverted N, applied N (Tier 1/2/3/4 breakdown),<br/>bytes reclaimed, trend Δ, watching next-run change_ids
    else mode = interactive
        loop one proposal at a time
            I->>A: CURRENT / PROPOSED quoted exactly, evidence cited
            A->>I: approve / reject
            opt approved
                I->>IC: apply_proposal(..., pre_metrics=&lt;current efficiency&gt;)
                IC->>D: write change to target file
                IC->>D: append improve_changes.jsonl + improve_log.jsonl
            end
        end
    else mode = audit-only
        loop each surviving proposal
            I->>IC: write_proposal(...)
            IC->>D: append improve_proposals.jsonl (staged, NOT applied)
        end
        I->>A: "N proposals staged — review in Streamlit → Analytics."

        note over U,App: User reviews proposals in Streamlit (Analytics page)
        U->>App: Analytics → Pending Improvements table
        App->>D: list_pending_proposals() → improve_proposals.jsonl
        App->>U: show severity, pain_point, file, patch preview, evidence
        alt Approve + Apply approved
            App->>IC: apply_proposal change_id — direct Python call, no CLI hop
            IC->>D: write change to target file — .md/.py git-committed, JSON via inverse-op
            IC->>D: append improve_changes.jsonl + improve_log.jsonl
            IC-->>App: result JSON — ok / reason / detail
            App->>App: invalidate_data_cache + st.rerun
        else Dismiss + Dismiss selected
            App->>IC: dismiss_proposal change_id
            IC->>D: mark proposal dismissed in improve_proposals.jsonl
        end
    else mode = --apply — CLI from chat, not from UI
        I->>IC: apply_proposal change_id
        IC->>D: write change + append improve_changes.jsonl + improve_log.jsonl
        IC-->>I: result JSON
        I->>A: print result
    end

    opt user clicks Revert in Improvement History
        U->>App: tick Revert, click Apply reverts
        App->>IC: revert_change change_id
        alt tracked file — .md or .py
            IC->>D: git revert commit_sha — preserves other applied changes
        else gitignored — candidate_info.json
            IC->>D: apply structured inverse-op — no commit
        end
        IC->>D: mark reverted=true in improve_changes.jsonl
        App->>App: invalidate_data_cache + st.rerun
    end
```

---

## <a id="project-layout"></a>Project Layout: Key Components

| Path | What it’s for | Who is responsible |
|------|------------------|---------------------|
| `orchestrator.py` | Initializes `data/` and creates the jobs DB (if missing). | **User runs once** at the start (or after a reset). |
| `reset.py` | Clears config/jobs/snapshot history; writes a fresh empty `data/candidate_info.json`. | **User runs** only when starting over. |
| `data/candidate_info.json` | Your active candidate profile. | **User creates/edits** via `/setup` + the Streamlit “Update Profile” button. |
| `data/sovereign_agent.db` | Persisted jobs table. | **Auto**: written/updated during `/fetchjobs`. |
| `data/history/` | Append-only snapshots for profile/jobs/wisdom. | **Auto**: created during saves + `/fetchjobs` + wisdom updates. |
| `src/job_finder/config.py` | Load/save profile config. | **Auto** (internal). |
| `src/job_finder/candidate_disk_sync.py` | Detects external profile edits via sha256 fingerprint and snapshots on next `load_config`. | **Auto** (internal). |
| `src/job_finder/history.py` | Snapshot list/get helpers. | **Auto** (internal), or **optional** if the user inspects snapshots manually. |
| `src/job_finder/persistence.py` | Persists fetched jobs + keeps existing feedback/weights. | **Auto** (internal) during `/fetchjobs`. |
| `src/job_finder/link_validation.py` | `filter_valid_job_links` (HTTP + content + title checks). | **Auto**: run before persisting jobs in `/fetchjobs`. |
| `scripts/snapshot_history.py` | CLI to list snapshot metadata (optional). | **User runs optionally** for debugging/restore. |
| `scripts/dump_judge_context.py` | Creates evidence payload for the in-chat judge (optional). | **User runs** only when doing the optional judge step. |
| `scripts/evaluate_nudge_system.py` | Prints external-LMM judge prompts (optional). | **User runs optionally**. |
| `.cursor/skills/evaluate-nudge-and-wisdom/` | Agent judge for nudge + wisdom (optional QA). | **Auto inside the agent** when the user requests it (not manual). |
| `.cursor/skills/validate-job-links/` | Extra MCP web validation (used when the agent audits listings). | **Auto inside the agent** when needed (not manual). |
| `.claude/commands/fetchjobs.md` / `.cursor/rules/jobsearch.mdc` | `/fetchjobs` agent rule — variant dispatcher + Full (Opus) Max-tier orchestrator + Context Team + Discovery Team + Persistence Agent. | **User triggers** by typing `/fetchjobs` (agent runs internals automatically). |
| `.claude/commands/fetchjobs-pro.md` | `/fetchjobs-pro` agent rule — Lean (Sonnet 4.6) Pro-tier variant: description externalization + Scoring Subagent. Same DB contract as `/fetchjobs`. | **User triggers** by typing `/fetchjobs-pro` directly, OR auto-invoked when picking "Lean" in the `/fetchjobs` dispatcher. |
| `.cursor/rules/setup_from_resume.mdc` | `/setup` agent rule. | **User triggers** by typing `/setup` (agent proposes JSON). |
| `app.py` | Streamlit UI: edit profile + set job feedback/weights. | **User runs** `streamlit run app.py`; updates happen when the user clicks buttons. |

### <a id="candidate-profile-json"></a>Candidate profile JSON (`candidate_info.json`)

- **Preferred file:** `data/candidate_info.json` — used automatically if it exists.
- The app and `job_finder.config.load_config()` resolve the active path via `job_finder.paths.resolve_active_config_path()`.
- **User can paste or drop a full JSON** with the canonical keys. Identity / search keys: `core_identity`, `scientific_moat`, `engineering_stack`, `target_seniority`, `target_country`, `priority_domains`, `golden_keywords`, `search_targets`, `noise_keywords`, `peer_companies`, `wisdom`. Self-tuning keys (written by `/improve`'s feedback synthesizer): `inclinations`, `disinclinations`, `learn_skills`. Hard-filter keys (UI-editable exclusions): `excluded_companies`, `excluded_areas`, `excluded_pairs`. Behavior toggles: `auto_improve_enabled` (boolean — when true, every `/fetchjobs` ends with an auto `/improve --auto` that auto-reverts regressions and auto-applies Tier 1–4 compaction; PATTERN_*/SCORING_DRIFT_ still stage for review). `auto_improve_audit_enabled` (legacy — when true, dispatches `/improve --audit-only` instead, which stages everything to `data/improve_proposals.jsonl` for human approval; use this if you want to review every cost-only change). Tier keys (used by the `/fetchjobs` variant dispatcher): `plan_tier` (`"pro"` / `"max5x"` / `"max20x"` — asked once and saved; drives which variant is recommended first) and optional `runtime_mode_override` (`"lean"` / `"full"` / unset — when set, dispatcher silently uses it and skips the per-run prompt). As long as the shape matches the schema in `.cursor/rules/setup_from_resume.mdc`, it will load in the app and in `/fetchjobs` without re-running setup; missing keys are normalized in via `_normalize_config_shape`.

---

### <a id="flow-sequence"></a>Sequence

```mermaid
sequenceDiagram
    participant U as User (Terminal)
    participant O as orchestrator.py
    participant D as data/
    participant A as User (AI Chat)
    participant S as /setup
    participant F as /fetchjobs (dispatcher)
    participant FF as Full path (Opus 4.7)
    participant FL as Lean path (Sonnet 4.6)
    participant CT as Context Team
    participant DT as Discovery Team
    participant SC as Scoring Subagent
    participant PA as Persistence Agent
    participant IM as /improve (--auto or --audit-only)
    participant App as streamlit app.py

    U->>O: run orchestrator
    O->>D: create data/, candidate_info.json (if no profile), sovereign_agent.db
    U->>D: drop resume PDF (or paste candidate_info.json)

    A->>S: /setup
    S->>D: read resume PDF
    S->>A: propose profile JSON (incl. plan_tier)
    A->>D: save active profile JSON

    A->>F: /fetchjobs
    F->>D: read plan_tier / runtime_mode_override
    F->>A: ask variant (Lean recommended for Pro, Full for Max) unless override set

    alt variant = Full (Opus 4.7, Max tier)
        A->>FF: continue in same turn
        note over FF,CT: Step 1 — Context Team (3 parallel tool calls)
        par
            FF->>CT: Read profile JSON
            FF->>CT: Bash get_nudge_context.py
            FF->>CT: Read resume PDF
        end
        CT-->>FF: profile + nudge + achievements

        note over FF,DT: Step 2 — Discovery (3 waves, each wave = one parallel message)
        FF->>DT: Wave 1 — WebSearch (8–10 parallel)
        DT-->>FF: search results
        FF->>DT: Wave 2 — WebFetch (6–8 parallel)
        DT-->>FF: job descriptions (kept in main context)
        FF->>DT: Wave 3 — backfill WebSearch + WebFetch
        DT-->>FF: backfill results

        note over FF: Step 3 — Score in main context
        FF->>FF: moat scoring + noise filter + soft-bias patterns

        note over FF,PA: Steps 4–6 — Persistence + Wisdom run in parallel
        par Persistence Agent (background)
            FF->>PA: scored job list
            PA->>PA: filter_valid_job_links
            PA->>D: persist_jobs → sovereign_agent.db, history snapshot
            PA->>D: append run_diagnostics.jsonl
            PA-->>FF: valid_jobs_count, stale_links_pruned
        and Wisdom Loop (main)
            FF->>FF: synthesize wisdom from scored batch
            FF->>D: update wisdom → profile + intelligence snapshot
        end
    else variant = Lean (Sonnet 4.6, Pro tier)
        A->>FL: invoke fetchjobs-pro skill
        FL->>D: clear data/_descriptions/, data/_fetchjobs_candidates.json

        note over FL,CT: Step 1 — Context Team (identical to Full)
        par
            FL->>CT: Read profile JSON
            FL->>CT: Bash get_nudge_context.py
            FL->>CT: Read resume PDF
        end
        CT-->>FL: profile + nudge + achievements

        note over FL,DT: Step 2 — Discovery + externalize descriptions to disk
        FL->>DT: Wave 1 — WebSearch
        DT-->>FL: search results
        FL->>DT: Wave 2 — WebFetch
        DT-->>FL: descriptions
        FL->>D: write data/_descriptions/{idx}.txt (one file per candidate)
        FL->>DT: Wave 3 — backfill WebSearch + WebFetch
        DT-->>FL: more descriptions → externalize
        FL->>D: write data/_fetchjobs_candidates.json (slim rows, snippets only)

        note over FL,SC: Step 3 — Scoring Subagent (background, Python only)
        FL->>SC: spawn with candidates JSON
        SC->>D: read descriptions from data/_descriptions/
        SC->>D: write data/_fetchjobs_scored.json
        SC-->>FL: top_3 summary + score distribution (main context stays lean)

        note over FL,PA: Steps 4–5 — Persistence + Wisdom run in parallel
        par Persistence Agent (background)
            FL->>PA: pointer to data/_fetchjobs_scored.json
            PA->>PA: filter_valid_job_links
            PA->>D: persist_jobs → sovereign_agent.db, history snapshot
            PA->>D: append run_diagnostics.jsonl
            PA-->>FL: valid_jobs_count, stale_links_pruned
        and Wisdom (main, compressed — no description reload)
            FL->>FL: wisdom from top_3 summary + discovery diagnostics
            FL->>D: update wisdom → profile + intelligence snapshot
        end
    end

    note over F,D: Finalization (both variants)
    F->>D: session_marker (uv run python -m job_finder.session_marker)
    F->>D: token diagnostics backfill (patch latest run_diagnostics row)
    F->>A: print recency-grouped jobs table (this run / active apps / earlier)

    alt auto_improve_enabled (default for pro users)
        F->>IM: dispatch /improve --auto
        IM->>D: auto-revert regressed prior changes (via revert_change)
        IM->>D: auto-apply Tier 1-4 compaction (with pre_metrics on each row)
        IM->>D: stage PATTERN_*/SCORING_DRIFT_ to improve_proposals.jsonl
        IM->>A: AUTO_SUMMARY (reverted N, applied N, bytes reclaimed, trend Δ)
    else auto_improve_audit_enabled (legacy — review-everything mode)
        F->>IM: dispatch /improve --audit-only
        IM->>D: stage proposals to data/improve_proposals.jsonl (no auto-apply)
    end

    U->>App: streamlit run app.py
    App->>D: load config + jobs ((mtime, size)-cached)
    App->>U: jobs board + wisdom + profile form + pending improvements
```

---

## <a id="maintenance"></a>Maintenance/Troubleshooting: after-setup + snapshot history + resetting

### <a id="after-setup"></a>After setup (rerun without full reset)

- **Edit profile:** In the app, **Update Profile** saves to the active JSON path.
- **Search again:** **`/fetchjobs`** merges new jobs; snapshots append under `data/history/`.
- **LLM judge / QA (in Cursor):** **`evaluate-nudge-and-wisdom`** — `uv run python scripts/dump_judge_context.py`, then judge **nudge + MCP link checks + wisdom** in chat.  
- **External LLM (optional):** `uv run python scripts/evaluate_nudge_system.py` prints copy-paste prompts for tools outside Cursor.

---

### <a id="snapshot-history"></a>Snapshot history (safe revert / programmatic diff)

Snapshots are append-only rows stored under `data/history/` in three separate SQLite DBs:

- Candidate profile snapshots (`data/history/candidate_history.db`)
  - created when `save_config(..., record_snapshot=True)` runs (e.g. app **Update Profile**)
  - and also when the user edits the active profile JSON directly on disk; on the next `load_config`, the app detects the external change by comparing the file's sha256 to `data/history/.candidate_profile_fingerprint.json`
- Jobs table snapshots (`data/history/jobs_history.db`)
  - created when `persist_jobs()` runs (e.g. after `/fetchjobs`)
- Wisdom / intelligence snapshots (`data/history/intelligence_history.db`)
  - created when `update_wisdom()` runs

| Database | Contents |
|----------|----------|
| `candidate_history.db` | Full profile JSON snapshots |
| `jobs_history.db` | Full jobs table as JSON per snapshot |
| `intelligence_history.db` | `wisdom` string per snapshot |

```bash
# List recent snapshot rows (latest 30)
uv run python scripts/snapshot_history.py candidate
uv run python scripts/snapshot_history.py jobs
uv run python scripts/snapshot_history.py intelligence

# Python API (inspect + load snapshots)
python3 - <<'PY'
from job_finder.history import list_snapshots, get_candidate_snapshot, get_jobs_snapshot

candidate_rows = list_snapshots("candidate", limit=5)
print("candidate snapshots:", candidate_rows)

if candidate_rows:
    latest_candidate_id = candidate_rows[0]["id"]
    candidate_payload = get_candidate_snapshot(latest_candidate_id)
    print("latest candidate snapshot keys:", sorted(candidate_payload.keys()) if candidate_payload else None)

jobs_rows = list_snapshots("jobs", limit=1)
print("jobs snapshots:", jobs_rows)

if jobs_rows:
    latest_jobs_id = jobs_rows[0]["id"]
    jobs_payload = get_jobs_snapshot(latest_jobs_id)
    print("latest jobs snapshot rows:", len(jobs_payload) if jobs_payload else None)
PY
```

---

### <a id="resetting"></a>Resetting (start over)

1. **Terminal:** `python3 reset.py` or `uv run python reset.py`  
   Clears **`data/history/*.db`**, removes **`data/candidate_info.json`** if present, writes empty **`data/candidate_info.json`**, deletes **`data/sovereign_agent.db`**. Resume PDF(s) are **not** deleted.
2. **Terminal:** Run `orchestrator.py` to recreate the DB.
3. **AI Chat:** **/setup** (or paste a new `candidate_info.json`), then **/fetchjobs**.

**In Cursor:** **`/reset`** runs the reset script (see `.cursor/rules/reset.mdc`).

---

## <a id="how-to-distribute-this-repo"></a>How to distribute

Before sharing: run `reset.py`, then ensure `data/*.pdf`, `data/sovereign_agent.db`, `data/candidate_info.json`, and `data/history/` are not committed (see `.gitignore`).
