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

## Sequence:

1. **Context Ingestion:**
   - Load the active profile JSON (`candidate_info.json`) and use **all** of: `core_identity`, `scientific_moat`, `engineering_stack`, `target_seniority`, `target_country`, `golden_keywords`, `noise_keywords`, `priority_domains`, `search_targets`, `wisdom`. Optionally `priority_industries` (alias for priority_domains), `peer_companies`.
   - **User feedback & weights (nudge):** Run `uv run python scripts/get_nudge_context.py` and use its JSON output (or call `get_high_signal_jobs()` from `job_finder.persistence`). Use the listed jobs as **positive signals**: bias search queries and scoring toward similar roles (company, title, theme, rationale); mention these patterns in the wisdom synthesis. Apply stronger nudge for higher `user_weight` (e.g. 100 = very strong signal).
   - Parse the resume PDF: list `data/` (e.g. `ls data/`) and pick any `.pdf` whose filename contains "resume" (case-insensitive). Use that file to extract Top 3 Achievements and enrich rationale (no hard-coded company names). (PDFs may be gitignored — do not rely on glob alone.)
   - **`peer_companies` organic search:** If `peer_companies` is non-empty, run additional discovery queries such as `"[peer] competitor" "Applied Scientist" site:lever.co` or `"people also hired from [peer]" "Data Scientist"` to find adjacent companies hiring from similar talent pools. If `peer_companies` is empty, suggest the user populate it with 2–3 prior employers or known peer companies so future runs can exploit this signal.

2. **Vectorized Search (Active Roles Only) — use every field:**
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
   - **Lever/Ashby pages are JS-rendered:** `WebFetch` on `jobs.lever.co` and `ashbyhq.com` job URLs typically returns only CSS — no readable job description text. When this happens, **do not skip the job**. Instead run a secondary search: `"[company]" "[title]" site:builtin.com OR site:simplify.jobs OR site:levels.fyi` to retrieve the description text from an aggregator. Use that text for scoring and rationale. Log the fallback in the diagnostics block.
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

4. **Persistence:**
   - **Do not call terminal scripts for persistence.** Discovery/scoring must happen inside the agent (Claude Code + MCP tools). Persistence happens by calling Python functions in-context (no `python3 scripts/run_fetchjobs.py` fallback).
   - **Link validation (primary quality gate):** Before writing to the DB, run **`filter_valid_job_links`** (from `job_finder.link_validation`) with `require_title_in_body=False`. Drops: bad/missing URLs, non-2xx, short/empty bodies, dead-job phrases, and board-index pages.
     - **Known false-positive pattern:** Some ATS pages (e.g. Greenhouse-hosted boards, fintech career sites) embed CDN asset paths that may contain substrings matching dead-page phrases. If `content_failed > 0`, manually HTTP-check each failing URL (using `requests.get`) and print `status_code` + first 200 chars of body. Re-admit any URL that returns 200 with non-trivial job content (confirmed live). Log each manual rescue with reason.
     - Immediately before calling `filter_valid_job_links`, print `discovered_jobs_count` and a small sample of `{company,title,link}`.
     - After validation, print `valid_jobs_count` and a small sample of `{company,title,link}` from `valid_jobs`.
     - Always call `persist_jobs(valid_jobs)` even if `valid_jobs` is empty.
   - **Schema contract (important):** Each job dict passed to `persist_jobs` must include *non-empty* keys exactly named `company`, `title`, `link`, `score`, `theme`, `rationale`. If any required key is missing, `persist_jobs` will silently skip that row.
   - **Snapshots:** `persist_jobs` appends a full jobs-table snapshot to `data/history/jobs_history.db` (append-only).

5. **Wisdom Loop:**
   - Analyze the **entire validated batch** (not just top 1-2 jobs): synthesize cumulative patterns across domain mix, role seniority, methods demanded, tooling patterns, and hiring signals.
   - Generate wisdom as **3-6 short, clear sentences** when you have validated jobs evidence.
   - If `valid_jobs` is empty, generate **1-2 sentences** that (a) explicitly say the evidence was empty and (b) state the most likely gating cause and what to change next time.
   - Ensure each sentence is grounded in current run evidence. If evidence is weak, say so explicitly.
   - The wisdom string must be written by the agent during `/fetchjobs` — not delegated to user edits.
   - Update the **active profile JSON** (`candidate_info.json`): set **`wisdom`** only. **Preserve every other key** when writing.

6. **Diagnostics Emit (required — feeds `/improve`):**
   After the wisdom loop completes, append a single JSON object (one line) to `data/run_diagnostics.jsonl`. Create the file if it does not exist. Never overwrite — always append. Include:
   ```json
   {
     "run_date": "<ISO timestamp>",
     "websearch_calls": <int>,
     "webfetch_calls": <int>,
     "candidates_extracted": <int>,
     "content_failed": <int>,
     "manual_rescues": <int>,
     "valid_jobs": <int>,
     "board_page_hits": <int>,
     "zero_result_queries": <int>,
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

7. **LLM judge (optional QA):** Use the **`evaluate-nudge-and-wisdom`** skill (`.cursor/skills/evaluate-nudge-and-wisdom/SKILL.md`): run `uv run python scripts/dump_judge_context.py`, then judge in chat (**nudge + MCP verification of listing links + wisdom**). External LLM prompts: `uv run python scripts/evaluate_nudge_system.py`.

---

## Reliability Guardrails (must print every run)
- Print `fetchjobs_mode`: `mcp_live_search`.
- Print `discovery_path_used`: list of tools actually used (e.g., `WebSearch, WebFetch`).
- Print `python_scraper_used`: always `false` for discovery in this rule.
- Print `lever_ashby_description_fallback_used`: true/false — was builtin.com/simplify.jobs used to retrieve any job description?
- Print `manual_link_rescues`: count of content_failed jobs manually verified and re-admitted.
- If network/policy restrictions affect validation (`filter_valid_job_links` fetch_none/content fallback), print:
  - `validation_network_constraint`: true/false
  - `validation_fallback_mode`: `none | link_only_network | link_only_content`
  - `validation_note`: one short sentence with recommended next step.
