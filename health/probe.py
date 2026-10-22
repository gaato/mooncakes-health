"""Check modules the way a consumer meets them. Runs third-party code (build
scripts of the modules and their dependencies), so it runs in a job with no
permissions and no secrets, and its output is validated before use.

For each job: create an empty consumer module that requires the module (and
the versions the plan says to add), let moon resolve and download it, read
the package list moon writes, then import every package a consumer can
import and run `moon check`.

    python3 -m health.probe SHARD.jsonl OUT.jsonl
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .plan import toolchain_id

TIMEOUT = 300
# Names moon uses in packages.json for the targets in index rows.
TARGET_NAMES = {"wasm": "Wasm", "wasm-gc": "WasmGC", "js": "Js", "native": "Native", "llvm": "LLVM"}
ERROR_CODE = re.compile(r"Error: \[(\d{4})\]")
REJECTED = re.compile(r"`moon\.(?:mod|pkg)\.json` at '[^']*/\.mooncakes/([^/']+/[^/']+)")
# The resolved versions do not provide a package some module imports.
POSTADD = re.compile(r"failed to execute postadd script in [^\n]*?\.mooncakes/([^/\s]+/[^/\s]+)")
MISSING_IMPORT = re.compile(r"Cannot find import '([^']+)' in ([^\s]+)")
NETWORK = re.compile(r"(?i)failed to download|error sending request|connection (?:reset|refused|closed)|timed out|dns error|status code 5\d\d")


def moon(work: Path, *args: str) -> tuple[int | None, str]:
    """Exit code (None on timeout) and combined output."""
    try:
        p = subprocess.run(["moon", *args], cwd=work, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return None, out
    return p.returncode, p.stdout + p.stderr


def write_probe(work: Path, requires: list[str], imports: list[str]) -> None:
    lines = "".join(f'  "{r}",\n' for r in requires)
    (work / "moon.mod").write_text(f'name = "health/probe"\n\nversion = "0.0.0"\n\nimport {{\n{lines}}}\n')
    imported = "".join(f'  "{p}" @p{i},\n' for i, p in enumerate(imports))
    (work / "moon.pkg").write_text(f"import {{\n{imported}}}\n" if imports else "")
    (work / "probe.mbt").write_text("pub fn probe() -> Unit {\n  ()\n}\n")


def importable(work: Path, name: str, target: str) -> list[str]:
    data = json.loads((work / "_build" / target / "debug/check/packages.json").read_text())
    want = TARGET_NAMES.get(target)
    out = []
    for p in data["packages"]:
        rel = p.get("rel", "")
        if p.get("root") != name or p.get("is-main") or "internal" in rel.split("/"):
            continue
        if want not in p.get("supported-targets", [want]):
            continue
        out.append(f"{name}/{rel}" if rel else name)
    return sorted(out)


def summarize(code: int | None, log: str, work: Path) -> dict:
    log = log.replace(str(work), "probe")
    return {
        "errors": sorted(set(ERROR_CODE.findall(log)))[:20],
        "log": log[-2000:],
        "timed_out": code is None,
        "network": bool(NETWORK.search(log)),
        "rejected": REJECTED.findall(log),
        "missing_import": MISSING_IMPORT.search(log),
        "postadd": POSTADD.search(log),
    }


def check(job: dict, root: Path) -> dict:
    name, target, add = job["name"], job["target"], job["add"]
    work = Path(tempfile.mkdtemp(dir=root))
    started = time.monotonic()
    result = {"module": job["module"]}
    try:
        write_probe(work, [job["module"], *add], [])
        code, log = moon(work, "check", "--target", target)
        if code != 0:
            s = summarize(code, log, work)
            result.update(verdict_resolve(s, add))
        else:
            imports = importable(work, name, target)
            write_probe(work, [job["module"], *add], imports)
            code, log = moon(work, "check", "--target", target)
            s = summarize(code, log, work)
            result.update(verdict_check(code, s, add, len(imports), target))
            result["imported"] = len(imports)
        result.setdefault("errors", [])
        result.setdefault("log", "")
        result.setdefault("imported", 0)
    except Exception as e:  # noqa: BLE001 - one module must not stop the shard
        result.update(kind="unknown", message=f"probe failed: {type(e).__name__}: {e}"[:300], errors=[], log="")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def verdict_resolve(s: dict, add: list[str]) -> dict:
    """moon failed before compiling anything: resolving or downloading."""
    base = {"errors": s["errors"], "log": s["log"]}
    if s["timed_out"]:
        return {**base, "kind": "unknown", "message": "Timed out while resolving dependencies."}
    if s["rejected"]:
        rejected = ", ".join(sorted(set(s["rejected"])))
        tried = " even with the added versions" if add else ""
        return {**base, "kind": "dep_manifest", "message": f"moon rejects the JSON manifest of {rejected}{tried}."}
    if s["postadd"]:
        # Install hooks may need tools this environment does not have; that
        # says nothing about whether the module works.
        who = s["postadd"].group(1)
        return {**base, "kind": "unknown", "message": f"The install script of {who} failed in the checking environment."}
    if s["missing_import"]:
        pkg, where = s["missing_import"].groups()
        message = f"{where} imports {pkg}, which the resolved versions do not have"
        if add:
            return {**base, "kind": "dep_manifest", "message": f"With the added versions, {message}."}
        return {**base, "kind": "build", "message": f"{message}."}
    if s["network"]:
        return {**base, "kind": "unknown", "message": "A download failed."}
    return {**base, "kind": "unknown", "message": "moon could not resolve the dependencies."}


def verdict_check(code: int | None, s: dict, add: list[str], imported: int, target: str) -> dict:
    base = {"errors": s["errors"], "log": s["log"] if code != 0 else ""}
    if code == 0:
        note = "" if imported else f"Has no library package for {target}, so only its dependencies were checked."
        return {**base, "kind": "workaround" if add else "ok", "message": note}
    if s["timed_out"]:
        return {**base, "kind": "unknown", "message": "moon check timed out."}
    if s["network"] and not s["errors"]:
        return {**base, "kind": "unknown", "message": "A download failed."}
    if add:
        return {**base, "kind": "dep_manifest", "message": "With the added versions, moon check fails."}
    return {**base, "kind": "build", "message": "moon check fails."}


def main():
    shard, out = Path(sys.argv[1]), Path(sys.argv[2])
    jobs = [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
    toolchain = toolchain_id(subprocess.run(["moon", "version", "--all"], capture_output=True, text=True).stdout)
    root = Path(tempfile.mkdtemp(prefix="probe-"))
    with out.open("w") as f:
        for i, job in enumerate(jobs, 1):
            r = check(job, root)
            r["moon"] = toolchain
            f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"[{i}/{len(jobs)}] {job['module']}: {r['kind']} ({r['seconds']}s)", flush=True)


if __name__ == "__main__":
    main()
