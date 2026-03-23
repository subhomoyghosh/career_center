# Career Command Center Runbook

This repo is an autonomous job-search + match tracking package. The “brain” is implemented via Cursor rules (`.cursor/rules/*`) and the “tools” are the Python modules under `src/job_finder/`.

## Quick Start (fresh install)
1. Initialize data + DB (run once)
   - `python3 orchestrator.py`
   - or `uv run python orchestrator.py`
2. Drop your resume PDF into `data/`
   - Any filename containing `resume` (case-insensitive) is used.
3. Infer + create your profile (AI chat)
   - In Cursor, type `/setup` and review the proposed JSON.
   - Save to `data/candidate_info.json`.
4. Run the autonomous search
   - Type `/fetchjobs` after the profile is confirmed.
5. View/edit feedback in the UI
   - `streamlit run app.py`
   - Edit candidate config and job “Good/Bad” + weight, then click **Save feedback & weights**.

## Reset (start over)
1. `python3 reset.py`
2. Re-run `python3 orchestrator.py`
3. Re-run `/setup`, then `/fetchjobs`

Resume PDFs are not deleted by reset.

## Quality Gates (what must happen before persistence)
1. Active profile availability
   - `data/candidate_info.json` must exist and be valid JSON.
2. Link validation before `persist_jobs()`
   - Headless gate: `job_finder.link_validation.filter_valid_job_links(jobs)`
   - Removes non-`2xx` URLs (including LinkedIn 403/login walls).
   - For non-LinkedIn boards, also requires the HTML to echo enough of the job title to avoid generic board index pages.
3. Nudge context for the next run
   - High-signal jobs are those with `user_feedback='good'` or `user_weight >= 70`.
   - Run: `uv run python scripts/get_nudge_context.py`
4. Snapshots
   - Candidate edits and job persistence append to `data/history/*_history.db`.
   - Use `scripts/snapshot_history.py` to inspect recent rows.

## Optional QA / Judging (LLM-as-judge)
1. Evidence payload (for in-editor judging)
   - `uv run python scripts/dump_judge_context.py`
2. External prompt bundle (copy/paste)
   - `uv run python scripts/evaluate_nudge_system.py`

## Internal modules (for developers)
- `src/job_finder/config.py`: load/save and shape normalization for `candidate_info.json`
- `src/job_finder/persistence.py`: schema + upsert for `sovereign_agent.db`
- `src/job_finder/link_validation.py`: HTTP + dead-page + title-echo quality gate
- `src/job_finder/history.py`: append-only snapshot DBs

