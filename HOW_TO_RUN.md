# How to Run

1. `uv run python reset.py`
2. `uv run python orchestrator.py`
3. Add your resume PDF to `data/`
   - filename should contain `resume` (example: `my_resume.pdf`)
4. Run `/setup` in chat
5. Run `/fetchjobs` in chat
6. Open UI:
   - `uv run streamlit run app.py`
7. Add feedback (Good/Bad + weight) in UI and rerun `/fetchjobs`
8. Review snapshots for full history

## Snapshot review (optional)

```bash
uv run python scripts/snapshot_history.py candidate
uv run python scripts/snapshot_history.py jobs
uv run python scripts/snapshot_history.py intelligence
```
