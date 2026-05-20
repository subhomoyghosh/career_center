import streamlit as st

from job_finder.ui_data import load_data, profile_from_config
from job_finder.ui_job_board import (
    render_analytics,
    render_historical_runs,
    render_home_board,
)
from job_finder.ui_profile_editor import render_profile_sidebar


CUSTOM_CSS = """
<style>
.main { background-color: #f8f9fa; }
.stMetric { background-color: #ffffff; border-radius: 10px; padding: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
section[data-testid="stMain"] [data-testid="stVerticalBlock"] > [data-testid="stVerticalBlock"]:has(> [data-testid="stMetric"]) {
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

    config, jobs = load_data()
    if not config:
        st.warning(
            "No candidate profile yet. Run `/setup` in chat to infer one from your resume PDF in "
            "`data/`, or paste a JSON into `data/candidate_info.json`. Then refresh this page."
        )
        st.caption("First run? `python3 orchestrator.py` initializes `data/` and the jobs DB.")
        return

    profile = profile_from_config(config)
    show_wisdom, show_weight_column = render_profile_sidebar(profile, config)

    def _home() -> None:
        st.title("Career Command Center")
        st.caption("Identity-driven job research and match tracking")
        render_home_board(
            profile=profile,
            jobs=jobs,
            show_wisdom=show_wisdom,
            show_weight_column=show_weight_column,
        )

    def _analytics() -> None:
        st.title("Analytics")
        render_analytics(jobs)

    def _historical() -> None:
        st.title("Historical Runs")
        render_historical_runs(jobs)

    home_page = st.Page(_home, title="Home", default=True)
    analytics_page = st.Page(_analytics, title="Analytics")
    historical_page = st.Page(_historical, title="Historical Runs")
    nav = st.navigation([home_page, analytics_page, historical_page])
    nav.run()
