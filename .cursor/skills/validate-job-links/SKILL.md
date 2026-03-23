---
name: validate-job-links
description: Validates job listing URLs before persisting — dead pages, wrong board redirects, title mismatch. Use MCP web fetch for agent passes; headless code uses filter_valid_job_links. Primary quality gate for /fetchjobs.
---

# Validate job links (authoritative)

The product **does not store geo/date/work_mode** in the DB. **Link quality** is the main trust signal: each row must point to a **live listing** that matches **company + title**.

## Automated (Python)

`job_finder.link_validation.filter_valid_job_links(jobs)` runs before `persist_jobs` in the `/fetchjobs` persistence step:

- Valid `http(s)` URL only.
- HTTP **2xx**, non-empty body (**≥ ~400 chars**).
- Body must **not** contain dead-job phrases (see `DEAD_PAGE_PHRASES` in `link_validation.py`).
- For **non-LinkedIn** URLs, the HTML should **echo enough of the job title** (reduces “company board index” false positives). **LinkedIn** is relaxed (bot-visible HTML often omits full title).
- Optional: pass `require_title_in_body=False` to only enforce HTTP + length + dead phrases.

## Agent (MCP) — when user asks or before trusting a hand-built list

1. For each job, **MCP web fetch** `link`.
2. **Remove** if non-200 (including LinkedIn 403/login walls; inconclusive links are not persisted).
3. **Remove** if content matches gone-job language (expired, filled, 404, etc.).
4. **Greenhouse / board redirect:** 200 but generic board with **no** job title → remove.
5. **LinkedIn:** If 200 but page is another employer’s list / generic search and **not** company+title → remove. If 403/login → **remove**.
6. **Indeed / Glassdoor / ZipRecruiter:** 403/timeout → remove (to match headless gate); 200 + expired → remove.
7. **Workday:** “Oops, an error occurred” style → remove if no real JD.
8. **Lever:** 404 or “no longer” in body → remove.
9. **Ashby:** timeout/403 → keep; 200 + gone → remove.

## Then persist

Only pass jobs that survived validation into `persist_jobs`.

## Summary

| Layer | Tool |
|-------|------|
| Headless `/fetchjobs` | `filter_valid_job_links` → `persist_jobs` |
| Chat / audit | This skill + MCP fetch |
