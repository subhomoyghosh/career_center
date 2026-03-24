# /improve — God Mode Self-Improving Skill

> **Rules:** Never apply without explicit user approval per change. Never delete skill files. Quote exact current text in every proposal. Ground each proposal in a specific metric. One proposal at a time.

---

## 1. Ingest evidence

```bash
uv run python -c "
import json, pathlib
p = pathlib.Path('data/run_diagnostics.jsonl')
runs = [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()] if p.exists() else []
print(json.dumps(runs, indent=2))
"
```

```bash
uv run python scripts/get_nudge_context.py
```

```bash
uv run python -c "
import json, pathlib
p = pathlib.Path('data/improve_log.jsonl')
entries = [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()] if p.exists() else []
print(json.dumps(entries[-20:], indent=2))
"
```

Read current text of all skill/command files:

- `.claude/commands/setup.md`, `.claude/commands/fetchjobs.md`
- `.cursor/skills/validate-job-links/SKILL.md`, `.cursor/skills/leverage-feedback-and-weights/SKILL.md`
- `.cursor/skills/evaluate-nudge-and-wisdom/SKILL.md`, `.cursor/skills/discover-linkedin-jobs/SKILL.md`
- `.cursor/rules/jobsearch.mdc`, `.cursor/rules/setup_from_resume.mdc`
- `data/candidate_info.json`
- `.claude/commands/improve.md`, `.cursor/skills/godmode-improve/SKILL.md` (self-audit)

If **zero diagnostic runs** exist: print `NO_DIAGNOSTICS_YET` and stop.

---

## 2. Compute trend metrics (last 5 runs or all)

| Metric | Formula |
|--------|---------|
| `avg_zero_result_queries` | mean `zero_result_queries` |
| `avg_content_failed_rate` | mean `content_failed / max(candidates_extracted,1)` |
| `avg_board_page_hits` | mean `board_page_hits` |
| `avg_lever_css_fallback` | mean `lever_css_fallback_count` |
| `avg_valid_jobs` | mean `valid_jobs` |
| `pct_high_score` | mean `score_distribution["90-100"] / max(valid_jobs,1)` |
| `linkedin_ats_ratio` | mean `linkedin_with_ats / max(linkedin_discovered,1)` |
| `avg_manual_rescues` | mean `manual_rescues` |
| `domain_coverage_gaps` | domains in `priority_domains` at zero ≥ 3 consecutive runs |
| `max_skill_chars` | `wc -c` on each skill/command file; flag any > 10000 |
| `cross_tool_similarity` | flag pairs: fetchjobs↔jobsearch, setup↔setup_from_resume if char counts within 15% |

---

## 3. Pain point detection

| ID | Trigger | Severity |
|----|---------|---------|
| `SEARCH_TOO_NARROW` | `avg_zero_result_queries > 2` | HIGH |
| `LINK_VALIDATION_AGGRESSIVE` | `avg_content_failed_rate > 0.30` AND `avg_manual_rescues > 1` | MEDIUM |
| `BOARD_PAGE_LEAKAGE` | `avg_board_page_hits > 3` | HIGH |
| `LEVER_CSS_FALLBACK_HIGH` | `avg_lever_css_fallback > 3` | MEDIUM |
| `LOW_YIELD` | `avg_valid_jobs < 5` | HIGH |
| `SCORING_TOO_STRICT` | `pct_high_score < 0.10` AND `avg_valid_jobs > 5` | MEDIUM |
| `LINKEDIN_ATS_WEAK` | `linkedin_ats_ratio < 0.40` | LOW |
| `DOMAIN_BLIND_SPOT` | domain in `priority_domains` with `domain_coverage_gaps` | HIGH |
| `CANDIDATE_PROFILE_DRIFT` | `bad_feedback_title_tokens` (from nudge context) has ≥ 1 token with frequency ≥ 3 not already in `noise_keywords` | MEDIUM |
| `WISDOM_STALE` | wisdom in `candidate_info.json` unchanged across 3+ run dates | LOW |
| `SKILL_STALE_REFERENCE` | skill file references removed field, dead path, or hardcoded year | MEDIUM |
| `SKILL_TOKEN_BLOAT` | any skill/command file > 10000 chars, OR a cross-tool pair flagged above | MEDIUM |

**Print:**

```text
PAIN_POINT_REPORT (N runs, <date> → <date>)
FOUND:  [SEVERITY] ID  — metric = value
CLEAR:  ID  ✓  metric = value
```

If nothing found: `SYSTEM_HEALTHY` and stop.

---

## 4. Proposals (highest severity first)

For each pain point, present:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROPOSAL N/TOTAL  |  <ID> [SEVERITY]
Evidence: <metric = value>
File: <path>  |  Section: <heading>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT:
"""<exact text>"""

PROPOSED:
"""<replacement>"""

<one sentence: what this fixes and why>
Approve? [yes / no / modify: …]
```

**Pain-point → file mapping:**

| ID | Target | Action |
|----|--------|--------|
| `SEARCH_TOO_NARROW` | `fetchjobs.md` §Query building | Add 2-keyword fallback query strategy; cite `zero_result_query_strings` from diagnostics to name the specific failing queries |
| `LINK_VALIDATION_AGGRESSIVE` | `validate-job-links/SKILL.md` | Narrow dead-phrase list |
| `BOARD_PAGE_LEAKAGE` | `fetchjobs.md` §board-index detection | Expand URL signal list |
| `LEVER_CSS_FALLBACK_HIGH` | `fetchjobs.md` §Lever/Ashby | Add aggregator fallback targets |
| `LOW_YIELD` | `candidate_info.json` | Add board to `search_targets` or broaden `golden_keywords` |
| `SCORING_TOO_STRICT` | `fetchjobs.md` §scoring | Lower moat-match threshold |
| `LINKEDIN_ATS_WEAK` | `fetchjobs.md` §ATS backfill | Add `site:ashbyhq.com` to backfill queries |
| `DOMAIN_BLIND_SPOT` | `candidate_info.json` | Add domain-specific board to `search_targets` |
| `CANDIDATE_PROFILE_DRIFT` | `candidate_info.json` | Append pattern to `noise_keywords` |
| `WISDOM_STALE` | `fetchjobs.md` §Wisdom | Add diff-vs-prior instruction |
| `SKILL_STALE_REFERENCE` | Affected skill file | Replace stale field/path |
| `SKILL_TOKEN_BLOAT` | Bloated file | Apply compression: prose→bullets, remove hedge words ("you should", "make sure to", "it is important that"), remove "Why this is needed" sections, convert multi-paragraph explanations to a single line, fold constraint blocks inline. For cross-tool duplicate pairs, propose making the Cursor file a thin wrapper: frontmatter + one sentence delegating to the canonical `.claude/commands/` file + any Cursor-specific delta only. |

**Profile edits:** Show full proposed JSON value. Write via Python preserving all other keys:

```bash
uv run python -c "import json,pathlib; p=pathlib.Path('data/candidate_info.json'); cfg=json.loads(p.read_text()); cfg['<key>']= <value>; p.write_text(json.dumps(cfg,indent=2))"
```

---

## 5. Apply approved changes

Per approved proposal:

1. Edit the file (Edit tool for `.md`/`.mdc`, Python for `.json`).
2. Read changed section back to verify.
3. Append to `data/improve_log.jsonl`:

```bash
uv run python -c "
import json,pathlib,datetime
entry={'timestamp':datetime.datetime.utcnow().isoformat(),'pain_point':'<ID>','severity':'<S>','file_changed':'<path>','section':'<h>','evidence':'<metric=val>','approved_by':'user','summary':'<1 sentence>'}
pathlib.Path('data/improve_log.jsonl').open('a').write(json.dumps(entry)+'\n')
"
```

4. Print `✓ Applied: <ID> → <file>`

If user modified the proposal text, apply verbatim and log `"approved_by":"user_modified"`.

---

## 6. Summary

```text
IMPROVE SUMMARY
Proposals: N  |  Applied: N  |  Rejected: N
Files changed: <list>
Expected improvement: <metric> <old> → <target>
Run /fetchjobs to validate.
```

**Self-improvement:** This file and `.cursor/skills/godmode-improve/SKILL.md` are subject to the same process. Propose edits to them if a pain point applies. The skill improves itself.
