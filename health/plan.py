"""Decide what the check job has to run.

Reads the registry index, predicts each module's outcome from manifest
formats, settles the verdicts that need no build, and splits the rest into
shards. Runs no third-party code.

    python3 -m health.plan --moon "$(moon version --all)" --previous URL --out DIR
"""

import argparse
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import formats as fm
from .index import Index, read
from .resolve import predict
from .schema import DEP_MANIFEST, OWN_MANIFEST, UNKNOWN, UNRESOLVABLE

PER_SHARD = 150
MAX_SHARDS = 16
FETCH_ROUNDS = 6


def toolchain_id(version_all: str) -> str:
    """`moonc v0.10.16+b1a0e846e; moon 1af328f` from `moon version --all`."""
    parts = {}
    for line in version_all.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] in ("moon", "moonc"):
            rev = words[2].strip("()") if words[0] == "moon" and len(words) > 2 else words[1]
            parts[words[0]] = rev
    if "moonc" not in parts or "moon" not in parts:
        raise SystemExit(f"cannot read the toolchain version from: {version_all!r}")
    return f"moonc {parts['moonc']}; moon {parts['moon']}"


class Formats:
    """Manifest formats by "name@version". Published archives never change,
    so a format once read is kept for good; failures are not cached."""

    def __init__(self, index: Index, known: dict[str, str]):
        self.index, self.known, self.missing = index, dict(known), set()
        self.failed: dict[str, str] = {}

    def __call__(self, name: str, version: str) -> str | None:
        key = f"{name}@{version}"
        if key not in self.known and key not in self.failed:
            self.missing.add(key)
        return self.known.get(key)

    def fetch_missing(self) -> int:
        todo = sorted(self.missing)
        self.missing.clear()

        def one(key):
            name, ver = key.rsplit("@", 1)
            v = self.index.get(name, ver)
            try:
                return key, fm.fetch(v), None
            except Exception as e:  # noqa: BLE001 - any failure leaves the format unknown
                return key, None, f"{type(e).__name__}: {e}"[:200]

        with ThreadPoolExecutor(8) as pool:
            for key, f, err in pool.map(one, todo):
                if f is None:
                    self.failed[key] = err
                else:
                    self.known[key] = f
        return len(todo)


def load_json(source: str | None, default):
    if not source:
        return default
    try:
        if source.startswith("https://"):
            req = urllib.request.Request(source, headers={"User-Agent": fm.USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        return json.loads(Path(source).read_text())
    except Exception as e:  # noqa: BLE001 - the first run has no previous data
        print(f"no previous data from {source}: {e}", file=sys.stderr)
        return default


def job_key(module: str, add, moon: str) -> str:
    return f"{module}|{','.join(add)}|{moon}"


def run(index: Index, moon: str, previous: dict, known_formats: dict, now: str):
    latest = index.latest()
    formats = Formats(index, known_formats)
    predictions = {}
    for round_ in range(FETCH_ROUNDS):
        pending = [n for n in latest if predictions.get(n) is None]
        for name in pending:
            predictions[name] = predict(index, name, latest[name].version, formats)
        if not formats.missing:
            break
        n = formats.fetch_missing()
        print(f"round {round_}: read {n} archive listings", file=sys.stderr)

    prev_modules = previous.get("modules", {})
    decided, jobs, fallback = {}, [], {}
    for name, v in sorted(latest.items()):
        p = predictions[name]
        module = f"{name}@{v.version}"
        base = {"name": name, "version": v.version, "created_at": v.created_at, "target": v.target or "wasm-gc"}
        if p is None:
            prev = prev_modules.get(name)
            if prev and prev.get("version") == v.version:
                decided[name] = prev
            else:
                decided[name] = {**base, "kind": UNKNOWN, "blockers": [], "add": [], "errors": [],
                                 "message": "Could not read an archive needed to resolve its dependencies.",
                                 "log": "", "moon": moon, "checked_at": now, "key": ""}
            continue
        if p.kind in (OWN_MANIFEST, UNRESOLVABLE, DEP_MANIFEST):
            decided[name] = {**base, "kind": p.kind, "blockers": list(p.blockers), "add": list(p.add),
                             "errors": [], "message": "", "log": "", "moon": moon, "checked_at": now,
                             "key": job_key(module, p.add, moon)}
            continue
        key = job_key(module, p.add, moon)
        prev = prev_modules.get(name)
        if prev and prev.get("key") == key and prev.get("kind") != UNKNOWN:
            decided[name] = prev
            continue
        jobs.append({**base, "module": module, "add": list(p.add), "blockers": list(p.blockers), "key": key})
        if prev and prev.get("version") == v.version and prev.get("kind") != UNKNOWN:
            fallback[name] = prev  # shown again if this check cannot tell

    shards = min(MAX_SHARDS, -(-len(jobs) // PER_SHARD))
    return decided, jobs, fallback, shards, formats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=Path, default=Path(os.environ.get("MOON_HOME", Path.home() / ".moon")) / "registry/index")
    ap.add_argument("--moon", required=True, help="output of `moon version --all`")
    ap.add_argument("--previous", help="URL or path of the base that holds status.json and formats.json")
    ap.add_argument("--seed", type=Path, default=Path(__file__).parent.parent / "seed/formats.json")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    moon = toolchain_id(args.moon)
    base = (args.previous or "").rstrip("/")
    previous = load_json(base and f"{base}/status.json", {})
    known = {**json.loads(args.seed.read_text()), **load_json(base and f"{base}/formats.json", {})}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    index = read(args.index)
    decided, jobs, fallback, shards, formats = run(index, moon, previous, known, now)

    args.out.mkdir(parents=True, exist_ok=True)
    for i in range(shards):
        with (args.out / f"shard-{i}.jsonl").open("w") as f:
            for job in jobs[i::shards]:
                f.write(json.dumps(job) + "\n")
    (args.out / "plan.json").write_text(json.dumps({
        "moon": moon,
        "generated_at": now,
        "index_fetched_at": index_fetched_at(args.index),
        "decided": decided,
        "assigned": {j["module"]: j for j in jobs},
        "fallback": fallback,
    }))
    (args.out / "formats.json").write_text(json.dumps(formats.known, sort_keys=True))
    print(f"{len(index.latest())} modules: {len(decided)} settled, {len(jobs)} to check in {shards} shards; "
          f"{len(formats.failed)} archives unreadable", file=sys.stderr)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"shards={json.dumps(list(range(shards)))}\n")


def index_fetched_at(index_dir: Path) -> str:
    """When `moon update` last moved the index, from the git reflog."""
    log = index_dir / ".git/logs/HEAD"
    try:
        stamp = int(log.read_text().splitlines()[-1].split("\t")[0].split()[-2])
    except (OSError, IndexError, ValueError):
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
