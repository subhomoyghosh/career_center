import streamlit as st

from job_finder.config import save_config
from job_finder.paths import resolve_active_config_path
from job_finder.ui_data import invalidate_data_cache
from job_finder.ui_helpers import wisdom_to_table


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
        excluded_companies_list = profile.get("excluded_companies") or []
        excluded_areas_list = profile.get("excluded_areas") or []
        excluded_pairs_list = profile.get("excluded_pairs") or []
        allowed_metros_list = profile.get("allowed_metros") or []
        allowed_metros_list = allowed_metros_list if isinstance(allowed_metros_list, list) else [allowed_metros_list]
        allowed_work_modes_list = profile.get("allowed_work_modes") or []
        allowed_work_modes_list = allowed_work_modes_list if isinstance(allowed_work_modes_list, list) else [allowed_work_modes_list]
        remote_anywhere_ok = bool(profile.get("remote_anywhere_ok", True))

        countries = ["USA", "UK", "Canada", "Remote", "Europe"]
        curr_country = profile.get("target_country") or "USA"
        try:
            country_index = countries.index(curr_country) if curr_country in countries else 0
        except (ValueError, TypeError):
            country_index = 0

        with st.form("candidate_profile_form", clear_on_submit=False, border=False):
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
            new_country = st.selectbox(
                "Target Country",
                countries,
                index=country_index,
                key="target_country_select",
            )
            new_keys = st.text_input(
                "Search keywords (golden_keywords)",
                value=profile.get("golden_keywords", ""),
            )
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

            with st.expander("Exclusions", expanded=False):
                new_excl_companies = st.text_input(
                    "Excluded companies (comma-sep)",
                    value=", ".join(excluded_companies_list),
                    help=(
                        "Case-insensitive EXACT match on company. "
                        "Example: 'Meta, X Corp'. "
                        "excluded_* always WINS over peer_companies."
                    ),
                    key="excluded_companies_input",
                )
                new_excl_areas = st.text_input(
                    "Excluded areas (comma-sep)",
                    value=", ".join(excluded_areas_list),
                    help=(
                        "Case-insensitive SUBSTRING match on the job theme. "
                        "Example: 'safety, trust' drops any theme containing those words. "
                        "Empty entries silently dropped. Exclusion wins over priority_domains."
                    ),
                    key="excluded_areas_input",
                )
                new_excl_pairs = st.text_input(
                    "Excluded company:area pairs (comma-sep, format COMPANY:AREA)",
                    value=", ".join(excluded_pairs_list),
                    help=(
                        "Paired AND-match: drops a job only when BOTH company AND theme match. "
                        "Example: 'OpenAI:safety' keeps OpenAI engineering AND other companies' safety roles."
                    ),
                    key="excluded_pairs_input",
                )

            with st.expander("Location & Work Mode", expanded=False):
                new_allowed_metros = st.text_input(
                    "Allowed metros (comma-sep, fuzzy regions)",
                    value=", ".join(allowed_metros_list),
                    help=(
                        "Fuzzy region names — LLM uses geographic knowledge to judge city membership. "
                        "Examples: 'San Francisco Bay Area, CA', 'Greater Boston', 'NYC metro'. "
                        "Mountain View / Oakland / Palo Alto are recognized as SF Bay Area. "
                        "Empty = no metro constraint (any location OK)."
                    ),
                    key="allowed_metros_input",
                )
                new_allowed_work_modes = st.multiselect(
                    "Allowed work modes",
                    options=["remote", "hybrid", "onsite"],
                    default=[m for m in allowed_work_modes_list if m in {"remote", "hybrid", "onsite"}],
                    help=(
                        "Hard filter on the JD's stated work mode. "
                        "Empty (no chips selected) = no mode constraint (all modes pass through). "
                        "Picking all three has the same effect as leaving it empty."
                    ),
                    key="allowed_work_modes_input",
                )
                new_remote_anywhere_ok = st.toggle(
                    "Remote roles bypass the metro check",
                    value=remote_anywhere_ok,
                    help=(
                        "If ON: fully-remote postings are kept regardless of location. "
                        "If OFF: even remote roles must be in an allowed_metros region "
                        "(rare — usually used when 'remote' actually means 'remote within a specific metro')."
                    ),
                    key="remote_anywhere_ok_toggle",
                )

            st.divider()
            new_auto_audit = st.toggle(
                "Auto-improve audit after each /fetchjobs",
                value=bool(config.get("auto_improve_audit_enabled", False)),
                help=(
                    "When /fetchjobs finishes, automatically runs /improve --audit-only. "
                    "Proposals appear in Analytics → Pending Improvements. "
                    "No edits are auto-applied; every change still requires your click."
                ),
                key="auto_improve_audit_toggle",
            )

            submitted = st.form_submit_button(
                "Update Profile",
                use_container_width=True,
                type="primary",
            )

        if submitted:
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
                "excluded_companies": to_list(new_excl_companies),
                "excluded_areas": to_list(new_excl_areas),
                "excluded_pairs": to_list(new_excl_pairs),
                "allowed_metros": to_list(new_allowed_metros),
                "allowed_work_modes": list(new_allowed_work_modes),
                "remote_anywhere_ok": bool(new_remote_anywhere_ok),
                "auto_improve_audit_enabled": bool(new_auto_audit),
            }
            try:
                save_config(payload)
                invalidate_data_cache()
                # Drop the data_editor's positional edit state; rows may have shifted.
                st.session_state.pop("jobs_editor", None)
                st.success(f"Configuration synced to {resolve_active_config_path()}!")
                st.rerun()
            except OSError as e:
                st.error(f"Could not save: {e}")

        st.divider()
        st.caption("Dashboard")
        show_wisdom = st.toggle("Show Market Intelligence", value=True)
        show_weight_column = st.toggle(
            "Show weight column (0–100)",
            value=True,
            help="Per-job weight for next /fetchjobs.",
        )

        if show_wisdom:
            st.divider()
            st.subheader("Market Intelligence")
            wisdom_text = profile.get("wisdom") or "Awaiting next /fetchjobs run..."
            wisdom_df = wisdom_to_table(wisdom_text)
            if not wisdom_df.empty:
                st.dataframe(
                    wisdom_df,
                    column_config={
                        "Intelligence": st.column_config.TextColumn("Intelligence", width="large"),
                    },
                    hide_index=True,
                    use_container_width=True,
                )
            else:
                st.info(wisdom_text)

    return show_wisdom, show_weight_column
