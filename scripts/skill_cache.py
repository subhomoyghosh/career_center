"""
Content-addressed cache for /improve skill file analysis.

check <path1> [path2 ...]
    For each path: compute SHA256, compare to cache, validate entry.
    Prints JSON: {"cache_hits": {rel_path: entry}, "cache_misses": [rel_path]}
    Read-only — never writes to disk.

update '<json>'
    Receives {rel_path: entry} JSON string (LLM-generated analysis).
    Merges into data/skill_analysis_cache.json atomically.
    Prints: {"updated": [...], "total_entries": N}
"""

import argparse
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "skill_analysis_cache.json"
CACHE_VERSION = 1

REQUIRED_ENTRY_FIELDS = frozenset({
    "sha256", "size_bytes", "analyzed_at", "token_count_approx",
    "sections", "tier1_candidates_count", "tier2_candidates_count",
    "tier3_candidates_count", "pain_points_seen", "summary",
})


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {"version": CACHE_VERSION, "entries": {}}
    try:
        data = json.loads(CACHE_PATH.read_bytes())
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": CACHE_VERSION, "entries": {}}
        if data.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "entries": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": CACHE_VERSION, "entries": {}}


def _save_cache(data: dict) -> None:
    tmp = str(CACHE_PATH) + ".tmp_skill_cache"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


def _to_rel(path_str: str) -> str:
    try:
        return str(pathlib.Path(path_str).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return path_str


def _validate_entry(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    if not REQUIRED_ENTRY_FIELDS.issubset(entry.keys()):
        return False
    sha = entry.get("sha256", "")
    if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)):
        return False
    for int_field in ("size_bytes", "token_count_approx",
                      "tier1_candidates_count", "tier2_candidates_count", "tier3_candidates_count"):
        v = entry.get(int_field)
        if not (isinstance(v, int) and v >= 0):
            return False
    if not (isinstance(entry.get("analyzed_at"), str) and entry["analyzed_at"]):
        return False
    if not (isinstance(entry.get("summary"), str) and entry["summary"]):
        return False
    if not isinstance(entry.get("sections"), list):
        return False
    if not isinstance(entry.get("pain_points_seen"), list):
        return False
    return True


def cmd_check(paths: list) -> None:
    cache = _load_cache()
    entries = cache.get("entries", {})
    cache_hits = {}
    cache_misses = []

    for path_str in paths:
        abs_path = pathlib.Path(path_str)
        if not abs_path.is_absolute():
            abs_path = PROJECT_ROOT / path_str
        rel = _to_rel(str(abs_path))

        if not abs_path.exists():
            cache_misses.append(rel)
            continue
        try:
            current_hash = _file_sha256(str(abs_path))
        except OSError:
            cache_misses.append(rel)
            continue

        entry = entries.get(rel)
        if entry is None or not _validate_entry(entry) or entry["sha256"] != current_hash:
            cache_misses.append(rel)
        else:
            cache_hits[rel] = entry

    print(json.dumps({"cache_hits": cache_hits, "cache_misses": cache_misses}, indent=2))


def cmd_update(json_arg: str) -> None:
    try:
        new_entries = json.loads(json_arg)
    except json.JSONDecodeError as exc:
        print(f"skill_cache update: invalid JSON — {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(new_entries, dict):
        print("skill_cache update: expected a JSON object {rel_path: entry}", file=sys.stderr)
        sys.exit(1)

    cache = _load_cache()
    updated = []
    for rel_path, entry in new_entries.items():
        if not isinstance(entry, dict) or not entry.get("sha256") or not entry.get("summary"):
            print(f"skill_cache update: skipping malformed entry for {rel_path!r}", file=sys.stderr)
            continue
        # stamp analyzed_at if missing
        if not entry.get("analyzed_at"):
            entry["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        cache["entries"][rel_path] = entry
        updated.append(rel_path)

    _save_cache(cache)
    print(json.dumps({"updated": updated, "total_entries": len(cache["entries"])}))


def main() -> None:
    p = argparse.ArgumentParser(description="Skill file analysis cache")
    sub = p.add_subparsers(dest="cmd", required=True)

    check_p = sub.add_parser("check", help="Check paths against cache")
    check_p.add_argument("paths", nargs="+")

    update_p = sub.add_parser("update", help="Write analysis entries to cache")
    update_p.add_argument("json_arg", help="JSON string: {rel_path: entry}")

    args = p.parse_args()
    if args.cmd == "check":
        cmd_check(args.paths)
    elif args.cmd == "update":
        cmd_update(args.json_arg)


if __name__ == "__main__":
    main()
