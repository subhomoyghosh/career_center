# Career Command Center — Walkthrough

**Listen first:** [Career Command Center Overview](./assets/command_center_voice_over.mp3) (2 min audio introduction)

---

## The Idea

This is a job-search system designed for people who want to run sophisticated searches without burning through tokens or getting overwhelmed by noise.

**The problem it solves:** Job boards (LinkedIn, Indeed, Wellfound, etc.) are noisy. A raw search for "machine learning" returns thousands of listings—most irrelevant to your background, goals, or constraints. Manually filtering is slow and error-prone. Naive AI automation is expensive and misses context-dependent nuance (e.g., "ML roles at startups" vs. "ML roles at scale").

**The solution:** An autonomous agent that:
1. **Discovers** deeply—uses WebSearch + multi-board discovery to cast a wider net than LinkedIn alone
2. **Scores** accurately—understands your background, moat, and target trajectory; explains *why* a role is a match (or not)
3. **Remembers**—learns from your feedback loop; next search biases toward roles you've marked `Good`/`Applied` and away from patterns you've marked `NotForMe`/`Closed`
4. **Stays lean**—for pro-tier Claude users, the whole run fits in a 5-hour window and costs ~500K tokens; for Max users, the system scales to richer scoring at ~20M tokens per run
5. **Self-improves**—audits token efficiency after each run and auto-applies cost-reducing improvements without losing signal quality

---

## How it works: three layers

### Layer 1: Orchestration (`/fetchjobs`)

You run `/fetchjobs` in chat. The system:

1. **Reads your profile** — pulls your resume, moat, stack, seniority level, target domains, and constraints (exclusions)
2. **Dispatches a variant** — chooses Lean (Sonnet 4.6, ~500K tokens, fits Pro window) or Full (Opus 4.7, ~20M tokens, needs Max budget) based on your plan tier
3. **Discovers jobs** — WebSearch waves across multiple boards (LinkedIn, AngelList, job boards, company career pages); externalizes descriptions to disk to keep the main agent lean
4. **Scores each role** — in-context (Full) or via background Scoring Subagent (Lean); explains match confidence and reasoning
5. **Validates links** — checks HTTP status + content quality before persisting; transient failures (403, 429, 5xx) are marked live, not dropped
6. **Persists to SQLite** — writes to `data/sovereign_agent.db` alongside your feedback and weight history
7. **Synthesizes wisdom** — brief market summary (e.g., "startups are hiring DS roles at lower TC but higher equity; mid-market prefers remote-friendly")

### Layer 2: Feedback Loop (Streamlit UI)

`uv run streamlit run app.py` opens a three-page dashboard:

- **Home:** Your profile + market intelligence on the left; jobs table (editable, filterable by domain/source) on the right
- **Analytics:** Token usage breakdown (input/output/cache), pending improvements, applied change history with revert buttons
- **Historical Runs:** Last 5 searches; job counts and domain mix per run

You edit per-row **Lifecycle** (New/Applied/InProgress/Closed/Won/NotForMe), **Good/Bad**, and **Weight**, then click **Save**. The next `/fetchjobs` uses this feedback to bias discovery and scoring.

### Layer 3: Self-tuning (`/improve`)

After each `/fetchjobs`, the system can optionally run `/improve --auto` to:

1. **Self-heal** — Revert any prior applied change whose next-run metrics regressed (fewer valid jobs, lower high-score rate, or worse cost-per-valid-job)
2. **Auto-compact** — Inline transforms (hedge cleanup, prose→bullets), externalize examples to archive, move cold sections to archive, collapse cross-file duplication
3. **Stage human reviews** — Pain-point proposals (e.g., "you're searching too narrowly" or "scoring is rejecting roles you'd actually apply to") go to Streamlit for explicit approval
4. **Log recovery** — Every action logged in machine-readable form for future meta-improvement

Pro-user contract: you run heavy searches; the system keeps token cost down while defending signal quality.

---

## Walk through a real flow

### Setup (one-time)

```bash
# 1. Initialize project and create empty DB
uv run python orchestrator.py

# 2. Add your resume to data/
cp ~/my_resume.pdf data/resume.pdf

# 3. In chat, run /setup
# Agent reads resume, proposes a profile JSON, asks your plan tier (Pro/Max), saves to data/candidate_info.json
```

Your profile now has:
- `core_identity` — what you do
- `scientific_moat` — your research strengths
- `engineering_stack` — tech areas you know
- `target_seniority` — desired role level (e.g., Staff Data Scientist)
- `target_country` — geographic constraint
- `priority_domains` — industries you target (e.g., Renewable Energy, Autonomous Systems, Biotech)
- `golden_keywords` — search terms that work for you
- `noise_keywords` — filter out these
- `excluded_companies` — names to skip exactly
- `excluded_areas` — substring match on job theme
- `excluded_pairs` — AND-filters (e.g., "Microsoft:recruiter-only-roles")

### First search

```bash
# In chat: /fetchjobs
# System dispatches Lean (Pro) or Full (Max)
# ~ 10 turns, 500K tokens (Lean) or 150 turns, 20M tokens (Full)
# All jobs persisted to data/sovereign_agent.db
```

### Feedback + learning

```bash
# Open the Streamlit UI
uv run streamlit run app.py

# Edit each row: mark Lifecycle (Applied/NotForMe/etc.), Good/Bad, Weight
# Click "Save feedback & status"
```

The system logs which roles you applied to, which you rejected, and why (patterns: company size, domain, compensation, role maturity, team structure, etc.).

### Next search

```bash
# In chat: /fetchjobs again
# System reads your feedback log
# Biases discovery + scoring toward roles like ones you marked "Applied" or "Good"
# Avoids patterns from "NotForMe" or "Closed"
# Searches broader, but focuses deeper on validated signal
```

### Tune the system

```bash
# After a few searches, run:
# In chat: /improve

# System analyzes:
# - Token cost trend (is it creeping up?)
# - Proposal quality (what % of results are actually matches?)
# - Pain points (are you rejecting 90% of results? Is discovery too broad?)
#
# Auto-applies low-risk cost cuts (hedge cleanup, example externalization)
# Stages behavior changes (e.g., "search narrower on domain X") for approval
# Reverts any prior change that made metrics worse
```

---

## Key trade-offs

### Lean (Pro) vs. Full (Max)

| Dimension | Lean | Full |
| --- | --- | --- |
| **Token cost** | ~500K | ~20M |
| **Token source** | Pro 5-hour window | Max budget |
| **Architecture** | Descriptions externalized to disk; background Scoring Subagent | Descriptions in main agent context; orchestrator + multi-team setup |
| **Scoring nuance** | Top-3 summary from Subagent | Full description context |
| **Final result** | Same jobs, same quality | Same jobs, more pattern depth |

**Pro tip:** Pro users default to Lean. Max users can skip Lean and run Full directly (`/fetchjobs-pro` always runs Lean; to run Full, use `/fetchjobs` and pick it from the dispatcher).

### Feedback vs. computational cost

The more you give feedback, the richer the next search—but at a cost: the system expands discovery and scoring complexity to capture signal from your patterns. `/improve` auto-detects when that cost isn't worth it and stages a proposal to revert it.

### Automation vs. control

`/improve --auto` (default for Pro users) auto-applies cost-only changes and stages behavior changes for review. If you prefer manual review of everything, set `auto_improve_audit_enabled: true` in `candidate_info.json` and all proposals stage to Streamlit.

---

## When to run what

| Goal | Command | Notes |
| --- | --- | --- |
| **First time setup** | `/setup` | One-time; reads resume, saves profile config |
| **Discover new roles** | `/fetchjobs` | Run after profile update or feedback save |
| **Mark roles as applied/rejected** | Streamlit UI | Edit table rows, save feedback |
| **Compress cost, detect issues** | `/improve` | Run after 3+ `/fetchjobs` cycles; auto-applies safe changes |
| **Review pending improvements** | Streamlit Analytics tab | Approve human-gated proposals from `/improve` |
| **Revert a change** | Streamlit Improvement History | Click "Revert" on any applied row |
| **Start over fresh** | `python3 reset.py` + `python3 orchestrator.py` | Clears jobs/profile/history; keeps resume PDF |

---

## Troubleshooting

**Q: I see "VIRTUAL_ENV does not match"**  
A: Benign. Another project's `.venv` is active. Run `uv run ...` commands as-is, or `deactivate` the other env first.

**Q: My searches are too broad / too narrow**  
A: Use `/improve` to propose adjustments. Adjust `golden_keywords` or `noise_keywords` in the Streamlit sidebar, then save profile. Next search will reflect the change.

**Q: A role got dropped as invalid, but it's actually live**  
A: Link validation can have false positives. The next `/improve` cycle may auto-propose a fix, or you can manually adjust validation rules in `src/job_finder/link_validation.py` and rerun.

**Q: I want to try Full instead of Lean (or vice versa)**  
A: Add `"runtime_mode_override": "lean"` (or `"full"`) to `candidate_info.json` and the dispatcher will skip the prompt.

---

## Next steps

1. Read [How to Run](./HOW_TO_RUN.md) for step-by-step tactical commands
2. Explore [README.md](./README.md) for deep dives on architecture, schema, and `/improve` compaction tiers
3. Check [SKILLS.md](./SKILLS.md) for available Claude Code commands and skills
