## 5.5 `--auto` orchestration (autonomous mode)

Triggered automatically at the end of every `/fetchjobs` run when `auto_improve_enabled` is true (the default for pro users). The orchestrator is the /improve LLM itself; the script provides the data, the LLM does the dispatch. No human gate for cost-only changes — but every change is regression-checked on the next run and silently reverted if quality drops.

**Sequence:**

1. **Audit.** `uv run python scripts/audit_run_efficiency.py > /tmp/improve_audit.json`. If `audit_failed: true`, abort the cycle and print one line: `AUTO_ABORTED: audit_failed (<reason>)`. Do not apply or revert anything.

2. **Self-heal (auto-revert).** Walk `auto_revert_candidates.regressions` from the audit. For each entry:
   ```bash
   uv run python -c "from job_finder import improve_changes; print(improve_changes.revert_change('<change_id>', reverted_by='auto_regression_guard'))"
   ```
   Capture (a) change_id, (b) regression_reasons (already human-readable, include the threshold), (c) revert result. If revert fails (e.g., source file no longer in expected state), log the failure but continue — do NOT halt the whole cycle.

3. **Mark validated.** Walk `auto_revert_candidates.validated`. For each, append `{"change_id": "...", "validated_at": "<iso>"}` to a `validations` log so future audits don't re-check the same row. (Schema: extend `data/improve_changes.jsonl` rows by appending a new line with `{"change_id", "validated_at"}` keyed to the original.)

4. **Walk compaction tiers.** For each Tier 1–4 candidate (§0.7), generate a proposal dict using the same shape as `--audit-only` (§4.1), then immediately apply:
   ```bash
   uv run python -c "from job_finder import improve_changes; print(improve_changes.apply_proposal('<change_id>', approved_by='auto', pre_metrics=<from audit.efficiency>))"
   ```
   - `pre_metrics` MUST be the JSON-serialized current `efficiency` block from the audit. This is what powers the next-run regression check.
   - For Tier 3 (`archive_section`), the proposal's `archive_path` must resolve under `.claude/_archive/`; `apply_proposal` validates this and refuses otherwise.
   - Track `total_bytes_reclaimed` += (source section bytes − stub bytes) for the summary.

5. **Stage human-gated proposals.** PATTERN_*, SCORING_DRIFT_DETECTED, REGRESSION_DETECTED, COMPACTION_STAGNATION, LOW_APPLY_CONVERSION, LATENCY_CRITICAL_PATH, PRUNER_FPR_ALERT → call `write_proposal()` to stage to Streamlit; do NOT auto-apply.

6. **Brief summary.** Print the Auto Summary block (§6 below). Keep it under 12 lines — the user reads it at the end of every /fetchjobs cycle.

**Safety invariants the orchestrator must NOT violate:**

- Never auto-apply a Tier 3 archive if the section's `n_fingerprints == 0` (unmeasurable — the absence of refs is meaningless).
- Never auto-apply if `efficiency.valid_jobs == 0` for the current run (the run failed; don't compact based on a broken signal).
- Never auto-apply more than 10 Tier 3 archives in a single cycle (cap per-run churn; if there are more candidates, the rest stage to Streamlit for batch review).
- If `auto_revert_candidates.regressions` is non-empty, SKIP step 4 entirely for this cycle — first prove quality holds at current bytes before cutting further.
