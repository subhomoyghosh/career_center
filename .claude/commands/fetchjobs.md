# /fetchjobs

You MUST NOT fail silently.

## Variant dispatch (MUST run before any other step)

This file owns the **Full (Max-tier)** Opus flow. The sibling skill `fetchjobs-pro` owns the **Lean (Pro-tier)** Sonnet flow with description externalization + Scoring Subagent. Either variant is available to any user — Pro users normally want Lean (the Full flow exceeds a Pro 5-hour rate-limit window), Max users normally want Full but may want Lean for a quick / cheap run.

Before doing anything else:

1. Read `data/candidate_info.json`. If the file does not exist or is invalid JSON, fall through to the existing prerequisite-gating block below (which will instruct the user to run `/setup`).
2. Inspect the keys `plan_tier` and `runtime_mode_override`:
   - `plan_tier` ∈ {`"pro"`, `"max5x"`, `"max20x"`} — captured during `/setup` or on first `/fetchjobs`. Drives the *recommended default* for variant selection.
   - `runtime_mode_override` ∈ {`"lean"`, `"full"`, `""` or absent} — optional escape valve. If set to `"lean"` or `"full"`, the dispatcher skips the per-run question and uses that mode every time. Useful for users who never want to be asked.
3. **If `plan_tier` is missing or empty:** ask via `AskUserQuestion` ("Which Claude Code plan are you on?", options: `Pro ($20/mo)` → `"pro"`, `Max 5x ($100/mo)` → `"max5x"`, `Max 20x ($200/mo)` → `"max20x"`). Save the lowercase key into `candidate_info.json` (read existing JSON, mutate only `plan_tier`, preserve every other key, write back).
4. **Determine the recommended default mode** from `plan_tier`:
   - `"pro"` → `lean` (Full would burn through Pro's 5-hour window)
   - `"max5x"` or `"max20x"` (or anything else) → `full`
5. **Choose this run's mode:**
   - If `runtime_mode_override` is `"lean"` or `"full"`: use it directly (no prompt). Print `variant_dispatch: <mode> (override; plan_tier=<X>)`.
   - Otherwise, ask via `AskUserQuestion`: "Which /fetchjobs variant for this run?". Two options, **listed in recommended-default order for the user's tier**:
     - `Lean (Sonnet 4.6, ~500K tokens, ~10 turns, fits Pro window)` — recommended for Pro
     - `Full (Opus 4.7, ~20M tokens, ~150 turns, needs Max budget)` — recommended for Max
6. **If chosen mode is `lean`:** invoke the `fetchjobs-pro` skill via the `Skill` tool and **STOP**. Print `variant_dispatch: lean (chosen; plan_tier=<X>)` before stopping. `fetchjobs-pro` owns the full run.
7. **If chosen mode is `full`:** print `variant_dispatch: full (chosen; plan_tier=<X>)` and continue with the Max-tier flow below — architecturally unchanged.

**Note for users:** you can also invoke the variants directly without going through this dispatcher — `/fetchjobs-pro` always runs Lean for anyone. The dispatcher exists to make the choice explicit on every `/fetchjobs` invocation.

---

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
   - Load the active profile JSON (`candidate_info.json`) and use **all** of: `core_identity`, `scientific_moat`, `engineering_stack`, `target_seniority`, `target_country`, `golden_keywords`, `noise_keywords`, `priority_domains`, `search_targets`, `wisdom`, `inclinations`, `disinclinations`, `learn_skills`, `excluded_companies`, `excluded_areas`, `excluded_pairs`, `allowed_metros`, `allowed_work_modes`, `remote_anywhere_ok`. Optionally `priority_industries` (alias for priority_domains), `peer_companies`.
   - **Hard filters (applied BEFORE scoring/rationale)** — canonical rules in `.claude/_scoring_rules.md §1`. Apply ALL of them in Step 3 (BEFORE the moat scoring loop) and again in the handoff to the Persistence Agent in Step 4:
     - **`excluded_*` lists** — (a) `company` case-insensitive exact in `excluded_companies`, OR (b) any entry in `excluded_areas` case-insensitive substring of `theme`, OR (c) any `company:area` in `excluded_pairs` matches both sides (split on FIRST colon; empty entries silently ignored).
     - **Location + work-mode (§1e)** — parse `mode` and `location` from the JD's location/work-mode header (NOT arbitrary mentions). Drop when `allowed_work_modes` non-empty AND mode ∉ allowed_work_modes, OR when `allowed_metros` non-empty AND location not in any allowed region (use geographic knowledge — e.g. "Mountain View, CA" ∈ "San Francisco Bay Area, CA"). Remote + `remote_anywhere_ok=true` bypasses the metro check. Unknown mode/location → keep.
     - Precedence: hard filters ALWAYS win over `peer_companies` — do not include excluded companies or out-of-metro companies in peer-companies organic search queries.
   - **Pattern fields** (`inclinations` / `disinclinations` / `learn_skills`) come from the LLM-driven synthesizer in `/improve` after human approval. Each entry has `{pattern | skill, evidence_job_ids | source_jobs, confidence: HIGH|MED|LOW, source_axis, added_at}`. Use them in scoring (Step 3) — NEVER as hard filters.
   - **User feedback & weights (nudge):** Run `uv run python scripts/get_nudge_context.py` and use its JSON output (or call `get_high_signal_jobs()` from `job_finder.persistence`). Use the listed jobs as **positive signals**: bias search queries and scoring toward similar roles (company, title, theme, rationale); mention these patterns in the wisdom synthesis. Apply stronger nudge for higher `user_weight` (e.g. 100 = very strong signal).
   - **Nudge amplification (mandatory when high_signal_jobs is non-empty):** For each job with `user_weight ≥ 70`, extract its `theme` sector and add **at least one dedicated Wave 1 query** targeting that sector: e.g., if "Demand Forecasting & Pricing Science" is high-signal, add `"pricing science" "data scientist" site:lever.co OR site:job-boards.greenhouse.io`. This ensures the nudge directly shapes discovery, not just scoring. Log `nudge_amplified_queries: <count>` in the status block.
   - Parse the resume PDF: list `data/` (e.g. `ls data/`) and pick any `.pdf` whose filename contains "resume" (case-insensitive). Use that file to extract Top 3 Achievements and enrich rationale (no hard-coded company names). (PDFs may be gitignored — do not rely on glob alone.)
   - **`peer_companies` organic search:** If `peer_companies` is non-empty, run additional discovery queries such as `"[peer] competitor" "Applied Scientist" site:lever.co` or `"people also hired from [peer]" "Data Scientist"` to find adjacent companies hiring from similar talent pools. If `peer_companies` is empty, suggest the user populate it with 2–3 prior employers or known peer companies so future runs can exploit this signal.

2. **Vectorized Search (Active Roles Only) — use every field** *(Discovery Team — fire in 3 waves, each wave is one parallel message)*:

   **Before Wave 1 — Zero-result avoidance (1 Bash call):** Load the last 2 runs' `zero_result_query_strings` from `data/run_diagnostics.jsonl`:
   ```bash
   uv run python -c "import json,pathlib; lines=[json.loads(l) for l in pathlib.Path('data/run_diagnostics.jsonl').read_text().splitlines() if l.strip() and 'zero_result_query_strings' in l]; recent=[q for r in lines[-2:] for q in r.get('zero_result_query_strings',[])]; print(json.dumps(recent))"
   ```
   For any Wave 1 query you planned that is lexically identical to a recent zero-result query, **replace it with a broader fallback** (drop one domain-specific term, or widen to 2-keyword + target_country). Log `zero_result_avoided: <count>` in diagnostics. Do not skip the query entirely — broaden it.

   **Wave 1 — Search Wave:** Fire all primary `WebSearch` queries simultaneously in a single message (target 8–10 calls). Build queries per the rules below, then dispatch all at once.

   **Pre-Wave-2 URL Filter (board-page early-exit):** Before fetching candidates, filter out URLs matching board-page patterns: path ends in `/jobs`, `/openings`, `/careers`, `/positions` with no job-ID segment; or query-only params like `?q=`, `?keyword=`, `?department=`, `?location=`, `?error=true`; or URL contains aggregator index pages (e.g. `/search`, `/browse`, `/listings` without a job ID). Log filtered count as `board_page_urls_skipped_pre_fetch`. This prevents wasted fetches and improves Wave 2 efficiency.

   **Wave 2 — Fetch Wave:** After Wave 1 returns, fire all `WebFetch` calls for surviving candidates simultaneously in a single message (target 6–8 calls). Includes aggregator fallback fetches for JS-rendered ATS pages.

   **Wave 3 — Backfill Wave:** After Wave 2 returns, fire any remaining secondary `WebSearch` queries (ATS backfill for LinkedIn, aggregator searches for 403 Lever/Ashby pages) + any follow-up `WebFetch` calls simultaneously in a single message. **Include domain blind spot recovery:** tally which `priority_domains` have 0 discovered candidates after Wave 2. For each zero-coverage domain, add one broad targeted query to this wave: `"Data Scientist" OR "Applied Scientist" "[domain keyword]" site:lever.co OR site:job-boards.greenhouse.io` (e.g. "autonomous systems", "renewable energy", "climate"). Log `blind_spot_recovery_queries: [<domain>, ...]`.

   - **Site targets:** If `search_targets` is present, run **one or more queries per target** using `site:[domain]`. Always include both `jobs.lever.co` and `lever.co`, and both `job-boards.greenhouse.io` and `boards.greenhouse.io` if either appears in `search_targets`.
   - **Workday path (dedicated):** If `search_targets` includes `workday.com` or `myworkdayjobs.com`, run queries against **`site:myworkdayjobs.com`** (not `site:workday.com` — the latter rarely surfaces individual listings). Example: `site:myworkdayjobs.com "Applied Scientist" ("Bayesian" OR "Causal Inference") USA`. Also try `"[company] careers" "Applied Scientist" workday` for companies known to use Workday.
   - **LinkedIn (dedicated path):** If `search_targets` includes **`linkedin.com/jobs`**, run `site:linkedin.com/jobs` searches. **However:** LinkedIn via `site:` often returns stale aggregation pages rather than individual listings. Treat LinkedIn as a **discovery hint layer only** — extract company+title from any hits, then immediately run a secondary ATS backfill search (e.g. `"[company]" "[title]" site:lever.co OR site:greenhouse.io OR site:ashbyhq.com`) to find the direct ATS URL. Persist the ATS link, not the LinkedIn link, whenever possible.
   - **ATS backfill for LinkedIn hits:** If LinkedIn fetch is blocked/login-walled, do not stop: run company+title ATS search and continue. Keep LinkedIn URL only as a last resort when no ATS link is found.
   - **Query building:** Combine (1) **golden_keywords** (roles + methods), (2) **scientific_moat** terms, (3) **engineering_stack** terms, (4) **priority_domains** so each query is specific. Use `target_country` and recency tokens ("2026", "Hiring Now") where appropriate. Run queries at two levels of specificity: a tight version (3+ keywords) and a relaxed version (2 keywords + domain) to avoid zero-result dead ends. **Multi-site queries must use OR syntax:** `(site:lever.co OR site:greenhouse.io) "Applied Scientist"` — never `site:lever.co/greenhouse.io` (Google treats the slash as a URL path, not two sites, producing zero results).
   - **Location/work-mode hints (token efficiency — added before Wave 1 dispatch):** If `allowed_metros` is non-empty, append a permissive location clause to **broad/moat-aligned queries** so search engines pre-filter out roles you'd reject in §1e. Construction:
     - Build the clause as `(<metro 1> OR <metro 2> OR ... [OR "Remote"])`. Use the metro's most-recognizable short form (e.g. `"San Francisco" OR "Bay Area"` for `"San Francisco Bay Area, CA"`). Include `"Remote"` in the OR list iff `remote_anywhere_ok` is true.
     - Example: `"Applied Scientist" ("Bayesian" OR "Causal Inference") ("San Francisco" OR "Bay Area" OR "Remote") site:lever.co`.
     - **Apply ONLY to broad queries.** Skip the clause for: peer_companies organic-search queries (location is the peer's HQ; the clause would over-filter); domain blind-spot recovery queries (already narrow); nudge-amplification queries targeting a specific theme (you want the broadest possible discovery). One-line rule of thumb: *if the query already names a specific company, skip the location clause.*
     - If `allowed_metros` is empty AND `allowed_work_modes ⊊ {remote, hybrid, onsite}`, still append `("Remote")` or analogous to broad queries to honor the work-mode constraint.
     - Log `location_filtered_queries: <count>` in the status block — number of Wave 1 queries that got the clause appended.
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
   - **Greenhouse pre-canonicalization:** Before fetching any `boards.greenhouse.io` URL, rewrite it to `job-boards.greenhouse.io` — the old subdomain 301-redirects every request, wasting a round-trip per Greenhouse URL.

   - **Posted-date extraction (best-effort, optional but valued):** For each candidate, attempt to capture an absolute posting timestamp into a `posted_at` field on the job dict. The persistence layer normalizes epoch ms / epoch seconds / ISO strings; non-parseable strings are stored verbatim, and missing values are stored as NULL — so do not fabricate. Sources by reliability:
     - **Lever API**: `createdAt` (epoch ms) — always reliable when the API call succeeds. Extract this every time.
     - **Greenhouse**: HTML rarely exposes a date; skip unless the page surfaces an explicit `updated_at` or "Posted on YYYY-MM-DD".
     - **Workday**: pages often render relative phrases ("Posted 5 days ago"). If parseable, convert to an ISO date by subtracting from the current run date; if only `Posted Today` / `Posted Yesterday`, that's still useful. Otherwise leave NULL.
     - **Ashby**: typically CSS-only via WebFetch — skip; rely on aggregator fallback.
     - **Aggregators (builtin.com, simplify.jobs, levels.fyi)**: often surface an absolute "Posted on …" line. Capture it if visible.
     - **LinkedIn**: relative dates only and frequently stale — do not trust LinkedIn for `posted_at`; leave NULL.

     If you can't get a reliable absolute date, omit the field entirely. NULL is a fine and expected outcome — the recency view in Step 9 falls back to `first_seen` which is always populated.
   - **Ashby pages are JS-rendered:** `WebFetch` on `ashbyhq.com` job URLs typically returns CSS only. Run aggregator fallback: `"[company]" "[title]" site:builtin.com OR site:simplify.jobs OR site:levels.fyi`. Log fallbacks in the diagnostics block.
   - **Verify individual listings, not board pages:** Before adding a candidate to your list, confirm the URL resolves to a single job posting (not a board index). Board-page signals — URL level (check before fetching): URL path ends in `/jobs`, `/openings`, `/careers`, or `/positions` with no job-ID segment; or URL has only `?q=`, `?keyword=`, `?department=`, `?location=`, or `?error=true` query params; or final URL after redirect no longer contains the original job-ID path. Body level (check after fetching): page lists 10+ distinct job titles without a company-specific description block; or `filter_valid_job_links` detects a title-not-found miss. If you detect a board page at either level, drop it, do NOT persist that URL, and log it toward `board_page_hits` in diagnostics (count URLs dropped as board pages — not total candidates). Run a more specific search instead (e.g. add job ID or title to query).
   - Print a short LinkedIn summary before validation: `linkedin_discovered`, `linkedin_with_ats`, `linkedin_fallback_only`, `linkedin_dropped_reason_counts`.

3. **Scientific Moat Evaluation (Moat-Seeker Logic):**

   **Read `.claude/_scoring_rules.md` for the full scoring rules before scoring.** That file is the canonical source. Summary of what it specifies:
   - Hard filters (§1): noise_keywords, excluded_companies, excluded_areas, excluded_pairs, location+work-mode constraints (allowed_metros/allowed_work_modes/remote_anywhere_ok) — drop before scoring, log `excluded_dropped`
   - Score bands (§2): 90–100 = ≥3 scientific_moat items; 70–89 = core_identity + ≥2 engineering_stack or ≥1 scientific_moat
   - Soft-bias patterns (§3): inclinations +5, disinclinations -5, learn_skills +3; scaled by confidence; never hard filters
   - Lifecycle dedup (§4): drop links already in terminal statuses (Applied/InProgress/Closed/Won/NotForMe)
   - Vesting bonus (§5): 1.2× for Series C/D, IPO-bound, NIST/DOE grants
   - Confidence cap (§6): cap at 79 if description unavailable
   - Rationale contract (§7): cite JD phrases verbatim, explain moat→problem fit, note soft-bias matches

4. **Scoring Handoff** *(main agent — no WebFetch calls)*:

   Live-link validation is **delegated to the Persistence Agent** via `job_finder.link_validation.filter_valid_job_links`, which catches more dead-job patterns than ad-hoc WebFetch (e.g. Greenhouse silent-redirects to `/{org}?error=true`, ATS board pages without the original job ID, bot interstitials, sub-MIN_BODY_CHARS empty renders). Keeping it in Python also avoids pulling 13× WebFetch payloads into the main-agent context.

   - **Only forward candidates that passed scoring** (score ≥ 70 AND not filtered by `noise_keywords` AND not matched by any `excluded_*` rule AND not dropped by the location/work-mode rule §1e).
   - **Lever URL note:** still convert `jobs.lever.co/{company}/{jobId}` → `https://api.lever.co/v0/postings/{company}/{jobId}?mode=json` **during Wave 2 description fetching** (Step 2) so you can capture `createdAt` → `posted_at`. The final `link` written to the DB should remain the human-readable `jobs.lever.co` URL; `filter_valid_job_links` HEAD-checks that URL fine.
   - **Include the fetched description text** as a `description` field on each scored job. The Persistence Agent will store it for future LLM-driven feedback analysis. If a description is genuinely unavailable (Ashby CSS-only fallback), pass empty string — do not fabricate.
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
   - **Pruner false-positive observability (run BEFORE any pruning in this run):**
     - `sample = sample_pruned_links_for_fpr_check()` — random 10 prior-pruned links.
     - HEAD-check each via `requests.head(link, allow_redirects=True, timeout=5)`. Count how many return 200 with non-trivial content.
     - If `>5%` come back alive: print `pruner_fpr_alert: true` with the resurrected links AND skip new pruning this run (only the alive/quarantine bookkeeping runs). Add `pruner_fpr_alert: <bool>` to diagnostics.

   - **Stale-link pruning — two-strike protocol (run after `persist_jobs`):**
     - **Construct `run_id`** at the start of pruning: `run_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or _now_iso()` — same session id the marker uses, so `pruned_history.jsonl` rows can be joined back to the audit's session transcript.
     - Load all existing jobs via direct SQLite, including `failed_validation_count` and `last_validated_at`.
     - Exclude any job whose `link` is in the current run's `alive` list — those were just live-checked, so call `mark_validation_success(conn, link)` on each of them (resets counter to 0, updates timestamp).
     - For pre-existing rows not in `alive`, re-run `filter_valid_job_links(pre, require_title_in_body=True, check_content=True)`.
     - For each FAILING row:
       - Call `mark_validation_failure(conn, link, fail_reasons)` — increments counter, returns new count.
       - If `new_count >= 2`: call `delete_and_log_pruned(conn, link, company, title, fail_reasons, first_failed_at, run_id)`. This deletes the row AND appends to `data/pruned_history.jsonl`.
       - If `new_count == 1`: row is now in quarantine — update `status='quarantine'` so the recency table can mark it ⚠. **Do not delete on first failure.**
     - Call `force_delete_expired_quarantine(conn, ttl_days=30)` — clears zombies older than 30 days.
     - Print: `stale_links_checked`, `stale_links_quarantined`, `stale_links_pruned`, `stale_links_ttl_expired`.

     Example imports:

     ```python
     from job_finder.persistence import (
         mark_validation_success, mark_validation_failure,
         delete_and_log_pruned, force_delete_expired_quarantine,
         sample_pruned_links_for_fpr_check,
     )
     ```

     **Token preservation:** All pruning logic runs in Python — only the summary counts are returned to the orchestrator.
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

   Note: `stale_links_pruned` = rows DELETED this run (second-strike + ttl-expired combined). `stale_links_quarantined` = rows that hit their FIRST validation failure this run and were flagged but kept. `stale_links_ttl_expired` = rows force-deleted because they sat in quarantine longer than `ttl_days` (30 default). `pruner_fpr_alert` = true if our FPR sample showed >5% of prior-pruned links came back alive (pruning is skipped for this run when true).

   Include these three fields in every diagnostic line (zero/false when the underlying check did not run):
   - `pruner_fpr_alert`: result of `compute_pruner_fpr_alert()` from `src.job_finder.link_validation` — reads `data/fpr_recheck_latest.json`, which the Persistence Agent writes after the FPR HEAD-check step (schema: `{"sample_size": int, "resurrected": [{"link": str, ...}]}`). Use the `pruner_fpr_alert` bool from the returned dict.
   - `stale_links_quarantined`: result of `count_stale_links_quarantined()` from `src.job_finder.link_validation` — `SELECT COUNT(*) FROM jobs WHERE status='quarantine'`.
   - `stale_links_ttl_expired`: result of `count_stale_links_ttl_expired()` from `src.job_finder.link_validation` — count of rows whose `last_validated_at` is older than `ttl_days` (60 default).

   **MANDATORY — compute the three fields explicitly before building the diagnostics dict.** The Persistence Agent must run this block (or its exact equivalent) AFTER the stale-link pruning step completes and BEFORE the `json.dumps` write. Defaults (`false`/`0`) must be emitted on EVERY run even if a check was skipped or its inputs were missing — never omit the keys.

   ```python
   from job_finder.link_validation import (
       compute_pruner_fpr_alert, count_stale_links_quarantined, count_stale_links_ttl_expired,
   )
   pruner_fpr_alert, stale_links_quarantined, stale_links_ttl_expired = False, 0, 0
   try: pruner_fpr_alert = bool(compute_pruner_fpr_alert().get("pruner_fpr_alert", False))
   except Exception as e: print(f"diagnostics_warn: pruner_fpr_alert ({e!r})")
   try: stale_links_quarantined = int(count_stale_links_quarantined())
   except Exception as e: print(f"diagnostics_warn: stale_links_quarantined ({e!r})")
   try: stale_links_ttl_expired = int(count_stale_links_ttl_expired(ttl_days=60))
   except Exception as e: print(f"diagnostics_warn: stale_links_ttl_expired ({e!r})")
   ```

   Use these three local variables verbatim when assembling the diagnostics dict below — the field names in the JSON MUST be exactly `pruner_fpr_alert`, `stale_links_quarantined`, `stale_links_ttl_expired`.

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
     "stale_links_quarantined": <int>,
     "stale_links_ttl_expired": <int>,
     "pruner_fpr_alert": <bool>,
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
     "search_targets_used": ["<domain>", ...],
     "excluded_dropped": <int>,
     "input_tokens": <int>,
     "output_tokens": <int>,
     "cache_tokens": <int>,
     "productive_tokens": <int>,
     "tokens_lost": <int>
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

   **c. Print the table** to chat using markdown, THREE sections with clear headers (lifecycle disentanglement):

   ```text
   ## Found this run (N jobs)
   | Status | Score | Company | Title | Theme | Posted | Link |
   |--------|-------|---------|-------|-------|--------|------|
   ...

   ## Active applications (P jobs)   ← status in {Applied, InProgress, Closed, Won}
   | Status | Score | Company | Title | Theme | First seen | Posted | Link |
   |--------|-------|---------|-------|-------|------------|--------|------|
   ...

   ## Earlier runs (M jobs)
   | Status | Score | Company | Title | Theme | First seen | Posted | Link |
   |--------|-------|---------|-------|-------|------------|--------|------|
   ...
   ```

   Group membership:
   - **Found this run**: `link IN this_run_links` AND `status NOT IN ('Applied','InProgress','Closed','Won','NotForMe','quarantine')` — newly surfaced AND not already user-acted-on.
   - **Active applications**: `status IN ('Applied','InProgress','Closed','Won')`. Sort by status priority (Won → InProgress → Applied → Closed), then `first_seen DESC`. **Always show this section** even if empty — visibility is the point.
   - **Earlier runs**: everything else still alive. Excludes `status = 'NotForMe'` (user-rejected; surface in a tiny tail bucket if non-empty, otherwise omit).

   Formatting rules:
   - **Status**: render as badge — `🟢 Won`, `🔵 InProgress`, `🟡 Applied`, `⚪ Closed`, `⛔ NotForMe`, `⚠ quarantine`, empty for `New`. The badge column comes first so the lifecycle state is the first thing the eye lands on.
   - Truncate Title to 60 chars and Theme to 30 chars.
   - Render Link as `[host](url)`.
   - **Posted**: render `posted_at` as `YYYY-MM-DD` if present, else `—` (em dash). Do NOT omit the column when most rows are null — the dashes are informative.
   - **First seen**: render as `YYYY-MM-DD`; for NULL (pre-migration) print `—`.
   - If a group is empty, print `(none)` under the header — do not omit the section (Active applications is REQUIRED even if 0).

   **d. Print final counts:** `final_table_total`, `final_table_this_run`, `final_table_active_applications` (with breakdown by status), `final_table_earlier`, `final_table_pruned_this_run`, `final_table_posted_at_coverage` (fraction of rows with non-null `posted_at`, e.g. `12/47`).

   **e. Write session marker** (required, last action):

   So the audit script can locate this run's transcript later:

   ```bash
   uv run python -m job_finder.session_marker
   ```

   This writes `data/last_session.json` with the session id + JSONL paths. If detection fails (e.g., env var not present), the marker has `detected: false` and the audit will skip cleanly — never invent paths.

   **Schema reference:** The `jobs` table has `first_seen TEXT` (always stamped on insert by `persist_jobs`) and `posted_at TEXT` (best-effort, may be NULL). Both are ISO 8601 UTC strings, so lexical sort matches chronological sort. `posted_at` is only as accurate as what the agent extracted during discovery — see Step 2's posted-date extraction notes. Pre-migration rows have `first_seen IS NULL`; treat as oldest.

   **f. Backfill token diagnostics** (cosmetic — keeps `run_diagnostics.jsonl` honest):

   The five token fields in the diagnostics row (`input_tokens`, `output_tokens`, `cache_tokens`, `productive_tokens`, `tokens_lost`) cannot be filled by the Persistence Agent at write-time because subagents don't have access to the main agent's per-turn usage block — that data only exists in the session JSONL. Step 9.e wrote the session marker; now run `audit_run_efficiency.py` and patch the just-appended diagnostics row with the real numbers. Must run AFTER step 9.e (the marker must exist).

   ```bash
   uv run python -c "
   import json, pathlib, subprocess
   r = subprocess.run(['uv','run','python','scripts/audit_run_efficiency.py'],
                      capture_output=True, text=True, check=False)
   if r.returncode != 0 or not r.stdout.strip():
       print(f'token_backfill_skipped: audit returncode={r.returncode}'); raise SystemExit(0)
   try:
       audit = json.loads(r.stdout)
   except json.JSONDecodeError as e:
       print(f'token_backfill_skipped: audit output not JSON ({e})'); raise SystemExit(0)
   totals = audit.get('totals', {}) or {}
   waste  = audit.get('waste',  {}) or {}
   tokens_lost = sum(int(b.get('tokens_lost', 0)) for b in waste.values())
   p = pathlib.Path('data/run_diagnostics.jsonl')
   lines = p.read_text().splitlines()
   if not lines:
       print('token_backfill_skipped: diagnostics file empty'); raise SystemExit(0)
   last = json.loads(lines[-1])
   last['input_tokens']      = int(totals.get('input_tokens', 0))
   last['output_tokens']     = int(totals.get('output_tokens', 0))
   last['cache_tokens']      = int(totals.get('cache_read_tokens', 0)) + int(totals.get('cache_creation_tokens', 0))
   last['productive_tokens'] = int(totals.get('productive_tokens', 0))
   last['tokens_lost']       = tokens_lost
   lines[-1] = json.dumps(last)
   p.write_text('\n'.join(lines) + '\n')
   print(f\"token_backfill: input={last['input_tokens']:,} output={last['output_tokens']:,} cache={last['cache_tokens']:,} productive={last['productive_tokens']:,} lost={last['tokens_lost']:,}\")
   "
   ```

   **Honest caveat:** the backfill itself runs DURING the main agent's final turn, so its own tokens are NOT yet in the session JSONL when the audit reads it. Treat the patched numbers as a tight lower bound — every prior turn is fully accounted, only this final backfill turn is slightly under-counted. This is fine for grep/awk inspection of `run_diagnostics.jsonl`; consumers that need exact numbers (e.g. `/improve`) re-run the audit themselves and don't rely on this row.

### 10. Auto-audit /improve (optional)

Read `auto_improve_enabled` and `auto_improve_audit_enabled` from `data/candidate_info.json`.

- If `auto_improve_enabled` is **true**: dispatch `/improve --auto`. This applies Tier 1–4 compaction automatically (with regression safety net) and stages PATTERN_*/SCORING_DRIFT_ proposals for human review. Print: `/improve --auto: applied, staged — see Streamlit → Analytics.`
- Else if `auto_improve_audit_enabled` is **true**: dispatch `/improve --audit-only`. Print: `/improve audit: <N> proposal(s) staged — review in Streamlit → Analytics → Pending Improvements.`
- If both are false/absent: print: `/improve disabled. Set auto_improve_enabled: true in candidate_info.json to auto-compact each run.`

**Safety:** `--auto` never touches PATTERN_* or SCORING_DRIFT_ proposals — those still require human approval in Streamlit. Tier 1–4 compaction auto-applies and auto-reverts on regression.

---

## Reliability Guardrails (must print every run)
- Print `fetchjobs_mode`: `mcp_live_search`.
- Print `discovery_path_used`: list of tools actually used (e.g., `WebSearch, WebFetch`).
- Print `python_scraper_used`: always `false` for discovery in this rule.
- Print `lever_ashby_description_fallback_used`: true/false — was builtin.com/simplify.jobs used to retrieve any job description?
- Print `link_check_skipped`: true/false — was Step 4 existence check bypassed due to rate-limiting?
- Print `stale_links_pruned`: count of pre-existing DB jobs deleted as dead this run (second-strike + TTL combined).
- Print `stale_links_quarantined`: count of pre-existing DB jobs that hit first-strike failure this run (kept, status='quarantine').
- Print `stale_links_ttl_expired`: count of quarantined rows force-deleted because they aged past `ttl_days` (default 30).
- Print `pruner_fpr_alert`: true if >5% of sampled prior-pruned links came back alive this run (pruning skipped when true).
- Print `final_table_total`, `final_table_this_run`, `final_table_earlier`: counts from the Step 9 recency-grouped display. The recency table must be printed every run, even if `final_table_this_run == 0`.
- Print `token_backfill`: one line `input=<int> output=<int> cache=<int> productive=<int> lost=<int>` from Step 9.f. If audit failed, print `token_backfill_skipped: <reason>` instead — never silently omit.
- Print `auto_improve_audit`: `{enabled: bool, proposals_written: int}` — present when Step 10 ran.
