# Scoring Rules — canonical reference

This file is the single source of truth for job scoring logic. Both the Max flow
(fetchjobs.md Step 3, inline main agent) and the Pro flow (Scoring Subagent in
fetchjobs-pro.md) read this file. Do NOT duplicate these rules in either skill file.

---

## 0. Theme definition

`theme`: a short 2–6 word descriptive phrase capturing the role's core focus, generated
from the JD text (e.g. "Bayesian Experimentation & Causal Inference", "AV V&V Safety
Validation", "Demand Forecasting & Pricing Science"). Used for:
- The recency-grouped display table
- Matching `excluded_areas` substrings — keep it lexically aligned with the JD's actual subject area

---

## 1. Hard filters — drop BEFORE scoring, do NOT generate rationale

Apply in this order. Any match → drop immediately, log `excluded_dropped` count.

**1a. Noise keywords** — drop if ANY `noise_keywords` entry appears case-insensitively in
the job `title` OR `description`:
- Default examples: Junior, Intern, Web Developer, Front End, Marketing Analyst,
  Business Intelligence, Entry Level, Contract, Sales Engineer
- Use the full `noise_keywords` list from `candidate_info.json`

**1b. Excluded companies** — drop if `company` (case-insensitive exact match) is in
`excluded_companies`.

**1c. Excluded areas** — drop if ANY entry in `excluded_areas` is a case-insensitive
substring of `theme`.

**1d. Excluded pairs** — drop if ANY entry in `excluded_pairs` matches BOTH sides:
split on the FIRST colon; left side = company (exact, case-insensitive), right side =
substring of theme. Empty-after-strip entries are silently ignored.

**1e. Location + work-mode constraints** — three orthogonal fields from `candidate_info.json`:

- `allowed_metros: [str, ...]` — empty list ⇒ no metro constraint. Each entry is a **fuzzy region name** (e.g. `"San Francisco Bay Area, CA"`, `"Greater Boston"`, `"NYC metro"`). Use your geographic knowledge to judge membership, **not** substring match. E.g. `"Mountain View, CA"`, `"Oakland, CA"`, `"Palo Alto"` all belong to `"San Francisco Bay Area, CA"`. `"Cambridge, MA"`, `"Somerville, MA"` belong to `"Greater Boston"`.
- `allowed_work_modes: [str, ...]` — subset of `{remote, hybrid, onsite}`; empty ⇒ any mode OK
- `remote_anywhere_ok: bool` — if true, remote roles bypass the metro check

**Parsing rules (anti-false-positive):**

- **Mode** must be parsed from the JD's location/work-mode header — typically the line immediately after the job title, the "Locations" field, the "Employment Type" field, or the very first paragraph. Look for `remote`, `hybrid`, `onsite`/`on-site`/`in-office`/`in person`. **Do NOT match stray mentions** elsewhere (e.g. "we have remote employees" in a benefits paragraph does NOT make the role remote). If the header is silent, mode = `unknown`.
- **Location** is the city/state/region stated in the same header / "Locations" field. If the JD says "Multiple locations" or lists several, use the FIRST location or the one the URL/post-slug suggests. If the header is silent, location = `unknown`.
- "Remote-first", "fully remote", "Remote (US)", "Anywhere in US" → `remote`.
- "Hybrid (3 days in office)" → `hybrid`; the cadence detail is informational only.
- "On-site at HQ", "5-day onsite" → `onsite`.

Drop logic:

1. If `allowed_work_modes` is non-empty AND `mode in {remote, hybrid, onsite}` AND `mode NOT IN allowed_work_modes` → drop, log `excluded_work_mode: <mode>`.
2. Else if `allowed_metros` is non-empty:
   - If `mode == remote` AND `remote_anywhere_ok` → keep (remote bypasses metro check).
   - Else: use geographic judgement to decide if `location` belongs to any `allowed_metros` region. If NO region contains the location → drop, log `excluded_location: <location>`.
3. `unknown` mode or `unknown` location → KEEP (do not drop on missing data; rationale should note the uncertainty).

**Precedence:** `excluded_*` and location/work-mode filters ALWAYS win over `peer_companies`. Do not include excluded companies in peer-company organic search queries.

---

## 2. Score bands

**Score 90–100:**
Either:
- (a) Description **explicitly requires ≥ 3 items** from `scientific_moat`, OR
- (b) Description explicitly requires **≥ 2 items** from `scientific_moat` AND the role
  is in a `priority_domain`.
High confidence required in both cases.

**Score 70–89:**
Matches `core_identity` AND at least **2 items from `engineering_stack`** OR at least
**1 item from `scientific_moat`**. Good fit with fewer rare-skill signals.

**Penalty:** Deduct points for generic job descriptions that do not mention rigorous
evaluation methods or specialized modeling. Generic cloud/data-pipeline roles without
domain-specific science score below 70.

**Threshold:** Only forward candidates with score ≥ 70 AND not matched by any hard filter.

---

## 3. Soft-bias patterns — NEVER a hard filter

Pattern fields (`inclinations`, `disinclinations`, `learn_skills`) come from the LLM-driven
synthesizer in `/improve` after human approval. Each entry has:
`{pattern|skill, evidence_job_ids|source_jobs, confidence: HIGH|MED|LOW, source_axis, added_at}`

Scale all adjustments by confidence: `HIGH = ×1.0`, `MED = ×0.7`, `LOW = ×0.4`.

**Inclinations:** For each entry whose `pattern` is a case-insensitive substring of the
job description or rationale → `+5` per match. Cap aggregate inclination bonus at `+15`.

**Disinclinations:** Same substring rule → `-5` per match. Cap aggregate penalty at `-15`.

**Learn skills:** For each entry whose `skill` appears in description AND is NOT in the
candidate's `engineering_stack` → `+3` (signals genuine growth opportunity). Cap at `+9`.

Note matched patterns in RATIONALE (e.g. `+10 inclinations: Series-B clean energy,
probabilistic forecasting`). This makes the soft bias auditable.

---

## 4. Lifecycle-status dedup

Query the live DB once for `{link: status}` of all rows whose
`status IN ('Applied', 'InProgress', 'Closed', 'Won', 'NotForMe')`.

If a freshly discovered candidate's `link` is already in that set → **DROP from this
run's surfacing list**. Persist nothing; do not score it.
Log `dropped_terminal_status: {link, prior_status}` in diagnostics.

**CRITICAL:** `Closed` is genre-POSITIVE (user applied, outcome was external — still wants
similar roles). The dedup only avoids re-surfacing the EXACT row; it does NOT remove the
row's positive contribution to soft-bias scoring (the synthesizer already extracted patterns
from it).

---

## 5. Vesting & funding bonus

Apply a **1.2× multiplier** for any of: "Series C/D", "IPO-bound", "NIST Grant",
"DOE funding" appearing in the description.

Apply a **negative signal** (score penalty, not hard filter) for generic "Agency" or
"Consultancy" roles unless the JD explicitly calls for PhD-level or scientific work.

---

## 6. Scoring confidence cap

If job description text was unavailable (Lever/Ashby CSS fallback) AND no aggregator
text was found → **cap score at 79** and note `description_not_fetched: true` in rationale.

---

## 7. Rationale contract

The `RATIONALE` field MUST:
1. Explain **HOW** the candidate's `scientific_moat` solves a **specific problem** mentioned
   in the JD — cite JD phrases verbatim.
2. Mention any soft-bias patterns matched (inclinations, disinclinations, learn_skills).
3. Note `description_not_fetched: true` if applicable (confidence cap was applied).

Do NOT write generic rationales. A rationale that could apply to any job is a bug.
