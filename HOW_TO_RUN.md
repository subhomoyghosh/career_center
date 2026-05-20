# How to Run

The workflow uses three slash commands in chat (`/setup`, `/fetchjobs`, `/improve`) and one
Streamlit UI. Steps 1–2 are one-time. Steps 5–8 form the steady-state loop.

1. `uv run python reset.py` — only when starting fresh; clears `data/` (keeps resume PDF).
2. `uv run python orchestrator.py` — creates `data/`, empty `candidate_info.json`, and the jobs DB.
3. Drop your resume PDF in `data/`. Filename must contain `resume` (e.g. `my_resume.pdf`).
4. **`/setup`** in chat (Cursor or Claude Code) — agent reads the resume, proposes a profile JSON, asks once which Claude Code plan you're on (Pro / Max 5x / Max 20x → saved as `plan_tier`), saves to `data/candidate_info.json`.
5. **`/fetchjobs`** in chat — opens with a variant prompt:
   - **Lean** (Sonnet 4.6, ~10 turns, ~500K tokens) — fits a Pro 5-hour rate-limit window. Externalizes job descriptions to `data/_descriptions/` and runs a background Scoring Subagent.
   - **Full** (Opus 4.7, ~150 turns, ~20M tokens) — needs a Max plan budget. The orchestrator + Context/Discovery/Persistence teams flow.

   The dispatcher lists the *recommended* variant first based on your `plan_tier` (Lean for Pro, Full for Max), but anyone can pick either. To skip the prompt forever, set `"runtime_mode_override": "lean"` (or `"full"`) in `data/candidate_info.json`. To bypass the dispatcher entirely, type **`/fetchjobs-pro`** directly — that always runs Lean.
6. **`uv run streamlit run app.py`** — open the Career Command Center UI.
   - Edit profile in the sidebar, click **Update Profile**.
   - Optional **Exclusions** expander: drop in `excluded_companies` (exact match),
     `excluded_areas` (substring on `theme`), and `excluded_pairs`
     (`company:area`, AND-match). Enforced both at `/fetchjobs` Step 3 and as a
     backstop in `persist_jobs`; the latter exposes `dropped_by_exclusion` and
     `exclusion_backstop_failed` counters in the debug summary so silent
     misconfigurations surface.
   - Set per-row **Lifecycle** (`New`/`Applied`/`InProgress`/`Closed`/`Won`/`NotForMe`),
     **Good/Bad**, and **Weight** in the table; click **Save feedback & status**.
     `Applied`/`InProgress`/`Closed`/`Won` stay POSITIVE-GENRE (the next search
     keeps nudging toward similar roles even after the specific row is closed).
7. Rerun **`/fetchjobs`** — new feedback/status biases the next search.
8. **`/improve`** — four modes:
   - **`--auto` (recommended for pro users):** set `auto_improve_enabled: true` in `data/candidate_info.json`. Every `/fetchjobs` then auto-runs `/improve --auto`, which (a) self-heals by reverting any prior applied change whose next-run metrics regressed (`valid_jobs < 0.85×`, `pct_high_score < 0.85×`, or `tokens_per_valid_job > 1.5×` vs. the change's recorded `pre_metrics`), (b) auto-applies Tier 1–4 compaction (hedge cleanup, example externalization, cold-section archive to `.claude/_archive/`, cross-file dedup), (c) stages PATTERN_*/SCORING_DRIFT_ to Streamlit for human review (those change behavior, not just cost), and (d) prints a brief `AUTO_SUMMARY` (reverted N, applied N, bytes reclaimed, total_skill_bytes trend, watching next-run change_ids). Pain-point thresholds activate from `n_priors_used ≥ 1` with graduated severity. Pro-user contract: run heavily and still have tokens left.
   - **`--audit-only` (legacy review-everything):** set `auto_improve_audit_enabled: true` instead. Every `/fetchjobs` stages every proposal — including cost-only ones — to `data/improve_proposals.jsonl`. Review and approve in **Streamlit → Analytics → Pending Improvements**. Per-change revert lives in the **Improvement History** table below.
   - **`--restore <change_id>`:** Streamlit "Revert" button on any applied row, or auto-dispatched when next-run regression detection fires. `archive_section` patches reconstitute the section from `semantic_diff.captured_content` and unlink the archive in one transaction.
   - **Manual:** type `/improve` in chat for the original one-at-a-time interactive walkthrough. See `.claude/commands/improve.md`.

## Snapshot review (optional)

Each `Update Profile` / `Save feedback` / `/fetchjobs` / wisdom update appends to
`data/history/*.db`. To inspect:

```bash
uv run python scripts/snapshot_history.py candidate
uv run python scripts/snapshot_history.py jobs
uv run python scripts/snapshot_history.py intelligence
```

## `uv` virtualenv warning

If you see `VIRTUAL_ENV=… does not match the project environment path`, that's a benign
notice from another project's `.venv` being active in your shell. Either run with
`uv run --active …` or `deactivate` the other env first. The job_finder commands work
either way.
