import streamlit as st

from job_finder.config import save_config
from job_finder.paths import resolve_active_config_path


def render_profile_sidebar(profile: dict, config: dict) -> tuple[bool, bool]:
    with st.sidebar:
        st.header("Candidate Configuration")
        moat_list = profile.get("scientific_moat") or []
        moat_list = moat_list if isinstance(moat_list, list) else [moat_list] if moat_list else []
        eng_list = profile.get("engineering_stack") or []
        eng_list = eng_list if isinstance(eng_list, list) else [eng_list] if eng_list else []
        priority_list = profile.get("priority_domains") or profile.get("priority_industries") or []
        noise_list = profile.get("noise_keywords") or []
        noise_list = noise_list if isinstance(noise_list, list) else [noise_list] if noise_list else []
        search_targets_list = profile.get("search_targets") or []
        search_targets_list = (
            search_targets_list
            if isinstance(search_targets_list, list)
            else [search_targets_list]
            if search_targets_list
            else []
        )

        new_pitch = st.text_area(
            "Strategic Pitch",
            value=profile.get("core_identity", ""),
            height=100,
            help="High-level professional narrative.",
            key="core_identity_pitch",
        )
        new_moat = st.text_input(
            "Scientific moat (comma-sep)",
            value=", ".join(moat_list),
            help="5–7 rare, high-barrier skills.",
        )
        new_eng = st.text_input(
            "Engineering stack (comma-sep)",
            value=", ".join(eng_list),
            help="Tools and infra e.g. Python, C++, AWS.",
        )
        new_seniority = st.text_input(
            "Target seniority (use forward slashes)",
            value=profile.get("target_seniority", "Staff / Principal / Lead"),
        )
        countries = ["USA", "UK", "Canada", "Remote", "Europe"]
        curr_country = profile.get("target_country") or "USA"
        try:
            country_index = countries.index(curr_country) if curr_country in countries else 0
        except (ValueError, TypeError):
            country_index = 0
        new_country = st.selectbox(
            "Target Country",
            countries,
            index=country_index,
            key="target_country_select",
        )
        new_keys = st.text_input("Search keywords (golden_keywords)", value=profile.get("golden_keywords", ""))
        new_noise = st.text_input(
            "Noise keywords (comma-sep)",
            value=", ".join(noise_list),
            help="Roles to filter out e.g. Junior, Intern.",
        )
        new_priority = st.text_input(
            "Priority domains (comma-sep)",
            value=", ".join(priority_list) if isinstance(priority_list, list) else str(priority_list),
        )
        new_search_targets = st.text_input(
            "Search targets / ATS sites (comma-sep)",
            value=", ".join(search_targets_list)
            if isinstance(search_targets_list, list)
            else str(search_targets_list),
        )

        st.divider()
        st.caption("Dashboard")
        show_wisdom = st.toggle("Show Market Intelligence", value=True)
        show_weight_column = st.toggle(
            "Show weight column (0–100)",
            value=True,
            help="Per-job weight for next /fetchjobs.",
        )

        if st.button("Update Profile", use_container_width=True, type="primary", key="save_recalibrate"):
            def to_list(s):
                return [x.strip() for x in str(s).split(",") if x.strip()]

            payload = {
                "core_identity": new_pitch,
                "scientific_moat": to_list(new_moat),
                "engineering_stack": to_list(new_eng),
                "target_seniority": new_seniority,
                "target_country": new_country,
                "golden_keywords": new_keys,
                "noise_keywords": to_list(new_noise),
                "priority_domains": to_list(new_priority),
                "search_targets": to_list(new_search_targets),
                "wisdom": config.get("wisdom") or config.get("market_wisdom", ""),
                "peer_companies": profile.get("peer_companies", []),
            }
            try:
                save_config(payload)
                st.success(f"Configuration synced to {resolve_active_config_path()}!")
                st.rerun()
            except OSError as e:
                st.error(f"Could not save: {e}")

    return show_wisdom, show_weight_column

