---
name: discover-linkedin-jobs
description: Discover LinkedIn jobs reliably and convert to direct ATS links. Use during /fetchjobs when search_targets includes linkedin.com/jobs or when LinkedIn jobs are missing from results.
---

# Discover LinkedIn Jobs

LinkedIn extraction is noisy and often blocked. Use this skill to get high-precision LinkedIn-origin signals, then convert to durable ATS links for persistence.

## When to apply

- During `/fetchjobs` if `search_targets` contains `linkedin.com/jobs`.
- When LinkedIn jobs are unexpectedly absent in discovered candidates.

## Why this is needed

- LinkedIn search results are time- and member-dependent.
- Generic LinkedIn queries pull people posts/profiles instead of job listings.
- LinkedIn pages can return login walls or anti-bot interstitials.
- Stable persistence is usually better with direct ATS links (Lever/Greenhouse/Ashby/Workday).

## Query strategy (high precision first)

1. Build role/method/location query strings using profile fields:
   - `golden_keywords` (titles + methods)
   - `scientific_moat`
   - `target_country`
2. Use LinkedIn-friendly Boolean style (uppercase `AND/OR/NOT`, quotes for phrases, parentheses allowed).
3. Run these query templates in order:
   - `site:linkedin.com/jobs/view/ "<role>" "<country>"`
   - `site:linkedin.com/jobs/view/ ("<method1>" OR "<method2>") "<country>"`
   - `site:linkedin.com/jobs/search "<role>" "<country>"`
4. Avoid broad `site:linkedin.com` queries.

## URL filtering rules

Keep only URLs matching at least one:
- `linkedin.com/jobs/view/`
- `linkedin.com/jobs/collections/`
- `linkedin.com/jobs/search/`

Drop:
- `linkedin.com/in/` (profiles)
- `linkedin.com/posts/` / `linkedin.com/feed/`
- `linkedin.com/company/` unless it is a clear job result page with a job URL

Normalize:
- Remove tracking query params when possible
- Keep canonical https URL

## Extraction contract (per candidate)

For each surviving LinkedIn-origin result, extract:
- `company`
- `title`
- `link`
- `source_hint` = `LinkedIn`
- optional: `linkedin_job_id` if present in URL

Reject candidate if company/title is missing.

## LinkedIn-to-ATS conversion (required preference)

For each LinkedIn-origin candidate:
1. Attempt to find external apply URL on listing page.
2. If found and host is ATS (`lever.co`, `greenhouse.io`, `ashbyhq.com`, `workday.com`), persist ATS URL.
3. If page is blocked or external URL not visible, backfill with web search:
   - `"<company>" "<title>" (site:jobs.lever.co OR site:boards.greenhouse.io OR site:jobs.ashbyhq.com OR site:workday.com)`
4. If ATS URL found, prefer ATS URL as persisted `link`.
5. Keep LinkedIn URL only when ATS backfill fails.

## Block/interstitial detection

Treat as blocked/unreliable if page contains signs like:
- `sign in` / `log in`
- `to continue`
- `unusual activity`
- session wall or anti-bot content without job details

If blocked:
- do not trust page for full parsing
- run ATS backfill path immediately
- mark confidence lower unless ATS recovered

## Confidence tiers (for internal selection)

- `high`: ATS link recovered and company/title confirmed
- `medium`: LinkedIn job URL with company/title confirmed but ATS not found
- `low`: ambiguous source or missing evidence (drop)

Persist only high/medium.

## De-duplication

- Primary key: normalized `link`
- Secondary key: lowercase `(company, title)` pair
- If duplicates exist, keep best in this order:
  1. ATS link over LinkedIn link
  2. higher score
  3. richer rationale evidence

## Output expectation

- Include LinkedIn-origin discoveries whenever relevant results exist.
- Prefer ATS-direct links in persisted rows.
- Keep LinkedIn URLs as fallback only.
- Do not force-link through validation if evidence is weak.
- If zero LinkedIn-origin survivors, log a concise reason:
  - `no_results` | `blocked` | `no_company_title` | `no_ats_backfill`
