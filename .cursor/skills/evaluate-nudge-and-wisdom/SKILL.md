---
name: evaluate-nudge-and-wisdom
description: LLM-as-judge for nudge wiring (feedback/weights), listing URL quality vs title/rationale, and wisdom / Market Intelligence table. Use after /fetchjobs or when auditing the jobs DB.
---

# Evaluate nudge, links, and wisdom (in-chat judge)

You **are** the judge. Do not tell the user to run `scripts/evaluate_nudge_system.py` unless they want terminal copy-paste prompts for an **external** LLM.

## When to use

- User asks to **judge**, **evaluate**, **QA**, or **audit**: nudge system, high-signal jobs, feedback/weights, **wisdom**, **Market Intelligence**, or **whether listing URLs match the jobs**.
- After **/fetchjobs** or profile edits.

## What to do (always)

### 1. Load structured context (project root)

```bash
uv run python scripts/dump_judge_context.py
```

JSON includes:

- `schema_ok`, `core_columns_ok`, `verdict`, `message`
- `wisdom_raw`, `intelligence_rows`
- `high_signal_jobs`, `high_signal_count`
- **`jobs_link_audit_sample`** — `company`, `title`, `link`, `score`, `rationale_preview` (use to MCP-verify URLs)
- `jobs_wisdom_context`

**Note:** There are **no** `country`, `city`, `work_mode`, or `date_posted` columns anymore.

### 2. Judgment A — Nudge + listing links (only if DB exists)

- **Nudge:** `schema_ok`, `high_signal_count`, sample rows — path for next /fetchjobs to bias search?
- **Links:** For **each** row in `jobs_link_audit_sample`, **MCP web fetch** `link`. Does the live page match **company + title + rationale_preview**? Flag wrong board, expired, or mismatched role. LinkedIn login wall → **UNVERIFIED** for that row.

**Output:**

```text
NUDGE_LINK_VERDICT: OK | FAIL | CONDITIONAL
NUDGE: (one sentence)
LISTING_URLS: (one sentence — how many verified vs blocked)
```

### 3. Judgment B — Wisdom + intelligence table

Same rubric as before: sense, grounding vs `jobs_wisdom_context`, actionable, non-obvious patterns.
Also enforce writing quality:

- Wisdom should read as **3-6 short sentences** (one claim per sentence).
- Prefer cumulative synthesis from the **full jobs_wisdom_context**, not isolated single-job comments.
- Flag run-on or jumbled statements as **WEAK** even when factually correct.
- Intelligence table should be **one-column bullet rows**, each row an independent, evidence-grounded insight.
- Remove/flag rows that are generic, repetitive, or not supported by current jobs evidence.

**Required structure:**

```text
WISDOM_QUALITY: OK | WEAK | FAIL
(one sentence)

TABLE_QUALITY: OK | WEAK | FAIL
(one sentence)

NON_OBVIOUS_INSIGHTS:
- …

ACTIONABLE_NEXT_STEPS:
1. …

OPTIONAL_WISDOM_REWRITE:
…

OVERALL_VERDICT: PASS | NEEDS_REVISION
```

### 4. Optional

- Wisdom-only: shorten Judgment A but still mention link audit if user cares about data quality.
- When rewriting wisdom, keep it concise and cumulative (3-6 short sentences).

## Summary

| Step | Action |
|------|--------|
| Context | `uv run python scripts/dump_judge_context.py` |
| Judge A | Nudge + **MCP-fetch** link sample |
| Judge B | Wisdom + table + rewrite |

**Link validation policy:** `.cursor/skills/validate-job-links/SKILL.md`
