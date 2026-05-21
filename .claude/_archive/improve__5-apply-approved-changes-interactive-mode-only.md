## 5. Apply approved changes (interactive mode only)

> Mode dispatch: `--audit-only` → skip this section (proposals serialized in §4.1 for Streamlit approval). `--apply <change_id>` → call `apply_proposal(change_id)` and exit. `--restore <change_id>` → call `revert_change(change_id, reverted_by="ui_user")` and exit. `--auto` → run the auto-orchestration loop (§5.5). The numbered steps below apply only to the interactive default.

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
