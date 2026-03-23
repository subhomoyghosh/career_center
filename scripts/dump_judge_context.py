#!/usr/bin/env python3
"""
Emit JSON context for in-chat LLM-as-judge (Cursor skill: evaluate-nudge-and-wisdom).
Run from project root: uv run python scripts/dump_judge_context.py
Does not print judge prompts — only structured evidence for the agent to analyze.
"""
import json
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.judge_context import build_judge_report


def main() -> None:
    r = build_judge_report()
    print(json.dumps(r, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
