"""Merge the plan's settled verdicts with the check results into the site.

The results come from a job that ran third-party code, so every row goes
through `schema.validate_result`; anything that does not pass becomes
"unknown" (or the previous verdict for the same version, if there is one).

    python3 -m health.publish --plan plan.json --results DIR --out DIST
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import KINDS, UNKNOWN, validate_result

SITE = Path(__file__).parent.parent / "site"
MAX_RESULT_LINE = 16_384


def read_results(results_dir: Path, assigned: dict) -> dict[str, dict]:
    """Valid rows by module; the first valid row for a module wins."""
    rows: dict[str, dict] = {}
    rejected = 0
    for path in sorted(results_dir.rglob("*.jsonl")):
        with path.open(errors="replace") as f:
            for line in f:
                if len(line) > MAX_RESULT_LINE:
                    rejected += 1
                    continue
                try:
                    row = validate_result(json.loads(line), assigned)
                except ValueError:
                    row = None
                if row is None:
                    rejected += 1
                elif row["module"] not in rows:
                    rows[row["module"]] = row
    if rejected:
        print(f"ignored {rejected} result lines that did not validate", file=sys.stderr)
    return rows


def merge(plan: dict, results: dict[str, dict], now: str) -> dict:
    modules = dict(plan["decided"])
    for module, job in plan["assigned"].items():
        name = job["name"]
        r = results.get(module)
        if r is None or r["kind"] == UNKNOWN:
            if name in plan.get("fallback", {}):
                modules[name] = plan["fallback"][name]
                continue
            r = r or {"kind": UNKNOWN, "errors": [], "log": "", "imported": 0,
                      "message": "The check produced no usable result."}
        modules[name] = {
            "name": name,
            "version": job["version"],
            "created_at": job["created_at"],
            "target": job["target"],
            "kind": r["kind"],
            "blockers": job["blockers"],
            "add": job["add"],
            "errors": r["errors"],
            "message": r["message"],
            "log": r["log"],
            "imported": r.get("imported", 0),
            "moon": plan["moon"],
            "checked_at": now,
            "key": job["key"],
        }
    counts = Counter(m["kind"] for m in modules.values())
    return {
        "generated_at": now,
        "moon": plan["moon"],
        "index_fetched_at": plan["index_fetched_at"],
        "counts": {k: counts.get(k, 0) for k in KINDS},
        "modules": dict(sorted(modules.items(), key=lambda kv: kv[0].lower())),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--formats", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    results = read_results(args.results, plan["assigned"]) if args.results.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = merge(plan, results, now)

    if args.out.exists():
        shutil.rmtree(args.out)
    shutil.copytree(SITE, args.out)
    (args.out / "status.json").write_text(json.dumps(status, separators=(",", ":")))
    shutil.copy(args.formats, args.out / "formats.json")
    print(" ".join(f"{k}={v}" for k, v in status["counts"].items()), file=sys.stderr)


if __name__ == "__main__":
    main()
