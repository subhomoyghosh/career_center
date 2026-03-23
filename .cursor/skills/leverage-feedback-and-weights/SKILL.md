---
name: leverage-feedback-and-weights
description: Use user feedback (good/bad) and per-job weight (0–100) from the jobs table to nudge search and scoring. When running /fetchjobs, load high-signal jobs and bias toward similar roles; when persisting, preserve existing feedback and weight.
---

# Leverage Feedback & Weights

The jobs table has **user_feedback** (`'good'`, `'bad'`, or NULL) and **user_weight** (0–100, default 50). The user sets these in the app. The next /fetchjobs run must use them so the system gets sharper over time.

## When to apply

- **Context (before search):** When running /fetchjobs, after loading `data/candidate_info.json` and before building queries.
- **Persistence (when saving jobs):** When calling `persist_jobs()`, ensure existing `user_feedback` and `user_weight` are preserved for rows that already exist in the DB (the `job_finder.persistence` module does this; do not overwrite with defaults).

## What to do

1. **Load high-signal jobs from the DB**
   - Run `uv run python scripts/get_nudge_context.py` and parse its JSON output, or call `get_high_signal_jobs(min_weight=70)` from `job_finder.persistence` (returns list of dicts: company, title, theme, rationale, user_weight, user_feedback).
   - Use these rows as **positive examples**: company, title, theme, rationale, and (if available) any description patterns.

2. **Nudge search and scoring**
   - **Query building:** Add or emphasize keywords, domains, and companies that appear in high-signal jobs when constructing search queries (e.g. site:lever.co, LinkedIn).
   - **Scoring:** When evaluating new roles, give a **bonus** to jobs that resemble high-signal ones (same theme, similar title, same company type). Scale the nudge by `user_weight` (e.g. weight 100 = strong positive signal; 50 = neutral).
   - **Wisdom:** When writing the `wisdom` update, mention which user-favored patterns showed up in this run (e.g. “User-marked-good roles in E-commerce; this run surfaced similar companies.”).

3. **When persisting new jobs**
   - Use `persist_jobs()` from `job_finder.persistence`. It **preserves** existing `user_feedback` and `user_weight` for jobs that are already in the DB (matched by `id` = hash of link). Do not pass default or empty feedback/weight for existing rows; the module merges them.

## Summary

- **Read** good/high-weight jobs from the DB at the start of /fetchjobs.
- **Use** them to bias queries, scoring, and wisdom.
- **Preserve** feedback and weight when persisting (handled by `persist_jobs`).
