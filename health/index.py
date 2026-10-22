"""Read the mooncakes registry index that `moon update` keeps on disk."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Version:
    name: str
    version: str
    created_at: str
    deps: dict[str, str]  # module -> exact version; path-only deps are left out
    yanked: bool
    target: str  # preferred target, "" when the module does not name one
    source: str  # source directory inside the archive, "" for the root


@dataclass
class Index:
    modules: dict[str, list[Version]] = field(default_factory=dict)  # in publish order

    def get(self, name: str, version: str) -> Version | None:
        for v in self.modules.get(name, ()):
            if v.version == version:
                return v
        return None

    def latest(self) -> dict[str, Version]:
        """The newest version that is not yanked, for every module that has one."""
        out = {}
        for name, versions in self.modules.items():
            live = [v for v in versions if not v.yanked]
            if live:
                out[name] = live[-1]
        return out


def parse_row(row: dict) -> Version:
    deps = {}
    for dep, spec in (row.get("deps") or {}).items():
        ver = spec if isinstance(spec, str) else (spec or {}).get("version")
        if ver:
            deps[dep] = ver
    return Version(
        name=row["name"],
        version=row["version"],
        created_at=row.get("created_at") or "",
        deps=deps,
        yanked=bool(row.get("yanked")),
        # Both spellings occur in the index.
        target=row.get("preferred-target") or row.get("preferred_target") or "",
        source=(row.get("source") or "").strip("/").removeprefix("./").strip("/"),
    )


def read(index_dir: Path) -> Index:
    index = Index()
    for path in sorted((index_dir / "user").glob("*/*.index")):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if rows:
            index.modules[rows[0]["name"]] = [parse_row(r) for r in rows]
    return index


def vkey(version: str) -> tuple:
    """Semver ordering: a pre-release sorts before its release."""
    core, _, pre = version.split("+", 1)[0].partition("-")
    nums = [int(x) if x.isdigit() else 0 for x in core.split(".")[:3]]
    nums += [0] * (3 - len(nums))
    pre_key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"\.", pre)) if pre else ()
    return (*nums, pre == "", pre_key)


def compatible(a: str, b: str) -> bool:
    """Same release line: same major, or same minor while the major is 0."""
    ka, kb = vkey(a), vkey(b)
    return ka[0] == kb[0] and (ka[0] > 0 or ka[1] == kb[1])
