import streamlit as st

from job_finder.ui_data import load_data, profile_from_config
from job_finder.ui_job_board import render_main_content
from job_finder.ui_profile_editor import render_profile_sidebar


CUSTOM_CSS = """
<style>
.main { background-color: #f8f9fa; }
.stMetric { background-color: #ffffff; border-radius: 10px; padding: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlock"] {
    background-color: #ffffff;
    border-radius: 8px;
    border: 1px solid #e9ecef;
    padding: 0.75rem 1rem;
    margin-bottom: 0.5rem;
}
.profile-section { font-size: 0.9rem; }
</style>
"""


def render_app() -> None:
    st.set_page_config(
        layout="wide",
        page_title="Career Command Center",
        page_icon=None,
    )
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    st.title("Career Command Center")
    st.caption("Identity-driven job research and match tracking")

    config, jobs = load_data()
    profile = profile_from_config(config) if config else {}

    if config:
        # Top Level Metrics
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("Total Roles Found", len(jobs))
        with m2:
            high_tier = len(jobs[jobs["score"] >= 85]) if not jobs.empty else 0
            st.metric("High-Moat Matches (85+)", high_tier)
        with m3:
            avg_score = round(jobs["score"].mean(), 1) if not jobs.empty else 0
            st.metric("Average Match Quality", f"{avg_score}%")

        show_wisdom, show_weight_column = render_profile_sidebar(profile, config)
        render_main_content(
            profile=profile,
            jobs=jobs,
            show_wisdom=show_wisdom,
            show_weight_column=show_weight_column,
        )
    else:
        st.error("Missing candidate profile. Run orchestrator.py, then add data/candidate_info.json.")

