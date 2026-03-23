import pandas as pd
import plotly.express as px
import streamlit as st

from job_finder.history import record_jobs_snapshot_from_db
from job_finder.persistence import update_jobs_feedback_batch

from job_finder.ui_helpers import wisdom_to_table


def render_main_content(profile: dict, jobs: pd.DataFrame, show_wisdom: bool, show_weight_column: bool) -> None:
    # Main: center = filters + job table, right = intelligence
    domains = (
        ["All"] + sorted(jobs["theme"].dropna().unique().tolist())
        if not jobs.empty and "theme" in jobs.columns
        else ["All"]
    )
    if "domain_filter" not in st.session_state:
        st.session_state["domain_filter"] = "All"

    col_left, col_right = st.columns([2, 1])

    with col_right:
        if show_wisdom:
            st.subheader("Market Intelligence")
            wisdom_text = profile.get("wisdom") or "Awaiting next /fetchjobs run..."
            wisdom_df = wisdom_to_table(wisdom_text)
            if not wisdom_df.empty:
                st.dataframe(
                    wisdom_df,
                    column_config={
                        "Intelligence": st.column_config.TextColumn(
                            "Intelligence",
                            width="large",
                        ),
                    },
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                st.info(wisdom_text)

    with col_left:
        if not jobs.empty:
            # Filters: domain first, then source
            selected_domain = st.selectbox(
                "Filter by domain",
                domains,
                key="domain_filter",
                help="Show only jobs in this domain. Distributions below reflect the selection.",
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

            # Same-genre distributions: one below the other (both use filtered jobs)
            fig_match = px.histogram(
                jobs,
                x="score",
                nbins=10,
                title="Match Quality Distribution",
                labels={"score": "Match Score"},
                color_discrete_sequence=["#636EFA"],
            )
            fig_match.update_layout(height=220, margin=dict(l=20, r=20, t=36, b=20))
            st.plotly_chart(fig_match, use_container_width=True)

            if "theme" in jobs.columns:
                theme_counts = jobs["theme"].value_counts()
                fig_domain = px.bar(
                    x=theme_counts.index,
                    y=theme_counts.values,
                    title="Domain Distribution",
                    labels={"x": "Domain", "y": "Count"},
                    color_discrete_sequence=["#00CC96"],
                )
                fig_domain.update_layout(
                    height=220,
                    margin=dict(l=20, r=20, t=36, b=20),
                    xaxis_tickangle=-25,
                )
                st.plotly_chart(fig_domain, use_container_width=True)

            st.subheader("Job board")

            # Job table only (feedback and weights; unchanged logic)
            display_cols = [c for c in jobs.columns if c != "user_weight" or show_weight_column]
            jobs_display = jobs[display_cols].copy()
            col_config = {
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
                "user_feedback": st.column_config.SelectboxColumn(
                    "Good / Bad",
                    options=["—", "Good", "Bad"],
                    required=True,
                    help="Mark roles that are a strong fit (Good) or poor fit (Bad). Used to nudge future search.",
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
                "id",
                "company",
                "title",
                "link",
                "score",
                "theme",
                "rationale",
                "source",
            ]
            if not show_weight_column:
                disabled_cols = [c for c in disabled_cols if c in jobs_display.columns]

            edited_jobs = st.data_editor(
                jobs_display,
                column_config=col_config,
                hide_index=True,
                use_container_width=True,
                disabled=disabled_cols,
                key="jobs_editor",
            )

            if st.button("Save feedback & weights", type="secondary", key="save_feedback"):

                def _fb_to_db(v):
                    if v in ("Good", "good"):
                        return "good"
                    if v in ("Bad", "bad"):
                        return "bad"
                    return None

                updates = []
                for _, row in edited_jobs.iterrows():
                    jid = row["id"]
                    fb = _fb_to_db(row.get("user_feedback"))
                    w = (
                        int(row["user_weight"])
                        if "user_weight" in edited_jobs.columns
                        and pd.notna(row.get("user_weight"))
                        else int(
                            jobs.loc[jobs["id"] == jid, "user_weight"].iloc[0]
                        )
                    )
                    updates.append((jid, fb, w))

                try:
                    update_jobs_feedback_batch(updates)
                    try:
                        record_jobs_snapshot_from_db()
                    except Exception:
                        pass
                    st.success("Feedback and weights saved. Next /fetchjobs will use them to nudge search.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")
        else:
            st.warning("Database empty. Run the `/fetchjobs` command to begin the autonomous research cycle.")

