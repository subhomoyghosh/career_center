# /setup (Inference & Initialization)

Act as a **Career Architect**. When the user has a resume PDF in `data/` whose **filename contains "resume"** (case-insensitive; e.g. `resume.pdf`, `My_Resume.pdf`), run this **before** any job fetch. **Discover the file** by listing `data/` (e.g. `ls data/`) and selecting any `.pdf` whose name contains "resume" — do not rely on glob/file search because `data/*.pdf` may be gitignored.

## Goal
Read the resume PDF, infer every field below from resume cues, and **propose** a JSON for `data/candidate_info.json`. Present for review and ask if the user wants to save. **Do not run /fetchjobs** until the user has confirmed or edited the file.

---

## When the resume is general or high-level

Resumes are often generic: "Data Scientist with 5+ years building ML models," "Experience in Python, SQL, and cloud," "Worked on forecasting and experimentation." Your job is to **harness** that and still **build a rich, structured profile** (same shape as the target schema) so job search and scoring work well.

- **Elevate, don't just copy.** Use the field mappings below to turn broad phrases into the **target vocabulary**: e.g. "statistical modeling" + "ML" → consider **Bayesian Inversion & UQ**, **Causal Inference & Structural Modeling**; "forecasting" / "time series" → **Spatiotemporal Modeling**; "A/B tests" / "experiments" → **Experimental Design (V&V)**. Prefer the compound, high-barrier labels from the map even when the resume doesn't use those exact words.
- **Infer domains from context.** If the resume doesn't name industries, infer from employer type and role focus: product/tech/SaaS companies → **E-commerce/Marketplace**; research lab / university / grants → **Climate/Geospatial** or **Biotech/R&D** if any science/env/health keywords; energy, utilities, infrastructure → **Renewable Energy/Grid**; anything with safety, perception, or robotics → **Autonomous Systems**. Always output 3–5 **priority_domains** so search can bias toward relevant verticals.
- **Keep the profile strong and searchable.** Always output: a single crisp **core_identity** sentence (degree + title + 2–3 method themes + domains if inferable); **5–7 scientific_moat** items (map broad skills to the taxonomy; you may add 1–2 related high-barrier skills that fit the profile so the list stays strong); **engineering_stack** from any tools mentioned (and common defaults like Python, SQL if the role is clearly technical); **golden_keywords** combining titles + methods + applications; full **search_targets** and **noise_keywords** from the default list. The goal is a profile **similar in structure and quality** to one built from a very detailed resume.
- **Default when silent.** If the resume doesn't specify location, use **target_country: "USA"**. If it doesn't specify seniority, infer from years and latest title and still output **target_seniority** (e.g. Staff / Principal / Lead for 8+ years). Never leave **priority_domains**, **search_targets**, or **noise_keywords** empty—use the defaults from the schema below.

---

## Resume → Field inference map (use these cues to infer; elevate general language to this vocabulary)

### 1. core_identity (Strategic Pitch)
- **Where to look:** Profile/Summary/Objective + **most recent 1–2 job titles** + recurring methods and domains across roles. If the resume is general, use title + "specializing in" themes you inferred from their skills (e.g. ML + forecasting → "predictive modeling and uncertainty-aware systems").
- **How to write:** One high-impact sentence: [Degree/Discipline if stated, else infer from role] + [Current or target title] + specializing in [2–3 method themes] + [1 line on impact]. Expert in [broad capability]. [Optional: domains—infer from employer type if not stated].
- **General resume:** "Data Scientist with 5+ years in ML" → elevate to e.g. *"... specializing in statistical modeling, forecasting, and decision systems. Expert in building scalable ML solutions for product and operations."* Add domains (e.g. E-commerce/Marketplace, Tech) if you can infer from company names or role focus.

### 2. scientific_moat (5–7 rare, high-barrier skills)
- **Where to look:** Education, Skills section, bullet points, publications. For general resumes, map **broad phrases** to the taxonomy below and add 1–2 adjacent skills that fit the profile so the list stays strong (e.g. "ML and forecasting" → include Spatiotemporal Modeling and Uncertainty Quantification).
- **Phrase → moat mapping (elevate general language to these labels):**
  - Bayesian, UQ, probabilistic, uncertainty, statistical modeling, inference → **"Bayesian Inversion & UQ"** or **"Uncertainty Quantification"**
  - Causal, structural models, econometrics, A/B testing, experiments, experimentation → **"Causal Inference & Structural Modeling"**
  - Time series, forecasting, spatial, geospatial, GIS, temporal → **"Spatiotemporal Modeling"**
  - Physics-informed, inverse problems, PDEs, simulation (or strong math/ML) → **"Physics-Informed ML"**, **"Inverse Problems"**
  - High-dimensional, multivariate, large-scale inference → **"High-Dimensional Statistical Inference"**
  - V&V, experimental design, hypothesis testing, metrics validation → **"Experimental Design (V&V)"**
  - Demand forecasting, forecasting pipelines, change-point, anomaly detection → **"Demand Forecasting"** or fold into Spatiotemporal / UQ.
- **Rule:** Always output 5–7 items. Use the compound labels above. For general resumes, infer from "ML," "statistics," "forecasting," "experimentation," "modeling" and still fill the list so the profile is searchable and similar in quality to a detailed one.

### 3. engineering_stack (tools and infrastructure)
- **Where to look:** Skills/Tools section, bullet points. If the resume only says "Python, SQL, cloud," list those and add **common defaults** for the role: e.g. scikit-learn, pandas, AWS so the stack is still useful for search.
- **Extract:** Languages (Python, R, C++, Julia, SQL), ML/libs (PyTorch, PyMC, Stan, JAX, scikit-learn), big data (PySpark, Spark, Databricks, dbt, Snowflake), cloud (AWS, GCP, Azure), HPC (OpenMP, MPI), CI/CD, Docker. Group **C++ with OpenMP/MPI** as **"C++ (HPC)"** if HPC is mentioned.
- **Format:** List as-is; use slashes for equivalents (e.g. **"PyMC / Stan"**). 8–15 items; include sensible defaults when the resume is general.

### 4. target_seniority
- **Where to look:** Years of experience (sum post-PhD or post-Bachelor), latest job titles (Senior, Staff, Principal, Lead, Director).
- **Mapping:** 0–3 yrs → Junior / Engineer; 4–7 yrs → Senior / Lead; 8+ yrs → Staff / Principal / Lead / Director.
- **Format:** Single string with slashes, e.g. **"Staff / Principal / Lead"**.

### 5. target_country
- **Where to look:** Current role location, "Remote" or office locations, citizenship/visa if stated.
- **Default:** **"USA"** unless resume clearly indicates another primary market (e.g. UK, Canada, Remote-global).

### 6. priority_domains (industries/verticals to prioritize)
- **Where to look:** Employer names, project descriptions, domain keywords. When the resume is general, **infer from employer type and role**: product/tech/SaaS → **E-commerce/Marketplace** or **Tech**; research/university/grants → **Climate/Geospatial** or **Biotech/R&D** if any science/env/health; energy/utilities/infra → **Renewable Energy/Grid**; safety/perception/robotics → **Autonomous Systems**.
- **Keyword → domain mapping (use exact labels):**
  - AV, robotics, perception, safety validation, autonomous → **"Autonomous Systems"**
  - Energy, grid, renewable, solar, storage, utilities → **"Renewable Energy/Grid"**
  - Marketplace, e-commerce, demand, supply chain, retail, product/tech company → **"E-commerce/Marketplace"**
  - Climate, GHG, emissions, geospatial, environmental, research → **"Climate/Geospatial"**
  - Biotech, pharma, clinical, drug discovery, health → **"Biotech/R&D"**
- **Rule:** Always output 3–5 domains. If the resume is silent, default to **["E-commerce/Marketplace"]** (or add **"Tech"**-style domain) so search still has a bias; the user can edit.

### 7. golden_keywords (search query fodder: roles + methods + applications)
- **Where to look:** Job titles, Skills section, bullets. For general resumes, **elevate**: "data science" → Applied Scientist, Data Scientist; "ML models" → Machine Learning, Scientific ML; "forecasting" → Time Series Forecasting, Demand Forecasting; "experiments" → Causal ML, Econometrics.
- **Combine:** Role titles (Applied Scientist, Research Scientist, Data Scientist) + methods (Bayesian Inference, UQ, Causal ML, Econometrics, Time Series Forecasting, Scientific ML, Physics-Informed ML, Structural Estimation) + applications (Demand Forecasting) that appear or are strongly implied.
- **Format:** Single comma-separated string. Always produce a rich list so /fetchjobs can build good queries even from a general resume.

### 8. search_targets (job boards / ATS)
- **Default (use unless resume suggests otherwise):**
  **["lever.co", "greenhouse.io", "ashbyhq.com", "workday.com", "jobs.lever.co", "boards.greenhouse.io", "linkedin.com/jobs"]**
  so that both ATS and LinkedIn jobs are in scope.

### 9. noise_keywords (filter out these role/title patterns)
- **Infer from level and role type:** Candidate is typically not targeting junior, intern, or non-quant roles.
- **Default list (use unless resume suggests otherwise):**
  **["Junior", "Intern", "Web Developer", "Front End", "Marketing Analyst", "Business Intelligence", "Entry Level", "Contract"]**
  to filter out mismatched postings.

### 10. allowed_metros + allowed_work_modes + remote_anywhere_ok (location/work-mode hard filter)

- **What:** Three orthogonal fields enforced as a hard filter in `_scoring_rules.md §1e`. Empty list / missing field on any axis ⇒ NO constraint on that axis.
- **Cannot be inferred from resume** — ask the user via `AskUserQuestion` after presenting the inferred JSON. Three questions:
  1. *"Which metro regions are you open to for hybrid/onsite roles? (Leave blank for any.)"* — answer is a list of **fuzzy region names** like `"San Francisco Bay Area, CA"`, `"Greater Boston"`, `"NYC metro"`. The Scoring Subagent uses geographic knowledge to judge city-to-region membership — do NOT enumerate cities. Default to **[]** if user says "any" or skips.
  2. *"Which work modes are you open to?"* — multi-select from `remote`, `hybrid`, `onsite`. Default to **["remote", "hybrid", "onsite"]** if user skips.
  3. *"Are remote roles outside your allowed_metros OK?"* — yes/no. Default **true** if user skips. (If `remote_anywhere_ok: true`, remote postings bypass the metro check.)
- **Format:**
  - `allowed_metros: ["San Francisco Bay Area, CA"]` — one fuzzy region per entry; LLM-judged membership at scoring time
  - `allowed_work_modes: ["remote", "hybrid", "onsite"]` (subset)
  - `remote_anywhere_ok: true`

### 11. wisdom
- Leave as **`""`**. It is filled later by /fetchjobs.

### 12. plan_tier (Claude Code subscription)
- **What:** Which Claude Code plan the user is on. Controls which `/fetchjobs` variant runs (Max-tier uses Opus + verbose chat; Pro-tier uses Sonnet + description-externalized scoring to fit a Pro 5-hour rate window).
- **How to fill:** Cannot be inferred from the resume — ask the user via `AskUserQuestion` at the END of the /setup flow (after presenting the inferred JSON). Options: `Pro ($20/mo)` → save as `"pro"`; `Max 5x ($100/mo)` → save as `"max5x"`; `Max 20x ($200/mo)` → save as `"max20x"`.
- **Default if user skips:** leave as `""`. The `/fetchjobs` dispatcher will ask again on first invocation.

---

## Output schema (flat, for `data/candidate_info.json`)

```json
{
  "core_identity": "",
  "scientific_moat": [],
  "engineering_stack": [],
  "target_seniority": "",
  "target_country": "USA",
  "priority_domains": [],
  "golden_keywords": "",
  "search_targets": ["lever.co", "greenhouse.io", "ashbyhq.com", "workday.com", "jobs.lever.co", "boards.greenhouse.io", "linkedin.com/jobs"],
  "noise_keywords": ["Junior", "Intern", "Web Developer", "Front End", "Marketing Analyst", "Business Intelligence", "Entry Level", "Contract"],
  "allowed_metros": [],
  "allowed_work_modes": ["remote", "hybrid", "onsite"],
  "remote_anywhere_ok": true,
  "wisdom": "",
  "plan_tier": ""
}
```

---

## Steps

1. **Find the resume PDF:** List `data/` (e.g. `ls data/`). Pick any file that (a) ends with `.pdf` and (b) has **"resume"** in the filename (case-insensitive). Use that path to read the PDF. If none exists, tell the user to add a resume PDF with "resume" in the filename and try again.
2. **Infer** each field using the map above. Fill the JSON. Leave `wisdom` and `plan_tier` as `""`.
3. **Ask `plan_tier`** via `AskUserQuestion` (cannot be inferred from resume — it's a subscription/budget question). Question: "Which Claude Code plan are you on? (controls whether /fetchjobs uses the full Opus-tier flow or the Sonnet-tier sharded variant)". Options: `Pro ($20/mo)` → `"pro"`; `Max 5x ($100/mo)` → `"max5x"`; `Max 20x ($200/mo)` → `"max20x"`. Save the lowercase key (`pro` / `max5x` / `max20x`) into the proposed JSON's `plan_tier` field. If the user skips, leave `""` — the /fetchjobs dispatcher will ask again on first run.
4. **Present** the full JSON to the user. Say: *"I've analyzed your resume; here is the identity and moat I've built for you. Review and edit if needed. Shall I save this to data/candidate_info.json (or your existing profile path)?"*
5. **Do not** run /fetchjobs in this flow. Only after the user confirms (and optionally saves) should they run `/fetchjobs` separately.
