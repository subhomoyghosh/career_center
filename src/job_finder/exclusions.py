"""Apply config-driven exclusion lists to scored job dicts.

Returns (kept, dropped) where dropped is [{job_meta, reasons:[str]}] for diag.
Idempotent — repeat application on already-normalized lists is a no-op.
"""
from job_finder.config import get_exclusions


def apply_exclusions(jobs: list, config: dict) -> tuple:
    companies, areas, pairs = get_exclusions(config)
    if not (companies or areas or pairs):
        return list(jobs), []
    kept = []
    dropped = []
    for j in jobs:
        c = str(j.get("company") or "").strip().lower()
        t = str(j.get("theme") or "").strip().lower()
        reasons = []
        if c and c in companies:
            reasons.append("excluded_companies")
        if t:
            for a in areas:
                if a and a in t:
                    reasons.append(f"excluded_areas:{a}")
                    break
        for pc, pa in pairs:
            if pc and pa and c == pc and pa in t:
                reasons.append(f"excluded_pairs:{pc}:{pa}")
                break
        if reasons:
            dropped.append({
                "company": j.get("company"),
                "title": j.get("title"),
                "theme": j.get("theme"),
                "reasons": reasons,
            })
        else:
            kept.append(j)
    return kept, dropped
