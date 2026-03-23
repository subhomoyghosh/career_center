import pandas as pd

from job_finder.wisdom_intel import wisdom_text_to_intelligence_rows


def wisdom_to_table(wisdom_text: str) -> pd.DataFrame:
    """Parse any wisdom blob into one-column intelligence table."""
    rows = wisdom_text_to_intelligence_rows(wisdom_text)
    if not rows:
        return pd.DataFrame(columns=["Intelligence"])
    return pd.DataFrame(rows)


def source_from_link(link: str) -> str:
    """Derive job board/source from URL so LinkedIn and ATS are visible."""
    if not link or not isinstance(link, str):
        return "—"
    link_lower = link.lower()
    if "linkedin.com" in link_lower:
        return "LinkedIn"
    if "lever.co" in link_lower:
        return "Lever"
    if "greenhouse.io" in link_lower or "boards.greenhouse" in link_lower:
        return "Greenhouse"
    if "ashbyhq.com" in link_lower:
        return "Ashby"
    if "workday.com" in link_lower:
        return "Workday"
    if "indeed.com" in link_lower:
        return "Indeed"
    if "glassdoor.com" in link_lower:
        return "Glassdoor"
    if "ziprecruiter.com" in link_lower:
        return "ZipRecruiter"
    return "Other"

