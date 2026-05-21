### Wave 3 — Backfill (single parallel message, mixed WebSearch + WebFetch)

Combine these into one parallel batch:
- ATS backfill for any LinkedIn hits (`"[company]" "[title]" site:lever.co OR site:greenhouse.io OR site:ashbyhq.com`)
- Aggregator fallback for Ashby (CSS-only) and Workday (JS-empty) candidates from Wave 2 (`"[company]" "[title]" site:builtin.com OR site:simplify.jobs OR site:levels.fyi`)
- Direct WebFetch of any new ATS URLs surfaced by Wave 3 searches

For each new fetched description, externalize as in Wave 2. Append to the candidate list with `source_wave: 3`.
