import json
import logging
import pathlib

import pandas as pd
import plotly.express as px
import streamlit as st

from job_finder.history import record_jobs_snapshot_from_db
from job_finder.improve_changes import (
    apply_proposal,
    dismiss_proposal,
    list_applied_changes,
    list_pending_proposals,
    revert_change,
)
from job_finder.persistence import (
    VALID_USER_STATUSES,
    update_jobs_feedback_batch,
    update_jobs_status,
)
from job_finder.ui_data import invalidate_data_cache

_LOG = logging.getLogger(__name__)
_DATA_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data"

HIDDEN_COLS = {"description", "failed_validation_count", "last_validated_at"}

# Newest-first columns the user looks at; remaining cols (id, etc.) trail behind.
PRIMARY_COL_ORDER = (
    "company", "title", "score", "theme", "rationale",
    "status", "user_feedback", "user_weight",
    "source", "link", "first_seen", "posted_at",
)

_USER_STATUS_OPTIONS = sorted(VALID_USER_STATUSES)

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_tokens", "productive_tokens", "tokens_lost")
_TOKEN_ROW_LABELS = {
    "input_tokens": "Input",
    "output_tokens": "Output",
    "cache_tokens": "Cache",
    "productive_tokens": "Productive",
    "tokens_lost": "Lost",
}

_GLOSSARY = {
    "run_date": "ISO timestamp of the /fetchjobs run",
    "valid_jobs": "jobs that passed validation and were persisted",
    "webfetch_calls": "HTTP fetches issued during the run",
    "calls_per_job": "webfetch_calls / max(valid_jobs, 1) — lower is better",
    "input_tokens": "LLM input-context tokens for the run",
    "output_tokens": "LLM output tokens generated",
    "cache_tokens": "LLM cached-prompt tokens",
    "productive_tokens": "tokens that produced persisted output",
    "tokens_lost": "tokens consumed without useful output",
}


def _diag_mtime() -> float:
    diag = _DATA_DIR / "run_diagnostics.jsonl"
    try:
        return diag.stat().st_mtime
    except OSError:
        return 0.0


@st.cache_data(show_spinner=False)
def _diagnostics_runs_cached(mtime: float, limit: int) -> list:
    diag = _DATA_DIR / "run_diagnostics.jsonl"
    if not diag.exists():
        return []
    rows = []
    try:
        for line in diag.read_text().splitlines()[-limit:]:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def _load_diagnostics_runs(limit: int = 20) -> list:
    """Read up to the last `limit` diagnostics rows. Cached on file mtime."""
    return _diagnostics_runs_cached(_diag_mtime(), limit)


def render_home_board(profile: dict, jobs: pd.DataFrame, show_wisdom: bool, show_weight_column: bool) -> None:
    """Home page: filters + full-width table + feedback save.

    Market Intelligence lives in the sidebar (rendered by render_profile_sidebar)
    so its width inherits the sidebar's native drag-to-resize. The `profile` and
    `show_wisdom` parameters are accepted for API stability but no longer used
    here — kept so callers don't have to change.
    """
    del profile, show_wisdom  # rendered in the sidebar; kept in signature for callers

    if jobs is None or jobs.empty:
        st.warning("Database empty. Run the `/fetchjobs` command to begin the autonomous research cycle.")
        return

    domains = (
        ["All"] + sorted(jobs["theme"].dropna().unique().tolist())
        if "theme" in jobs.columns else ["All"]
    )
    if "domain_filter" not in st.session_state:
        st.session_state["domain_filter"] = "All"

    selected_domain = st.selectbox(
        "Filter by domain",
        domains,
        key="domain_filter",
        help="Show only jobs in this domain.",
    )
    if selected_domain != "All":
        jobs = jobs[jobs["theme"] == selected_domain].copy()
    if "source" in jobs.columns:
        sources = ["All"] + sorted(jobs["source"].dropna().unique().tolist())
        source_choice = st.selectbox(
            "Filter by source",
            sources,
            key="job_source_filter",
        )
        if source_choice != "All":
            jobs = jobs[jobs["source"] == source_choice].copy()

    st.subheader("Job board")

    candidate_cols = [c for c in PRIMARY_COL_ORDER if c in jobs.columns]
    extras = [
        c for c in jobs.columns
        if c not in candidate_cols and c not in HIDDEN_COLS and c != "id"
    ]
    # `id` is required for diff-save; we hide it visually via column_config below.
    display_cols = ["id"] + candidate_cols + extras if "id" in jobs.columns else candidate_cols + extras
    if not show_weight_column and "user_weight" in display_cols:
        display_cols = [c for c in display_cols if c != "user_weight"]

    jobs_display = jobs[display_cols].copy()
    col_config = {
        "id": st.column_config.TextColumn("id", width="small", disabled=True, help="Internal row id"),
        "link": st.column_config.LinkColumn("Listing URL"),
        "score": st.column_config.ProgressColumn(
            "Moat Score",
            min_value=0,
            max_value=100,
            format="%d%%",
        ),
        "rationale": st.column_config.TextColumn("Scientific Fit", width="large"),
        "theme": st.column_config.TextColumn("Domain"),
        "source": st.column_config.TextColumn("Source", width="small"),
        "status": st.column_config.SelectboxColumn(
            "Lifecycle",
            options=_USER_STATUS_OPTIONS,
            required=True,
            help=(
                "Application lifecycle. Applied/InProgress/Closed/Won stay POSITIVE-GENRE "
                "(nudges future search toward similar roles). NotForMe is NEGATIVE-GENRE."
            ),
        ),
        "user_feedback": st.column_config.SelectboxColumn(
            "Good / Bad",
            options=["—", "Good", "Bad"],
            required=True,
            help="Genre preference, independent of lifecycle. Nudges future search.",
        ),
    }
    if show_weight_column:
        col_config["user_weight"] = st.column_config.NumberColumn(
            "Weight",
            min_value=0,
            max_value=100,
            default=50,
            help="How much to favor similar roles next fetch. 100 = very good.",
        )
    disabled_cols = [
        c for c in ("company", "title", "link", "score", "theme", "rationale", "source",
                    "first_seen", "posted_at")
        if c in jobs_display.columns
    ]

    edited_jobs = st.data_editor(
        jobs_display,
        column_config=col_config,
        hide_index=True,
        use_container_width=True,
        disabled=disabled_cols,
        key="jobs_editor",
    )

    if st.button("Save feedback & status", type="secondary", key="save_feedback"):
        _save_job_edits(jobs, edited_jobs, show_weight_column)


def _save_job_edits(
    original: pd.DataFrame,
    edited: pd.DataFrame,
    show_weight_column: bool,
) -> None:
    def _fb_to_db(v):
        if v in ("Good", "good"):
            return "good"
        if v in ("Bad", "bad"):
            return "bad"
        return None

    orig_by_id = original.set_index("id")
    fb_updates = []
    status_updates = []
    for _, row in edited.iterrows():
        jid = row["id"]
        if jid not in orig_by_id.index:
            continue
        orig = orig_by_id.loc[jid]

        new_fb = _fb_to_db(row.get("user_feedback"))
        old_fb = _fb_to_db(orig.get("user_feedback"))
        if show_weight_column and "user_weight" in edited.columns:
            new_w = int(row["user_weight"]) if pd.notna(row.get("user_weight")) else int(orig["user_weight"])
        else:
            new_w = int(orig["user_weight"])
        old_w = int(orig["user_weight"])
        if new_fb != old_fb or new_w != old_w:
            fb_updates.append((jid, new_fb, new_w))

        if "status" in edited.columns:
            new_status = str(row.get("status") or "New")
            old_status = str(orig.get("status") or "New")
            if new_status != old_status and new_status in VALID_USER_STATUSES:
                status_updates.append((jid, new_status))

    if not fb_updates and not status_updates:
        st.info("No changes to save.")
        return

    try:
        if fb_updates:
            update_jobs_feedback_batch(fb_updates)
        if status_updates:
            update_jobs_status(status_updates)
        try:
            record_jobs_snapshot_from_db()
        except (OSError, RuntimeError) as snap_err:
            _LOG.warning("snapshot after edit failed: %s", snap_err)
            st.warning(f"Edits saved, but snapshot failed: {snap_err}")
        invalidate_data_cache()
        st.success(
            f"Saved {len(fb_updates)} feedback/weight row(s) and {len(status_updates)} status row(s)."
        )
        st.rerun()
    except (ValueError, RuntimeError, OSError) as e:
        st.error(f"Could not save: {e}")


def _render_token_table_or_fallback() -> None:
    """Always-table view: rows = token categories, columns = sessions.

    Merges per-session telemetry from `data/run_diagnostics.jsonl` with a final
    "last audit" column from `/tmp/improve_audit.json` when present. Width is
    dynamic. Up to 5 most-recent sessions; cells default to "—" when unknown.
    Empty state still renders the row labels so the table shape is visible
    before any /fetchjobs has populated diagnostics.
    """
    st.subheader("Token Usage")

    sessions: list[tuple[str, dict]] = []

    # Source 1 — per-run diagnostics (rich Input/Output/Cache/Productive/Lost).
    runs = _load_diagnostics_runs(limit=20)
    runs_with_tokens = [r for r in runs if any(k in r and r.get(k) is not None for k in _TOKEN_FIELDS)]
    for r in runs_with_tokens[-5:]:
        ts = str(r.get("run_date") or "")[:16].replace("T", " ")
        sessions.append((ts or "—", {f: r.get(f) for f in _TOKEN_FIELDS}))

    # Source 2 — single-shot audit (only productive + lost; main_tokens shown
    # as "Productive" because the audit script aggregates main agent into it).
    audit_path = pathlib.Path("/tmp/improve_audit.json")
    try:
        if audit_path.exists():
            audit = json.loads(audit_path.read_text())
            if (audit.get("verification") or {}).get("ok"):
                prod = audit.get("productive_tokens")
                lost = sum(
                    (v or {}).get("tokens_lost", 0)
                    for v in (audit.get("waste") or {}).values()
                )
                sessions.append(
                    (
                        "last audit",
                        {
                            "productive_tokens": int(prod) if prod is not None else None,
                            "tokens_lost": int(lost) if lost else None,
                        },
                    )
                )
    except (OSError, ValueError, TypeError):
        pass

    sessions = sessions[-5:]  # cap
    sessions = list(reversed(sessions))  # newest leftmost

    empty = not sessions
    if empty:
        # Render a single placeholder column so users can still see the row labels.
        sessions = [("—", {f: None for f in _TOKEN_FIELDS})]

    def _fmt(v):
        if v is None:
            return "—"
        try:
            return f"{int(v):,}"
        except (TypeError, ValueError):
            return str(v)

    df = pd.DataFrame(
        {col: [_fmt(fields.get(f)) for f in _TOKEN_FIELDS] for col, fields in sessions},
        index=[_TOKEN_ROW_LABELS[f] for f in _TOKEN_FIELDS],
    )
    st.dataframe(df, use_container_width=True)
    if empty:
        st.caption(
            "No token data yet. Populated by /fetchjobs (writes per-run rows to "
            "`data/run_diagnostics.jsonl`) and `/improve` (writes a single-shot "
            "audit to `/tmp/improve_audit.json`). Newest session would appear leftmost."
        )
    else:
        st.caption(
            "Rows: token categories. Columns: most-recent sessions (newest leftmost; "
            "up to 5). `last audit` is the one-shot audit JSON; numbered timestamps are "
            "/fetchjobs runs."
        )


def _evidence_str(ev: dict) -> str:
    if not isinstance(ev, dict):
        return str(ev or "")
    metric = ev.get("metric", "")
    value = ev.get("value", "")
    if metric and value != "":
        return f"{metric}={value}"
    return json.dumps(ev, separators=(",", ":"))[:80]


def _patch_preview(patch: dict) -> str:
    if not isinstance(patch, dict):
        return ""
    ptype = patch.get("type", "")
    if ptype == "text_replace":
        new_s = patch.get("new_string", "")
        return f"text → {new_s[:60]}" + ("…" if len(new_s) > 60 else "")
    if ptype == "json_append":
        key = ".".join(map(str, patch.get("key_path", [])))
        items = patch.get("appended_items", [])
        if items and isinstance(items[0], dict):
            label = items[0].get("pattern") or items[0].get("skill") or json.dumps(items[0])[:40]
        else:
            label = json.dumps(items)[:40]
        return f"{key} += {label}"
    if ptype == "json_set":
        key = ".".join(map(str, patch.get("key_path", [])))
        return f"{key} ← {json.dumps(patch.get('new_value'))[:60]}"
    if ptype == "json_remove":
        return f"remove {'.'.join(map(str, patch.get('key_path', [])))}"
    return ptype


_PENDING_COLUMNS = [
    "approve", "dismiss", "severity", "pain_point", "file",
    "summary", "change", "evidence", "staged_at", "change_id",
]


def _render_pending_improvements() -> None:
    st.subheader("Pending Improvements")
    try:
        pending = list_pending_proposals()
    except Exception as e:  # noqa: BLE001 — surface any backend error in UI
        st.error(f"Could not load pending proposals: {e}")
        return

    if not pending:
        st.info(
            "No pending proposals yet. Run /fetchjobs with the auto-audit toggle "
            "on (sidebar), or `/improve --audit-only` manually. The table below "
            "shows the columns you'll see once proposals are staged."
        )
        rows: list[dict] = []
    else:
        st.info(
            f"{len(pending)} proposal(s) pending review. Tick **Approve?** to apply, "
            "**Dismiss?** to reject without applying, then click the matching button below."
        )
        rows = []
        for p in pending:
            rows.append({
                "approve": False,
                "dismiss": False,
                "severity": p.get("severity", ""),
                "pain_point": p.get("pain_point", ""),
                "file": p.get("file_changed", ""),
                "summary": p.get("summary", ""),
                "change": _patch_preview(p.get("patch") or {}),
                "evidence": _evidence_str(p.get("evidence") or {}),
                "staged_at": str(p.get("created_at", ""))[:19],
                "change_id": p.get("change_id", ""),
            })
    df = pd.DataFrame(rows, columns=_PENDING_COLUMNS)

    edited = st.data_editor(
        df,
        column_config={
            "approve": st.column_config.CheckboxColumn("Approve?", default=False),
            "dismiss": st.column_config.CheckboxColumn("Dismiss?", default=False),
            "severity": st.column_config.TextColumn("Sev", width="small"),
            "pain_point": st.column_config.TextColumn("Pain Point"),
            "file": st.column_config.TextColumn("File"),
            "summary": st.column_config.TextColumn("Proposed change", width="large"),
            "change": st.column_config.TextColumn("Patch preview", width="medium"),
            "evidence": st.column_config.TextColumn("Evidence"),
            "staged_at": st.column_config.TextColumn("Staged at", width="small"),
            "change_id": st.column_config.TextColumn("id", width="small", disabled=True),
        },
        disabled=[
            "severity", "pain_point", "file", "summary",
            "change", "evidence", "staged_at", "change_id",
        ],
        hide_index=True,
        use_container_width=True,
        key="pending_improvements_editor",
    )

    c1, c2 = st.columns(2)
    with c1:
        apply_clicked = st.button("Apply approved", type="primary", key="apply_approved_btn")
    with c2:
        dismiss_clicked = st.button("Dismiss selected", key="dismiss_selected_btn")

    if (apply_clicked or dismiss_clicked) and not pending:
        st.warning("No proposals to act on.")
        return

    if apply_clicked or dismiss_clicked:
        applied_n = dismissed_n = errors = 0
        for _, row in edited.iterrows():
            cid = row.get("change_id")
            if not cid:
                continue
            # Dismiss wins when both are checked on the same row.
            if dismiss_clicked and row.get("dismiss"):
                try:
                    dismiss_proposal(cid)
                    dismissed_n += 1
                except Exception as e:  # noqa: BLE001
                    st.error(f"{cid[:30]}: dismiss failed: {e}")
                    errors += 1
                continue
            if apply_clicked and row.get("approve"):
                try:
                    result = apply_proposal(cid)
                except Exception as e:  # noqa: BLE001
                    st.error(f"{cid[:30]}: apply raised: {e}")
                    errors += 1
                    continue
                if result.get("ok"):
                    applied_n += 1
                else:
                    st.error(
                        f"{cid[:30]}: {result.get('reason','blocked')} — "
                        f"{result.get('detail','')}"
                    )
                    errors += 1
        if applied_n:
            st.success(f"Applied {applied_n} proposal(s).")
        if dismissed_n:
            st.success(f"Dismissed {dismissed_n} proposal(s).")
        if applied_n or dismissed_n or errors:
            invalidate_data_cache()
            st.rerun()


_HISTORY_COLUMNS = [
    "revert", "severity", "pain_point", "file", "summary",
    "status", "mode", "commit", "applied_at", "change_id",
]


def _render_improvement_history() -> None:
    st.subheader("Improvement History")
    st.caption(
        "Recently applied /improve changes. Tick **Revert?** and click **Apply reverts** "
        "to undo. Tracked files (`.md`, `.py`) revert via `git revert`; gitignored files "
        "(e.g. `candidate_info.json`) revert via structured inverse-op. "
        "Partial reverts preserve other applied changes."
    )

    rows_to_show = st.number_input(
        "Rows to show",
        min_value=5,
        max_value=200,
        value=10,
        step=5,
        key="history_rows_input",
    )

    try:
        changes = list_applied_changes(limit=int(rows_to_show), include_reverted=True)
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load improvement history: {e}")
        return

    if not changes:
        st.info(
            "No /improve changes applied yet. The table below shows the columns "
            "you'll see once you approve a proposal above. The Revert button at "
            "the bottom becomes active once there's at least one applied change."
        )
        rows: list[dict] = []
        df = pd.DataFrame(rows, columns=_HISTORY_COLUMNS)
        # Still attempt to surface any legacy log entries below the empty preview.
        edited = st.data_editor(
            df,
            column_config={
                "revert": st.column_config.CheckboxColumn("Revert?", default=False),
                "severity": st.column_config.TextColumn("Sev", width="small"),
                "pain_point": st.column_config.TextColumn("Pain Point"),
                "file": st.column_config.TextColumn("File"),
                "summary": st.column_config.TextColumn("Summary", width="large"),
                "status": st.column_config.TextColumn("Status", width="small"),
                "mode": st.column_config.TextColumn("Mode", width="small"),
                "commit": st.column_config.TextColumn("Commit", width="small"),
                "applied_at": st.column_config.TextColumn("Applied at", width="small"),
                "change_id": st.column_config.TextColumn("id", width="small", disabled=True),
            },
            disabled=[c for c in _HISTORY_COLUMNS if c != "revert"],
            hide_index=True,
            use_container_width=True,
            key="improvement_history_editor_empty",
        )
        _render_legacy_improvement_log()
        return

    rows = []
    for c in changes:
        sha = c.get("commit_sha") or ""
        status = "reverted" if c.get("reverted") else "applied"
        rows.append({
            "revert": False,
            "severity": c.get("severity", ""),
            "pain_point": c.get("pain_point", ""),
            "file": c.get("file_changed", ""),
            "summary": c.get("summary", ""),
            "status": status,
            "mode": c.get("revert_mode", ""),
            "commit": sha[:7] if sha else "—",
            "applied_at": str(c.get("applied_at", ""))[:19],
            "change_id": c.get("change_id", ""),
        })
    df = pd.DataFrame(rows, columns=_HISTORY_COLUMNS)

    edited = st.data_editor(
        df,
        column_config={
            "revert": st.column_config.CheckboxColumn("Revert?", default=False),
            "severity": st.column_config.TextColumn("Sev", width="small"),
            "pain_point": st.column_config.TextColumn("Pain Point"),
            "file": st.column_config.TextColumn("File"),
            "summary": st.column_config.TextColumn("Summary", width="large"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "mode": st.column_config.TextColumn("Mode", width="small"),
            "commit": st.column_config.TextColumn("Commit", width="small"),
            "applied_at": st.column_config.TextColumn("Applied at", width="small"),
            "change_id": st.column_config.TextColumn("id", width="small", disabled=True),
        },
        disabled=[
            "severity", "pain_point", "file", "summary", "status",
            "mode", "commit", "applied_at", "change_id",
        ],
        hide_index=True,
        use_container_width=True,
        key="improvement_history_editor",
    )

    if st.button("Apply reverts", key="apply_reverts_btn"):
        reverted_n = errors = 0
        for _, row in edited.iterrows():
            if not row.get("revert"):
                continue
            if row.get("status") == "reverted":
                continue  # already reverted — silently skip
            cid = row.get("change_id")
            if not cid:
                continue
            try:
                result = revert_change(cid)
            except Exception as e:  # noqa: BLE001
                st.error(f"{cid[:30]}: revert raised: {e}")
                errors += 1
                continue
            if result.get("ok"):
                reverted_n += 1
            else:
                detail = result.get("detail", "")
                conflicting = result.get("conflicting_files") or []
                conflict_hint = f" — conflicting files: {conflicting}" if conflicting else ""
                st.error(
                    f"{cid[:30]}: {result.get('reason','blocked')} — {detail}{conflict_hint}"
                )
                errors += 1
        if reverted_n:
            st.success(f"Reverted {reverted_n} change(s).")
        if reverted_n or errors:
            invalidate_data_cache()
            st.rerun()


def _render_legacy_improvement_log() -> None:
    """Used only when improve_changes.jsonl is empty — surfaces the legacy improve_log."""
    with st.expander("Legacy /improve log (improve_log.jsonl)", expanded=False):
        imp_log = _DATA_DIR / "improve_log.jsonl"
        try:
            if imp_log.exists():
                lines = [l for l in imp_log.read_text().splitlines() if l.strip()][-5:]
                rows = []
                for l in reversed(lines):
                    try:
                        e = json.loads(l)
                    except Exception:
                        continue
                    summ = str(e.get("summary", ""))
                    if len(summ) > 80:
                        summ = summ[:77] + "..."
                    rows.append({
                        "timestamp": str(e.get("timestamp", ""))[:19],
                        "pain_point": e.get("pain_point", ""),
                        "severity": e.get("severity", ""),
                        "file_changed": e.get("file_changed", ""),
                        "summary": summ,
                    })
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                else:
                    st.info("No /improve actions applied yet. Run /improve to start the loop.")
            else:
                st.info("No /improve actions applied yet. Run /improve to start the loop.")
        except Exception:
            st.info("No /improve actions applied yet. Run /improve to start the loop.")


def render_analytics(jobs: pd.DataFrame) -> None:
    """Analytics tab: metrics row, Match Quality histogram, Token table, Latest improvement."""
    if jobs is None or jobs.empty:
        st.info("Database empty. Run `/fetchjobs` to populate.")
        return

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Total Roles Found", len(jobs))
    with m2:
        high_tier = len(jobs[jobs["score"] >= 85]) if "score" in jobs.columns else 0
        st.metric("High-Moat Matches (85+)", high_tier)
    with m3:
        avg_score = round(jobs["score"].mean(), 1) if "score" in jobs.columns else 0
        st.metric("Average Match Quality", f"{avg_score}%")

    if "score" in jobs.columns:
        fig_match = px.histogram(
            jobs,
            x="score",
            nbins=10,
            title="Match Quality Distribution",
            labels={"score": "Match Score"},
            color_discrete_sequence=["#636EFA"],
        )
        fig_match.update_layout(height=260, margin=dict(l=20, r=20, t=36, b=20))
        st.plotly_chart(fig_match, use_container_width=True)

    _render_token_table_or_fallback()
    st.divider()
    _render_pending_improvements()
    st.divider()
    _render_improvement_history()


def render_historical_runs(jobs: pd.DataFrame) -> None:
    """Historical Runs page: last-5 diagnostics table, Domain Distribution, column glossary."""
    runs = _load_diagnostics_runs(limit=200)
    st.subheader("Last 5 /fetchjobs runs")
    if not runs:
        st.caption("Run /fetchjobs to populate history.")
    else:
        last5 = list(reversed(runs[-5:]))  # newest first
        base_cols = ["run_date", "valid_jobs", "webfetch_calls", "calls_per_job"]
        token_cols_present = [f for f in _TOKEN_FIELDS if any(f in r and r.get(f) is not None for r in last5)]
        columns = base_cols + token_cols_present
        records = []
        for r in last5:
            vj = r.get("valid_jobs")
            wf = r.get("webfetch_calls")
            try:
                vj_i = int(vj) if vj is not None else None
                wf_i = int(wf) if wf is not None else None
            except (TypeError, ValueError):
                vj_i = wf_i = None
            cpj = (wf_i / max(vj_i, 1)) if (wf_i is not None and vj_i is not None) else None
            row = {
                "run_date": str(r.get("run_date") or "—")[:19],
                "valid_jobs": vj_i if vj_i is not None else "—",
                "webfetch_calls": wf_i if wf_i is not None else "—",
                "calls_per_job": round(cpj, 2) if cpj is not None else "—",
            }
            for f in token_cols_present:
                v = r.get(f)
                row[f] = v if v is not None else "—"
            records.append(row)
        df = pd.DataFrame(records, columns=columns)
        st.dataframe(df, use_container_width=True, hide_index=True)

    if jobs is not None and not jobs.empty and "theme" in jobs.columns:
        st.subheader("Domain Distribution (current job board)")
        theme_counts = jobs["theme"].value_counts()
        fig_domain = px.bar(
            x=theme_counts.index,
            y=theme_counts.values,
            title=None,
            labels={"x": "Domain", "y": "Count"},
            color_discrete_sequence=["#00CC96"],
        )
        fig_domain.update_layout(
            height=280,
            margin=dict(l=20, r=20, t=10, b=20),
            xaxis_tickangle=-25,
        )
        st.plotly_chart(fig_domain, use_container_width=True)

    # Column glossary, built dynamically from columns rendered above.
    glossary_cols = set()
    if runs:
        glossary_cols.update(["run_date", "valid_jobs", "webfetch_calls", "calls_per_job"])
        for r in runs[-5:]:
            for f in _TOKEN_FIELDS:
                if f in r and r.get(f) is not None:
                    glossary_cols.add(f)
    if glossary_cols:
        st.subheader("Column glossary")
        for col in [k for k in _GLOSSARY.keys() if k in glossary_cols]:
            st.caption(f"**{col}** — {_GLOSSARY[col]}")
