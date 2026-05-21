---
name: fetchjobs-pro
description: Pro-tier /fetchjobs variant — Sonnet 4.6, fetched descriptions externalized to disk, scoring offloaded to a Scoring Subagent. Invoked by the dispatcher at the top of fetchjobs.md when plan_tier=="pro".
model: claude-sonnet-4-6
---

# /fetchjobs-pro (Pro-tier sharded variant)

You MUST NOT fail silently.

**Purpose:** fit `/fetchjobs` within a Claude Pro 5-hour rate-limit window without quality degradation. The architectural deltas vs Max:

1. **Model:** Sonnet 4.6 (set via this file's `model:` frontmatter).
2. **Description externalization:** every fetched job description is written to `data/_descriptions/{idx}.txt` and the main agent keeps only `{company, title, link, snippet ≤200 chars, description_path}` in chat. Full text never enters main context.
3. **Scoring Subagent:** a dedicated subagent reads the candidate list + description files in isolation, applies the full moat-seeker scoring logic (Step 3 of `fetchjobs.md`), and writes `data/_fetchjobs_scored.json`. Main agent receives only the top-3 summary.
4. **Compressed display:** prints the top-5 + counts inline; full table available via Streamlit.
5. **3 discovery waves** instead of 5 (more aggressive batching).

Everything else — exclusion rules, lifecycle dedup, learned-pattern soft bias, Persistence Agent contract, wisdom format, Step 9.f token backfill — is **identical to `.claude/commands/fetchjobs.md`** and referenced by name below. Do not re-implement those; read them from the Max file when in doubt.

---

## CRITICAL: Never use subagents for WebSearch

Same rule as Max. All `WebSearch` and `WebFetch` calls must be in the main agent turn. Parallelize within a single message — never via Agent. The Scoring Subagent and Persistence Agent are Python-only (no web tools).

---

## Status block (print at start)

- `fetchjobs_start`: ISO timestamp
- `tier`: `pro` (sourced from `data/candidate_info.json` `plan_tier`)
- `variant`: `fetchjobs-pro` · `model`: `sonnet-4-6`
- `active_profile_path`: `data/candidate_info.json`
- `profile_json_valid`: true/false
- `profile_keys_present`: list of keys
- `resume_pdf_candidate`: chosen `data/*resume*.pdf` or `NONE`
- `nudge_context_high_signal_count`: from `scripts/get_nudge_context.py`
- `description_externalization`: `true` (always — distinguishes Pro from Max in diagnostics)
- `scoring_subagent_used`: `true` (always)
- `plan`: one-line summary

## Prerequisite gating

Same as Max (`fetchjobs.md` lines ~25–28): missing/invalid `candidate_info.json` → tell user to run `/setup`; missing `data/*resume*.pdf` → tell user to add one. Exit cleanly.

---

## Step 0 — Clear scratch directory

Before Wave 1, clear `data/_descriptions/` and `data/_fetchjobs_candidates.json` so we never score stale descriptions from a prior run:

```bash
rm -rf data/_descriptions && mkdir -p data/_descriptions
rm -f data/_fetchjobs_candidates.json
```

---

## Step 1 — Context ingestion (3 parallel tool calls in one message)

Identical to Max Step 1: load profile JSON, parse resume PDF, run `uv run python scripts/get_nudge_context.py`. Apply the same exclusion-list rules (`excluded_companies`, `excluded_areas`, `excluded_pairs`) and the same pattern-fields semantics (`inclinations`, `disinclinations`, `learn_skills`).

See `.claude/commands/fetchjobs.md` Step 1 for the full rules — they apply verbatim. **Read the file once if you need the details rather than reasoning from memory.**

---

## Step 2 — Discovery (3 waves, with description externalization)

### Wave 1 — Search (single parallel message, 8–10 WebSearch calls)

Build queries per `fetchjobs.md` Step 2's query-building rules (site-targets, golden_keywords ∩ scientific_moat ∩ engineering_stack ∩ priority_domains, recency tokens, target_country, peer_companies organic search). Fire all in one message.

### Wave 2 — Fetch + externalize (single parallel message, 6–8 WebFetch calls)

For each promising candidate from Wave 1:

1. Convert all `jobs.lever.co/{co}/{id}` URLs to `https://api.lever.co/v0/postings/{co}/{id}?mode=json` before fetching (Lever API rule from Max).
1b. **Pre-canonicalize Greenhouse URLs:** rewrite any `boards.greenhouse.io/*/jobs/*` URL to `job-boards.greenhouse.io/*/jobs/*` before fetching — eliminates a 301 redirect per Greenhouse URL (10 redirect round-trips wasted this run).
2. Fire all WebFetch calls in parallel.
3. **CRITICAL — externalize:** after the parallel batch returns, for each fetched result:
   - If the response is dead (404, NotFound, Gone, `?error=true` redirect, CSS-only, empty body): drop and log.
   - Otherwise, extract the description text and write it to a numbered file: `data/_descriptions/{idx:03d}.txt` where `{idx}` is a zero-padded counter starting at 001.
   - Extract `posted_at` from Lever API `createdAt` when present (epoch ms is fine — the persistence layer normalizes).
4. Build an in-memory list of candidate dicts with these keys ONLY:
   - `idx`: int
   - `company`: str
   - `title`: str
   - `link`: str (canonical human-readable URL — for Lever, the original `jobs.lever.co/...` form)
   - `snippet`: str (≤200 chars — the first 200 chars of the description, for context-free triage)
   - `description_path`: str (e.g. `data/_descriptions/001.txt`)
   - `posted_at`: int|str|null (passed through; persistence normalizes)
   - `source_wave`: 2

**Do NOT print the full descriptions back to chat.** Only print a one-line summary: `wave2_fetched: <int>, wave2_dead: <int>, wave2_externalized: <int>`.

### Wave 3 — Backfill *(archived: see .claude/_archive/fetchjobs-pro__wave-3-backfill.md; restore via /improve --restore COMPACT-2026-05-20-E)*

Single parallel message: ATS backfill for LinkedIn hits + aggregator fallback for Ashby/Workday CSS-empty candidates + direct WebFetch of new ATS URLs. Externalize each fetched description as in Wave 2; append with `source_wave: 3`.

### Hard MCP discovery requirements (must print before scoring)

```
mcp_websearch_calls: <int>     (minimum 8 across all waves)
mcp_webfetch_calls: <int>     (minimum 5)
mcp_candidates_extracted: <int>
mcp_blocked_or_failed_calls: <int>
linkedin_discovered: <int>
linkedin_with_ats: <int>
linkedin_fallback_only: <int>
linkedin_dropped_reason_counts: {...}
description_externalization: true
descriptions_written: <int>
```

If discovery genuinely failed (0 successful fetches across all waves), print `MCP_DISCOVERY_FAILED: <reason>` and skip to Step 5 with an empty candidate list. Do not pretend.

---

## Step 3 — Write candidates JSON, dispatch Scoring Subagent

Write the full candidate list to `data/_fetchjobs_candidates.json` as a JSON array.

Then spawn the **Scoring Subagent** in the background. Its prompt is below — copy it verbatim into the `Agent` tool call (subagent_type: `general-purpose`, run_in_background: `true`).

### Scoring Subagent prompt (verbatim)

```
You are the Scoring Subagent for /fetchjobs-pro. You ONLY use Read/Write/Bash — no WebSearch/WebFetch. Work in the project root (the current working directory inherited from the orchestrator).

INPUT FILES:
- data/_fetchjobs_candidates.json — array of {idx, company, title, link, snippet, description_path, posted_at, source_wave}
- data/_descriptions/*.txt — one file per candidate, full job description text
- data/candidate_info.json — profile (read these keys ONLY: scientific_moat, engineering_stack, core_identity, priority_domains, noise_keywords, excluded_companies, excluded_areas, excluded_pairs, inclinations, disinclinations, learn_skills). Do NOT read peer_companies — that's a discovery-only field, not used in scoring.

YOUR JOB: apply the moat-seeker scoring logic exactly as specified in .claude/commands/fetchjobs.md Step 3 (read that file once). Write the scored output to data/_fetchjobs_scored.json with these keys per row: {company, title, link, score, theme, rationale, description_path, posted_at?}.

`theme` definition: a short 2–6 word descriptive phrase capturing the role's core focus, generated by YOU from the JD (e.g. "Bayesian Experimentation & Causal Inference", "AV V&V Safety Validation", "Demand Forecasting & Pricing Science"). Used for the recency-grouped display AND for matching `excluded_areas` substrings — so keep it lexically aligned with the JD's actual subject area.

SCORING RULES (summary — see fetchjobs.md Step 3 for the full text, including all caps and bonuses):
1. HARD FILTER (drop before scoring, do not generate rationale):
   - Any noise_keyword case-insensitively in title OR description (Junior, Intern, Web Developer, Front End, Marketing Analyst, Business Intelligence, Entry Level, Contract, Sales Engineer, etc.)
   - excluded_companies (case-insensitive exact match on company)
   - excluded_areas (any entry is case-insensitive substring of theme)
   - excluded_pairs (company:area both match; split on FIRST colon)
2. SCORE BANDS:
   - 90–100: description explicitly requires ≥3 items from scientific_moat. Bonus if in a priority_domain.
   - 70–89: matches core_identity AND ≥2 items from engineering_stack.
   - Penalty for generic JDs without rigorous evaluation/specialized modeling.
3. SOFT-BIAS PATTERNS (NEVER a hard filter):
   - For each inclinations.pattern that is case-insensitive substring of description or rationale: +5 (cap aggregate +15). Scale by confidence: HIGH ×1.0, MED ×0.7, LOW ×0.4.
   - For each disinclinations.pattern matched: -5 (cap -15). Same scaling.
   - For each learn_skills.skill in description AND NOT in engineering_stack: +3 (cap +9). Same scaling.
   - Note matched patterns in rationale (e.g. "+10 inclinations: Series-B clean energy, probabilistic forecasting").
4. LIFECYCLE DEDUP: query the live DB for {link: status} where status IN ('Applied','InProgress','Closed','Won','NotForMe'). If candidate.link is in that set, DROP from this run (do not persist, do not score). Log dropped_terminal_status.
5. VESTING/FUNDING: bonus 1.2× for "Series C/D", "IPO-bound", "NIST Grant", "DOE funding". Negative for generic "Agency"/"Consultancy" unless explicitly PhD/scientific.
6. SCORING CONFIDENCE CAP: if description file is empty or only contained CSS-fallback hint AND no aggregator text was captured, cap score at 79 and note "description_not_fetched: true" in rationale.
7. RATIONALE: must explain HOW the candidate's scientific_moat solves a specific problem mentioned in the JD. Cite specific JD phrases. Mention any soft-bias patterns matched.

OUTPUT FILE FORMAT (data/_fetchjobs_scored.json):
[
  {
    "company": "...",
    "title": "...",
    "link": "...",
    "score": <int>,
    "theme": "...",
    "rationale": "...",
    "description_path": "data/_descriptions/001.txt",
    "posted_at": <epoch_ms or null>
  },
  ...
]

Use the same `description_path` you already have from the input candidate (e.g. `data/_descriptions/{idx:03d}.txt`). Do NOT read and re-embed the file — pass the path string only. The Persistence Agent hydrates it.

Only include rows where score >= 70 AND not noise-filtered AND not exclusion-matched AND not lifecycle-deduped.

PRINT TO CHAT:
- scoring_subagent_done
- candidates_input: <int>
- noise_dropped: <int>
- excluded_dropped: <int>
- lifecycle_deduped: <int>
- score_distribution: {"90-100": <int>, "70-89": <int>, "below-70": <int>}
- top_3: [{company, title, score}]

Keep printed output under 30 lines.
```

While the Scoring Subagent runs in the background, do nothing — wait for it to return. Then proceed to Step 4.

---

## Step 4 — Persistence Agent

After the Scoring Subagent returns, spawn the **Persistence Agent** exactly as specified in `.claude/commands/fetchjobs.md` Step 5. It reads `data/_fetchjobs_scored.json`, runs `filter_valid_job_links`, persists via `persist_jobs`, does FPR observability, runs two-strike stale-link pruning, emits diagnostics to `data/run_diagnostics.jsonl`.

Use the background-Agent prompt from Max Step 5, **with one Pro-tier addition prepended to the prompt** (insert after the opening role line, before "## Your job"):

```text
## Pro-tier: hydrate descriptions before persist_jobs

Rows in data/_fetchjobs_scored.json carry `description_path` (not `description`). After
`filter_valid_job_links` returns `alive`, hydrate each surviving row from disk before
calling `persist_jobs`:

    import pathlib
    for job in alive:
        if "description_path" in job and "description" not in job:
            job["description"] = pathlib.Path(job.pop("description_path")).read_text()

`filter_valid_job_links` only needs link/title — do not hydrate before it. Read each
file exactly once, right before the persist call.
```

The contract (inputs, outputs, return values) is otherwise identical to Max.

While the Persistence Agent runs in the background, move on to Step 5 (Wisdom).

---

## Step 5 — Wisdom (compressed, no description reload)

After the Scoring Subagent returned the top_3 summary and score distribution, you have enough to write wisdom. **Do NOT read description files again.** Use only the top-3 summary + score distribution + your own discovery diagnostics (waves, dead counts, domain coverage from candidates).

Generate **3–6 short sentences** following the same rubric as Max Step 6:
- Dominant moat-aligned vertical
- Notable miss-patterns (e.g. AV peer-companies returned dead)
- Tooling pattern across top scorers
- Tactical observation about query freshness or ATS productivity
- Calibration line (search counts → candidates → survivors)

If the scored list is empty, write 1–2 sentences explaining the most likely gating cause.

Write wisdom to `data/candidate_info.json` `wisdom` key (preserve every other key).

---

## Step 6 — Wait for Persistence Agent, then final display

Wait for the Persistence Agent's return value (`valid_jobs_count`, `stale_links_pruned`). If `valid_jobs_count` differs from the Scoring Subagent's top_3 implication, append a one-sentence correction to wisdom.

### Compressed final display (the Pro-tier display)

Query the DB the same way Max does (`fetchjobs.md` Step 9.b), but print a **compressed view**:

```
## Found this run (top 5 of N)
| Status | Score | Company | Title | Theme | Posted | Link |
| ...
| ...
(showing top 5 by score — see Streamlit for full N rows)

## Active applications (P jobs)   ← status in {Applied, InProgress, Closed, Won}
| Status | Score | Company | Title | Theme | First seen | Posted | Link |
| ...

## Earlier runs (M jobs visible — see Streamlit for full view)
| (top 3 by first_seen DESC) |
```

Keep total chat output for the display under ~30 lines. The full data lives in the DB; users can browse via Streamlit.

Print final counts (same as Max): `final_table_total`, `final_table_this_run`, `final_table_active_applications`, `final_table_earlier`, `final_table_pruned_this_run`, `final_table_posted_at_coverage`.

---

## Step 7 — Session marker

```bash
uv run python -m job_finder.session_marker
```

Identical to Max Step 9.e.

---

## Step 8 — Token diagnostics backfill (Step 9.f from Max)

Identical to Max Step 9.f — run the audit and patch the just-written diagnostics row. Use the exact Bash snippet from `fetchjobs.md` Step 9.f. Print `token_backfill: input=... output=... cache=... productive=... lost=...` (or `token_backfill_skipped: <reason>`).

---

## Step 9 — Auto-audit /improve (Step 10 from Max)

Identical to Max Step 10 — read `auto_improve_audit_enabled` from `candidate_info.json` and either dispatch `/improve --audit-only` or print the disabled-by-default notice.

---

## Reliability Guardrails (must print every run)

- `fetchjobs_mode`: `mcp_live_search_pro` (distinct from Max's `mcp_live_search`)
- `variant`: `fetchjobs-pro` · `model`: `sonnet-4-6` · `tier`: `pro`
- `discovery_path_used`: `WebSearch, WebFetch`
- `python_scraper_used`: `false`
- `description_externalization`: `true`
- `descriptions_written`: <int>
- `scoring_subagent_used`: `true`
- `scoring_subagent_id`: <agent-id reported by Agent tool>
- `lever_ashby_description_fallback_used`: true/false
- `link_check_skipped`: true/false
- `stale_links_pruned`, `stale_links_quarantined`, `stale_links_ttl_expired`, `pruner_fpr_alert`: same as Max
- `final_table_total`, `final_table_this_run`, `final_table_earlier`: same as Max (the recency display is compressed but the counts remain canonical)
- `token_backfill: input=... output=... cache=... productive=... lost=...` (or `token_backfill_skipped: <reason>`)
- `auto_improve_audit`: `{enabled: bool, proposals_written: int}`

---

## Quality-parity escape hatch

If the user reports wisdom-quality regression vs Max, the fix is to wrap **only Step 5 (Wisdom)** in a one-turn invocation of an Opus subagent (Agent tool with model override — see Agent SDK docs). All other steps stay on Sonnet. This preserves ~90% of the cost savings while restoring Opus-quality wisdom. Do not enable this preemptively — it's an opt-in remediation.
