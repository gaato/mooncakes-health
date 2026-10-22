"""Predict what a consumer gets when it adds one module, and which extra
dependency versions would replace the ones moon rejects.

The prediction only decides what to check and what to try; the verdict comes
from running moon.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .formats import LEGACY, NEW
from .index import Index, compatible, vkey

CORE = "moonbitlang/core"  # ships with the toolchain, not in the index


@dataclass(frozen=True)
class Resolution:
    selected: dict[str, str]  # every dependency module -> the version selected
    missing: tuple[str, ...]  # "name@version" requirements absent from the index


def resolve(index: Index, root: str, version: str, adds: dict[str, str] | None = None) -> Resolution:
    """Minimal version selection as Go does it: walk every version reachable
    from the requirements and take each module at the highest one.

    `adds` are extra requirements of the consumer, next to `root@version`.
    """
    selected: dict[str, str] = {}
    seen, missing = set(), set()
    stack = [(root, version), *(adds or {}).items()]
    while stack:
        name, ver = stack.pop()
        if (name, ver) in seen:
            continue
        seen.add((name, ver))
        row = index.get(name, ver)
        if row is None:
            missing.add(f"{name}@{ver}")
            continue
        if name != root and vkey(ver) > vkey(selected.get(name, "0.0.0-0")):
            selected[name] = ver
        stack.extend((d, v) for d, v in row.deps.items() if d != CORE)
    return Resolution(selected, tuple(sorted(missing)))


@dataclass(frozen=True)
class Prediction:
    kind: str  # "own_manifest", "unresolvable", "dep_manifest", "workaround", "check"
    blockers: tuple[str, ...] = ()  # rejected dependency versions, "name@version"
    add: tuple[str, ...] = ()  # versions a consumer would add, "name@version"


def predict(
    index: Index,
    name: str,
    version: str,
    fmt: Callable[[str, str], str | None],
    max_rounds: int = 10,
) -> Prediction | None:
    """None when a format needed for the prediction is unknown.

    `fmt(name, version)` returns NEW, LEGACY, NO_MANIFEST, or None if unknown.
    """
    try:
        return _predict(index, name, version, fmt, max_rounds)
    except UnknownFormat:
        return None


class UnknownFormat(Exception):
    pass


def _known(fmt, name: str, version: str) -> str:
    f = fmt(name, version)
    if f is None:
        raise UnknownFormat(f"{name}@{version}")
    return f


def _predict(index, name, version, fmt, max_rounds) -> Prediction:
    if _known(fmt, name, version) == LEGACY:
        return Prediction("own_manifest")
    adds: dict[str, str] = {}
    first_blockers: tuple[str, ...] = ()
    for _ in range(max_rounds):
        res = resolve(index, name, version, adds)
        if res.missing:
            return Prediction("unresolvable", blockers=res.missing)
        formats = {d: _known(fmt, d, v) for d, v in res.selected.items()}
        bad = sorted(f"{d}@{v}" for d, v in res.selected.items() if formats[d] == LEGACY)
        if not bad:
            if not adds:
                return Prediction("check")
            return Prediction("workaround", first_blockers, _fmt_adds(adds))
        if not adds:
            first_blockers = tuple(bad)
        for b in bad:
            dep, ver = b.rsplit("@", 1)
            candidate = replacement(index, dep, ver, fmt)
            if candidate is None:
                return Prediction("dep_manifest", first_blockers, _fmt_adds(adds))
            adds[dep] = candidate
    return Prediction("dep_manifest", first_blockers, _fmt_adds(adds))


def replacement(index: Index, name: str, version: str, fmt) -> str | None:
    """The oldest newer version in the new format, preferring the same release line.

    The oldest is the smallest change, so the most likely to keep the API.
    """
    newer = sorted(
        (v.version for v in index.modules.get(name, ()) if not v.yanked and vkey(v.version) > vkey(version)),
        key=vkey,
    )
    usable = [v for v in newer if _known(fmt, name, v) == NEW]
    same_line = [v for v in usable if compatible(version, v)]
    return (same_line or usable or [None])[0]


def _fmt_adds(adds: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted(f"{d}@{v}" for d, v in adds.items()))
