"""
Parse wisdom text into a one-column intelligence table.
Shared so the app and judge script stay consistent without importing Streamlit.
"""
import re
from typing import List


_SPLIT_RE = re.compile(r"(?:\n+|(?<=[.!?])\s+)")
_QUALITY_TOKENS = (
    "demand",
    "hiring",
    "role",
    "roles",
    "scientific",
    "bayesian",
    "causal",
    "inference",
    "ml",
    "ai",
    "data",
    "healthcare",
    "fintech",
    "energy",
    "ats",
    "greenhouse",
    "lever",
    "staff",
    "principal",
)


def _is_actionable_fragment(text: str) -> bool:
    cleaned = text.strip().lstrip("-* ").strip()
    if len(cleaned) < 24:
        return False
    lower = cleaned.lower()
    if lower.startswith(("awaiting", "n/a", "none", "unknown")):
        return False
    if not any(tok in lower for tok in _QUALITY_TOKENS):
        return False
    return True


def wisdom_text_to_intelligence_rows(wisdom_text: str) -> List[dict]:
    """
    Parse wisdom into rows with a single key: Intelligence.
    Each row is an independent bullet-style insight.
    """
    if not wisdom_text or not str(wisdom_text).strip():
        return []
    raw = str(wisdom_text).strip()
    if raw.lower().startswith("awaiting"):
        return []

    parts = [p.strip() for p in _SPLIT_RE.split(raw) if p and p.strip()]
    rows: List[dict] = []
    seen = set()
    for part in parts:
        if not _is_actionable_fragment(part):
            continue
        clean = part.rstrip(".") + "."
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"Intelligence": f"- {clean}"})

    if not rows and raw:
        fallback = raw.rstrip(".") + "."
        rows.append({"Intelligence": f"- {fallback}"})
    return rows
