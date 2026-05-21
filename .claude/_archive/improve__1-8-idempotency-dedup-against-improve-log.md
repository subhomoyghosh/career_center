## 1.8 Idempotency — dedup against improve_log

```bash
uv run python -c "
import json, pathlib, datetime
p = pathlib.Path('data/improve_log.jsonl')
entries = [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()] if p.exists() else []
# Pain-points approved in the last 14 days are considered 'recently closed' and should not re-propose
# unless their evidence metric has materially worsened (≥10% delta from the value recorded in the log).
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)).isoformat()
recent = [e for e in entries if e.get('timestamp', '') >= cutoff]
print(json.dumps({'recent_closed_pain_points': sorted({e['pain_point'] for e in recent}), 'recent_evidence_by_pp': {e['pain_point']: e.get('evidence', '') for e in recent}}, indent=2))
" > /tmp/improve_dedup.json
cat /tmp/improve_dedup.json
```

In Section 3 pain-point detection, BEFORE printing each FOUND pain-point: check the `recent_closed_pain_points` list. If the pain-point id appears there AND the current metric value is within 10% of the logged evidence value (i.e., no material drift), DROP it from the FOUND set and instead include it under a `SUPPRESSED (recently closed):` section. Print the suppressed list at the bottom of PAIN_POINT_REPORT so the user can see what was filtered.

If the user wants to force re-propose anyway, they can pass an explicit override (e.g., `/improve --force <PAIN_POINT_ID>`) — but no auto-resurfacing inside the 14-day window without ≥10% metric worsening.

**Note:** `data/improve_changes.jsonl` (written by `job_finder.improve_changes.apply_proposal`) and `data/improve_log.jsonl` (legacy mirror) are both written on every apply — dedup in this section reads `improve_log.jsonl` for back-compat. Don't migrate dedup off it without a coordinated change.
