# /fetchjobs

You MUST NOT fail silently.

Start by printing a small status block to the chat (even if later steps fail):
- `fetchjobs_start`: <timestamp>
- `active_profile_path`: `data/candidate_info.json` (or resolved active path)
- `profile_json_valid`: true/false
- `profile_keys_present`: list keys found (e.g. core_identity, scientific_moat, engineering_stack, target_seniority, target_country, priority_domains, search_targets, golden_keywords, noise_keywords, wisdom)
- `resume_pdf_candidate`: chosen `data/*resume*.pdf` path or `NONE`
- `nudge_context_high_signal_count`: count from `scripts/get_nudge_context.py`
- `plan`: short summary of next actions (ATS first vs LinkedIn first, etc.)

Prerequisite gating (print a clear reason then exit if missing):
- If `data/candidate_info.json` does not exist or is invalid JSON: instruct the user to run `/setup` and save the active profile.
- If no `data/*resume*.pdf` exists with "resume" in the filename: instruct the user to add a resume PDF into `data/` (filename must include `resume`).

Then continue with the normal sequence below.

**Active profile file:** `data/candidate_info.json`. (Same resolution as `job_finder.paths.resolve_active_config_path()`.)

---

## CRITICAL: Never use subagents for WebSearch

**Do not spawn Agent subagents to run web searches.** Subagents in this environment do not inherit WebSearch/WebFetch permissions — they will silently fail and waste tokens. All `WebSearch` and `WebFetch` calls must be made directly in the main agent turn. Parallelizing independent searches in a single message (multiple tool calls) is fine and encouraged; spawning a separate Agent for web discovery is not.

---

## Agent Teams (multi-agent execution model)

`/fetchjobs` runs as an **orchestrator + two specialist agents**. Architecture and logic are unchanged — only execution is parallelized.

| Team | Runs as | Responsibility |
|------|---------|----------------|
| **Context Team** | Parallel tool calls (main turn, Step 1) | Profile load + resume PDF + nudge context, all in one message |
| **Discovery Team** | Parallel tool calls (main turn, Steps 2–3) | All WebSearch waves + WebFetch waves in parallel batches; scoring in main context. **Cannot be delegated to a subagent.** |
| **Persistence Agent** | Background subagent (after Step 4) | `filter_valid_job_links` (live-link check) + `persist_jobs` + stale-link pruning + diagnostics emit. Spawned right after scoring so main agent can write wisdom in parallel. |

**Orchestration order:**

1. Fire **Context Team** (3 parallel tool calls) → wait for results.
2. Run **Discovery Team** in 3 sequential waves of parallel calls (Search Wave → Fetch Wave → Backfill Wave) → score candidates.
3. **Hand off scored candidates** to the Persistence Agent — link validation happens inside it (`filter_valid_job_links`) so main-agent context stays clean.
4. Spawn **Persistence Agent** in background with the full scored list (it filters, persists, and prunes).
5. While Persistence Agent runs: main agent writes wisdom.
6. Receive Persistence Agent output (`valid_jobs_count`, `stale_links_pruned`) → incorporate into wisdom if needed → print final summary.
7. **Final Display** (Step 9): query the cleaned DB and print the jobs table grouped by recency — this-run jobs first, prior-run jobs second. Must run AFTER stale-link pruning has completed so dead rows never appear.

---

## Sequence:

1. **Context Ingestion** *(Context Team — fire all three as parallel tool calls in one message)*:
   - Load the active profile JSON (`candidate_info.json`) and use **all** of: `core_identity`, `scientific_moat`, `engineering_stack`, `target_seniority`, `target_country`, `golden_keywords`, `noise_keywords`, `priority_domains`, `search_targets`, `wisdom`. Optionally `priority_industries` (alias for priority_domains), `peer_companies`.
   - **User feedback & weights (nudge):** Run `uv run python scripts/get_nudge_context.py` and use its JSON output (or call `get_high_signal_jobs()` from `job_finder.persistence`). Use the listed jobs as **positive signals**: bias search queries and scoring toward similar roles (company, title, theme, rationale); mention these patterns in the wisdom synthesis. Apply stronger nudge for higher `user_weight` (e.g. 100 = very strong signal).
   - Parse the resume PDF: list `data/` (e.g. `ls data/`) and pick any `.pdf` whose filename contains "resume" (case-insensitive). Use that file to extract Top 3 Achievements and enrich rationale (no hard-coded company names). (PDFs may be gitignored — do not rely on glob alone.)
   - **`peer_companies` organic search:** If `peer_companies` is non-empty, run additional discovery queries such as `"[peer] competitor" "Applied Scientist" site:lever.co` or `"people also hired from [peer]" "Data Scientist"` to find adjacent companies hiring from similar talent pools. If `peer_companies` is empty, suggest the user populate it with 2–3 prior employers or known peer companies so future runs can exploit this signal.

2. **Vectorized Search (Active Roles Only) — use every field** *(Discovery Team — fire in 3 waves, each wave is one parallel message)*:

   **Wave 1 — Search Wave:** Fire all primary `WebSearch` queries simultaneously in a single message (target 8–10 calls). Build queries per the rules below, then dispatch all at once.

   **Wave 2 — Fetch Wave:** After Wave 1 returns, fire all `WebFetch` calls for promising candidates simultaneously in a single message (target 6–8 calls). Includes aggregator fallback fetches for JS-rendered ATS pages.

   **Wave 3 — Backfill Wave:** After Wave 2 returns, fire any remaining secondary `WebSearch` queries (ATS backfill for LinkedIn, aggregator searches for 403 Lever/Ashby pages) + any follow-up `WebFetch` calls simultaneously in a single message.

   - **Site targets:** If `search_targets` is present, run **one or more queries per target** using `site:[domain]`. Always include both `jobs.lever.co` and `lever.co`, and both `job-boards.greenhouse.io` and `boards.greenhouse.io` if either appears in `search_targets`.
   - **Workday path (dedicated):** If `search_targets` includes `workday.com` or `myworkdayjobs.com`, run queries against **`site:myworkdayjobs.com`** (not `site:workday.com` — the latter rarely surfaces individual listings). Example: `site:myworkdayjobs.com "Applied Scientist" ("Bayesian" OR "Causal Inference") USA`. Also try `"[company] careers" "Applied Scientist" workday` for companies known to use Workday.
   - **LinkedIn (dedicated path):** If `search_targets` includes **`linkedin.com/jobs`**, run `site:linkedin.com/jobs` searches. **However:** LinkedIn via `site:` often returns stale aggregation pages rather than individual listings. Treat LinkedIn as a **discovery hint layer only** — extract company+title from any hits, then immediately run a secondary ATS backfill search (e.g. `"[company]" "[title]" site:lever.co OR site:greenhouse.io OR site:ashbyhq.com`) to find the direct ATS URL. Persist the ATS link, not the LinkedIn link, whenever possible.
   - **ATS backfill for LinkedIn hits:** If LinkedIn fetch is blocked/login-walled, do not stop: run company+title ATS search and continue. Keep LinkedIn URL only as a last resort when no ATS link is found.
   - **Query building:** Combine (1) **golden_keywords** (roles + methods), (2) **scientific_moat** terms, (3) **engineering_stack** terms, (4) **priority_domains** so each query is specific. Use `target_country` and recency tokens ("2026", "Hiring Now") where appropriate. Run queries at two levels of specificity: a tight version (3+ keywords) and a relaxed version (2 keywords + domain) to avoid zero-result dead ends.
   - **Discovery must be live (MCP web search):** For each query you construct, use `WebSearch` to retrieve current results and `WebFetch` for promising candidates. Do not guess or reuse a fixed shortlist. Deduplicate by `link`.
   - **Hard MCP requirement (no silent fallback):**
     - Minimum per run: at least **8 `WebSearch` calls** and at least **5 `WebFetch` calls** before concluding discovery.
     - Print a compact diagnostics block before scoring:
       - `mcp_websearch_calls`: <int>
       - `mcp_webfetch_calls`: <int>
       - `mcp_candidates_extracted`: <int>
       - `mcp_blocked_or_failed_calls`: <int>
     - If MCP calls were not made (or all failed), print: `MCP_DISCOVERY_FAILED` with reason and stop pretending discovery succeeded.
   - **Lever API path (use for ALL Lever jobs — never fetch `jobs.lever.co` directly):** `jobs.lever.co` returns 403 to all non-browser clients regardless of job status. Before any WebFetch of a Lever URL, convert it to the public JSON API endpoint: `https://jobs.lever.co/{company}/{jobId}` → `https://api.lever.co/v0/postings/{company}/{jobId}?mode=json`. This endpoint is unauthenticated, returns structured JSON containing `descriptionPlain`, `listsPlain`, and `createdAt` (epoch ms — the job's posting timestamp), and is used by all major aggregators without bot detection. Apply this conversion for **both description fetching (Wave 2) and link existence checks (Step 4)**. A live job returns a JSON object with a `text` field; a dead or missing job returns `{"code":"NotFound"}` or `{"code":"Gone"}` — use these as the dead-job signal. Only fall back to aggregator search (`site:builtin.com OR site:simplify.jobs`) if the API URL also returns NotFound/Gone or a connection error. **When the API returns `createdAt`, capture it on the job dict as `posted_at` (epoch ms is fine — `persistence._normalize_posted_at` converts to ISO automatically).**

   - **Posted-date extraction (best-effort, optional but valued):** For each candidate, attempt to capture an absolute posting timestamp into a `posted_at` field on the job dict. The persistence layer normalizes epoch ms / epoch seconds / ISO strings; non-parseable strings are stored verbatim, and missing values are stored as NULL — so do not fabricate. Sources by reliability:
     - **Lever API**: `createdAt` (epoch ms) — always reliable when the API call succeeds. Extract this every time.
     - **Greenhouse**: HTML rarely exposes a date; skip unless the page surfaces an explicit `updated_at` or "Posted on YYYY-MM-DD".
     - **Workday**: pages often render relative phrases ("Posted 5 days ago"). If parseable, convert to an ISO date by subtracting from the current run date; if only `Posted Today` / `Posted Yesterday`, that's still useful. Otherwise leave NULL.
     - **Ashby**: typically CSS-only via WebFetch — skip; rely on aggregator fallback.
     - **Aggregators (builtin.com, simplify.jobs, levels.fyi)**: often surface an absolute "Posted on …" line. Capture it if visible.
     - **LinkedIn**: relative dates only and frequently stale — do not trust LinkedIn for `posted_at`; leave NULL.

     If you can't get a reliable absolute date, omit the field entirely. NULL is a fine and expected outcome — the recency view in Step 9 falls back to `first_seen` which is always populated.
   - **Ashby pages are JS-rendered:** `WebFetch` on `ashbyhq.com` job URLs typically returns CSS only. Run aggregator fallback: `"[company]" "[title]" site:builtin.com OR site:simplify.jobs OR site:levels.fyi`. Log fallbacks in the diagnostics block.
   - **Verify individual listings, not board pages:** Before adding a candidate to your list, confirm the URL resolves to a single job posting (not a board index). Signal of a board page: URL contains `?department=`, `?location=`, or `?error=true`; body shows 100+ job titles; no company-specific description. If you land on a board, do not persist that URL — run a more specific search instead (e.g. add job ID or title to query).
   - Print a short LinkedIn summary before validation: `linkedin_discovered`, `linkedin_with_ats`, `linkedin_fallback_only`, `linkedin_dropped_reason_counts`.

3. **Scientific Moat Evaluation (Moat-Seeker Logic):**
   - **Score 90–100:** Job description **explicitly requires 3 or more** items from `scientific_moat`. Strong alignment with rare, high-barrier skills. Bonus if the role is in a `priority_domain`.
   - **Score 70–89:** Matches `core_identity` and **at least 2** items from `engineering_stack`. Good fit but fewer moat signals.
   - **Penalty:** Deduct points for generic job descriptions that do not mention rigorous evaluation or specialized modeling.
   - **Hard filter:** Discard any role whose title or description matches **any** of `noise_keywords` (e.g. Junior, Intern, Web Developer, Front End, Marketing Analyst, Business Intelligence, Entry Level, Contract).
   - **Vesting & Funding:** Bonus 1.2× for "Series C/D," "IPO-bound," "NIST Grant," "DOE funding." Negative signal for generic "Agency" or "Consultancy" unless explicitly PhD/scientific.
   - **Scoring confidence:** If job description text was unavailable (Lever/Ashby CSS fallback) and no aggregator text was found, cap score at 79 and note `description_not_fetched: true` in rationale.
   - **Requirement:** The `RATIONALE` must explain **how the candidate's `scientific_moat` solves a specific problem mentioned in the job description.**

4. **Scoring Handoff** *(main agent — no WebFetch calls)*:

   Live-link validation is **delegated to the Persistence Agent** via `job_finder.link_validation.filter_valid_job_links`, which catches more dead-job patterns than ad-hoc WebFetch (e.g. Greenhouse silent-redirects to `/{org}?error=true`, ATS board pages without the original job ID, bot interstitials, sub-MIN_BODY_CHARS empty renders). Keeping it in Python also avoids pulling 13× WebFetch payloads into the main-agent context.

   - **Only forward candidates that passed scoring** (score ≥ 70 and not filtered by `noise_keywords`).
   - **Lever URL note:** still convert `jobs.lever.co/{company}/{jobId}` → `https://api.lever.co/v0/postings/{company}/{jobId}?mode=json` **during Wave 2 description fetching** (Step 2) so you can capture `createdAt` → `posted_at`. The final `link` written to the DB should remain the human-readable `jobs.lever.co` URL; `filter_valid_job_links` HEAD-checks that URL fine.
   - Print: `scored_candidates_count`, sample of top-3 `{company, title, score}`. Hand the full scored list to the Persistence Agent.

   Wave 2/3 description fetches already act as a soft pre-filter (4× already-known 404s from Lever API responses were dropped before scoring). Do **not** re-implement the live check here in the main agent — it's strictly the Persistence Agent's job now.

5. **Persistence** *(Persistence Agent — spawn as background subagent after Step 4)*:

   Hand the scored job list to a **background Persistence Agent** (uses only Python/Bash — no WebSearch/WebFetch). The main orchestrator proceeds immediately to Step 6 (Wisdom Loop) in parallel.

   The Persistence Agent must:
   - **Do not call terminal scripts for persistence.** Persistence happens by calling Python functions in-context (no `python3 scripts/run_fetchjobs.py` fallback).
   - **Run `filter_valid_job_links` first** to drop dead listings. This is mandatory; do NOT skip it. Example:

     ```python
     from job_finder.link_validation import filter_valid_job_links
     alive = filter_valid_job_links(jobs, require_title_in_body=True, check_content=True)
     dead = [j for j in jobs if j["link"] not in {a["link"] for a in alive}]
     print(f"link_check_total: {len(jobs)}, link_check_dead: {len(dead)}, link_check_passed: {len(alive)}")
     for d in dead[:5]:
         print(f"  DROP: {d['company']} | {d['title']} | {d['link']}")
     ```

     This catches: HTTP non-2xx, `?error=true` redirects (Greenhouse silent-dead), final URL no longer containing the original `/jobs/{id}`, bot interstitials, sub-MIN_BODY_CHARS empty Workday/JS renders, and DEAD_PAGE_PHRASES in body.
   - Call `persist_jobs(alive)` (not the original `jobs`) — only live rows enter the DB. Always call it even if `alive` is empty.
   - Print `discovered_jobs_count` (= `len(alive)`) and a small sample of `{company,title,link}` before persisting.
   - **Schema contract (important):** Each job dict passed to `persist_jobs` must include *non-empty* keys exactly named `company`, `title`, `link`, `score`, `theme`, `rationale`. If any required key is missing, `persist_jobs` will silently skip that row.
   - **Snapshots:** `persist_jobs` appends a full jobs-table snapshot to `data/history/jobs_history.db` (append-only).
   - **Stale-link pruning (run after `persist_jobs`, inside Persistence Agent):**
     - Load all existing jobs via direct SQLite: `existing = conn.execute("SELECT company, title, link FROM jobs").fetchall()`.
     - Exclude any job whose `link` appears in the current run's `alive` list — those were just live-checked.
     - For the remaining (pre-existing) rows, **reuse `filter_valid_job_links`** (same module used in Step 5) to get the active subset. This guarantees the same dead-job rules apply to new and existing rows (no rule drift):

       ```python
       from job_finder.link_validation import filter_valid_job_links
       pre = [{"company": c, "title": t, "link": l} for (c, t, l) in existing if l not in {a["link"] for a in alive}]
       pre_alive = filter_valid_job_links(pre, require_title_in_body=True, check_content=True)
       pre_alive_links = {j["link"] for j in pre_alive}
       dead = [j for j in pre if j["link"] not in pre_alive_links]
       ```

     - Delete dead rows via direct SQLite: compute `id = hashlib.md5(link.encode()).hexdigest()` for each dead link, then `conn.execute("DELETE FROM jobs WHERE id = ?", (id,))`. Print `stale_links_checked`, `stale_links_pruned`, and a sample of pruned `{company, title, link}`.
     - **Token preservation:** All pruning logic runs in Python — only the summary counts are returned to the orchestrator.
   - Also run Step 7 (Diagnostics Emit) — see below.
   - Return `valid_jobs_count`, `stale_links_pruned` to the orchestrator.

   The orchestrator waits for the Persistence Agent before printing the final summary. If `valid_jobs_count` differs from what was assumed during wisdom writing, append a one-sentence correction.

6. **Wisdom Loop** *(main orchestrator — runs in parallel with Persistence Agent)*:
   - Analyze the **entire scored batch** (not just top 1-2 jobs): synthesize cumulative patterns across domain mix, role seniority, methods demanded, tooling patterns, and hiring signals.
   - Generate wisdom as **3-6 short, clear sentences** when you have validated jobs evidence.
   - If `valid_jobs` is empty, generate **1-2 sentences** that (a) explicitly say the evidence was empty and (b) state the most likely gating cause and what to change next time.
   - Ensure each sentence is grounded in current run evidence. If evidence is weak, say so explicitly.
   - The wisdom string must be written by the agent during `/fetchjobs` — not delegated to user edits.
   - Update the **active profile JSON** (`candidate_info.json`): set **`wisdom`** only. **Preserve every other key** when writing.

7. **Diagnostics Emit (required — feeds `/improve`)** *(runs inside Persistence Agent, after `persist_jobs`)*:

   Append a single JSON object (one line) to `data/run_diagnostics.jsonl`. Create the file if it does not exist. Never overwrite — always append. Include:
   ```json
   {
     "run_date": "<ISO timestamp>",
     "websearch_calls": <int>,
     "webfetch_calls": <int>,
     "candidates_extracted": <int>,
     "link_check_total": <int>,
     "link_check_dead": <int>,
     "link_check_passed": <int>,
     "link_check_skipped": false,
     "valid_jobs": <int>,
     "stale_links_pruned": <int>,
     "board_page_hits": <int>,
     "zero_result_queries": <int>,
     "zero_result_query_strings": ["<query>", ...],
     "lever_css_fallback_count": <int>,
     "workday_hits": <int>,
     "linkedin_discovered": <int>,
     "linkedin_with_ats": <int>,
     "linkedin_fallback_only": <int>,
     "domain_coverage": {"<domain>": <count>},
     "score_distribution": {"90-100": <int>, "70-89": <int>, "below-70": <int>},
     "search_targets_used": ["<domain>", ...]
   }
   ```
   Write this using a Python one-liner via Bash: `uv run python -c "import json, pathlib; pathlib.Path('data/run_diagnostics.jsonl').open('a').write(json.dumps({...}) + '\n')"`. Fill all fields from the actual run metrics collected above. This file is gitignored and stays local.

8. **LLM judge (optional QA):** Use the **`evaluate-nudge-and-wisdom`** skill (`.cursor/skills/evaluate-nudge-and-wisdom/SKILL.md`): run `uv run python scripts/dump_judge_context.py`, then judge in chat (**nudge + MCP verification of listing links + wisdom**). External LLM prompts: `uv run python scripts/evaluate_nudge_system.py`.

9. **Final Display & Cleanup (required — runs at the very end, in main agent)**:

   By this point the Persistence Agent has finished, including stale-link pruning. This step is the user-facing payoff: a clean, recency-grouped view of the active job board.

   **a. Pruning guarantee:** Confirm `stale_links_pruned` was reported by the Persistence Agent. If the Persistence Agent failed or returned no value, run the same pruning logic now in the main agent before display — never show a table that may contain dead links. Print the count and a 3-line sample of pruned `{company, title, link}`.

   **b. Build the recency-grouped table:** Track the set of `link`s persisted **in this run** (call it `this_run_links` — built from the survivors handed to the Persistence Agent). Then load the live DB:

   ```python
   import sqlite3
   from job_finder.paths import get_db_path
   conn = sqlite3.connect(get_db_path())
   # first_seen IS NULL → backfilled rows from before the migration; sort them last.
   rows = conn.execute(
       """SELECT company, title, score, theme, link, status, first_seen, posted_at
          FROM jobs
          ORDER BY (first_seen IS NULL), first_seen DESC, score DESC, company ASC"""
   ).fetchall()
   ```

   Split rows into two groups by `link` membership in `this_run_links`:
   - **Group 1 — Found this run** — newly discovered or re-confirmed today. Within this group, sort by `score DESC`, then `company ASC` (this run's `first_seen` values are all within seconds of each other, so score is the more useful primary sort).
   - **Group 2 — Earlier runs** — everything else still alive after pruning. Within this group, keep the SQL order: `first_seen DESC` (most-recently-discovered runs first), with NULL `first_seen` rows (pre-migration backfill) sorted last; tiebreak by score then company.

   **c. Print the table** to chat using markdown, two sections with clear headers:

   ```text
   ## Found this run (N jobs)
   | Score | Company | Title | Theme | Posted | Link |
   |-------|---------|-------|-------|--------|------|
   ...

   ## Earlier runs (M jobs)
   | Score | Company | Title | Theme | First seen | Posted | Link |
   |-------|---------|-------|-------|------------|--------|------|
   ...
   ```

   Formatting rules:
   - Truncate Title to 60 chars and Theme to 30 chars.
   - Render Link as `[host](url)`.
   - **Posted**: render `posted_at` as `YYYY-MM-DD` if present, else `—` (em dash). Do NOT omit the column when most rows are null — the dashes are informative.
   - **First seen** (Group 2 only): render as `YYYY-MM-DD`; for NULL (pre-migration) print `—`.
   - If a group is empty, print `(none)` under the header — do not omit the section.

   **d. Print final counts:** `final_table_total`, `final_table_this_run`, `final_table_earlier`, `final_table_pruned_this_run`, `final_table_posted_at_coverage` (fraction of rows with non-null `posted_at`, e.g. `12/47`).

   **Schema reference:** The `jobs` table has `first_seen TEXT` (always stamped on insert by `persist_jobs`) and `posted_at TEXT` (best-effort, may be NULL). Both are ISO 8601 UTC strings, so lexical sort matches chronological sort. `posted_at` is only as accurate as what the agent extracted during discovery — see Step 2's posted-date extraction notes. Pre-migration rows have `first_seen IS NULL`; treat as oldest.

---

## Reliability Guardrails (must print every run)
- Print `fetchjobs_mode`: `mcp_live_search`.
- Print `discovery_path_used`: list of tools actually used (e.g., `WebSearch, WebFetch`).
- Print `python_scraper_used`: always `false` for discovery in this rule.
- Print `lever_ashby_description_fallback_used`: true/false — was builtin.com/simplify.jobs used to retrieve any job description?
- Print `link_check_skipped`: true/false — was Step 4 existence check bypassed due to rate-limiting?
- Print `stale_links_pruned`: count of pre-existing DB jobs deleted as dead this run.
- Print `final_table_total`, `final_table_this_run`, `final_table_earlier`: counts from the Step 9 recency-grouped display. The recency table must be printed every run, even if `final_table_this_run == 0`.
