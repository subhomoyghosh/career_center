#!/usr/bin/env python3
"""
CLI: print nudge + listing-link + wisdom judge prompts (for copy-paste into an external LLM).
For in-editor judging, use the Cursor skill **evaluate-nudge-and-wisdom** instead.

Run: uv run python scripts/evaluate_nudge_system.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(ROOT, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.judge_context import build_judge_report


def build_judge_prompt(report: dict) -> str:
    link_sample = json.dumps(report.get("jobs_link_audit_sample", []), indent=2)
    return f"""You are evaluating whether the "nudge system" in a job-search app is working, and whether **stored listing URLs** plausibly match the jobs (title + rationale).

**Definition:** The nudge system uses user feedback (good/bad) and per-job weight (0–100) from the jobs table. When the user runs /fetchjobs again, the agent should load jobs where user_feedback='good' or user_weight >= 70 and use them as positive signals to bias search queries and scoring.

**Note:** The database does **not** store country, city, state, work_mode, or date_posted (removed as unreliable). Trust comes from **link + title + rationale** and automated `filter_valid_job_links` before persist.

**Evidence from the system:**

1. Active config path: {report.get('config_path')}. Config exists: {report['config_exists']}. DB exists: {report['db_exists']}. Schema has user_feedback and user_weight: {report['schema_ok']}. Core job columns present: {report.get('core_columns_ok')}.
2. High-signal jobs (good or weight >= 70): {report['high_signal_count']} job(s).
3. Programmatic path: get_high_signal_jobs(); scripts/get_nudge_context.py.

**High-signal jobs (sample):**
{json.dumps(report.get('high_signal_jobs', [])[:5], indent=2)}

**Job rows — link audit sample (MCP-fetch each `link` to verify the page is the right role):**
{link_sample}

**Questions:**
(a) Is the nudge system correctly set up so that the next /fetchjobs run would use these jobs to bias search and scoring?
(b) For the sample rows, after fetching each link (or noting login walls), does the live page match company + title + rationale_preview?
(c) Any suspicious URLs (wrong board, expired, generic search page)?

Answer in exactly this format:
VERDICT: OK or VERDICT: FAIL
One sentence each for (a), (b), and (c).
"""


def build_wisdom_intelligence_judge_prompt(report: dict) -> str:
    wisdom = report.get("wisdom_raw") or ""
    rows = report.get("intelligence_rows") or []
    jobs_ctx = json.dumps(report.get("jobs_wisdom_context", []), indent=2)
    table_json = json.dumps(rows, indent=2, ensure_ascii=False)
    wisdom_display = wisdom if len(wisdom) <= 6000 else wisdom[:5997] + "..."

    return f"""You are a **senior labor-market / hiring intelligence judge** for a technical job-search assistant (PhD-level applied science / ML roles).

**Your job:** Assess whether the **`wisdom`** field and the **Market Intelligence table** (Aspect | Insight rows derived by splitting the wisdom string) are **worth using**. They should:
1. **Make sense** — logically consistent, no internal contradictions, aspects should align with their insight sentences.
2. **Be grounded** — claims should be plausible given the **current job sample** below (companies, titles, themes, scores). Flag generic platitudes that could apply to any candidate.
3. **Be actionable** — the reader should know what to *do* next (e.g. emphasize X in applications, prioritize Y boards, avoid Z titles). Vague "market is hot" is insufficient.
4. **Surface non-obvious signal** — reward patterns that are **not** trivial from skimming job titles alone (e.g. concentration of methods, seniority drift, ATS patterns, domain shifts). Call out **missed** opportunities: themes present in the job data that wisdom ignores.

---

**Full `wisdom` text (from profile JSON):**
---
{wisdom_display if wisdom_display.strip() else "(empty or placeholder)"}
---

**Intelligence table (Aspect | Insight) — same parsing as the Streamlit app:**
```json
{table_json if rows else "[]"}
```

**Top jobs in DB (context for grounding — use to validate / sharpen wisdom):**
```json
{jobs_ctx}
```

---

**Deliver your judgment in this exact structure:**

**WISDOM_QUALITY:** OK | WEAK | FAIL  
One sentence: does the raw wisdom justify its claims vs the job sample?

**TABLE_QUALITY:** OK | WEAK | FAIL  
One sentence: do Aspect labels match insights; any broken splits or misleading aspects?

**NON_OBVIOUS_INSIGHTS:**  
Bullet list (2–5): specific patterns **supported by the job context** that are valuable and **under-emphasized or missing** in current wisdom.

**ACTIONABLE_NEXT_STEPS:**  
Numbered list (2–4): concrete steps for the candidate or the next /fetchjobs run (queries, domains, title filters, profile edits).

**OPTIONAL_WISDOM_REWRITE:**  
If WISDOM_QUALITY is WEAK or FAIL, paste **one** improved paragraph (≤120 words) that merges the best of current wisdom with your NON_OBVIOUS_INSIGHTS; otherwise write "N/A".

**OVERALL_VERDICT:** PASS | NEEDS_REVISION  
One line summary.
"""


def main() -> None:
    report = build_judge_report()
    skip_links = not report.get("db_exists")

    print("--- Nudge + schema report ---")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k
                not in (
                    "high_signal_jobs",
                    "jobs_link_audit_sample",
                    "jobs_wisdom_context",
                    "intelligence_rows",
                )
            },
            indent=2,
        )
    )
    if report.get("wisdom_raw"):
        print(f"\nWisdom preview ({len(report['wisdom_raw'])} chars): {report['wisdom_raw'][:200]}...")
    if report.get("intelligence_rows"):
        print(f"\nIntelligence table rows: {len(report['intelligence_rows'])}")
    if report.get("high_signal_jobs"):
        print("\nHigh-signal jobs (up to 5):")
        for j in report["high_signal_jobs"][:5]:
            print(f"  - {j.get('company')} | {j.get('title')} | weight={j.get('user_weight')} | {j.get('user_feedback')}")
    if report.get("jobs_link_audit_sample"):
        print("\nLink audit sample (for judge):")
        for j in report["jobs_link_audit_sample"][:5]:
            print(f"  - {j.get('company')}: {j.get('title')[:50]}... | {j.get('link', '')[:60]}...")

    print("\n" + "=" * 72)
    print("--- LLM JUDGE PROMPT 1: Nudge + listing link quality ---")
    print("=" * 72)
    if not skip_links:
        print(build_judge_prompt(report))
    else:
        print("(Skipped: no jobs database.)")

    print("\n" + "=" * 72)
    print("--- LLM JUDGE PROMPT 2: Wisdom + Market Intelligence table ---")
    print("=" * 72)
    print(build_wisdom_intelligence_judge_prompt(report))

    print("\n--- End ---")
    print("\nTip: In Cursor Chat, use the **evaluate-nudge-and-wisdom** skill for the same analysis in-editor without copy-paste.")
    sys.exit(0 if report["verdict"] == "OK" else 1)


if __name__ == "__main__":
    main()
