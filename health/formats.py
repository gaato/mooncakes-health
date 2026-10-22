"""Tell whether a published version still uses the JSON manifests that moon
now rejects, by reading only the zip central directory over HTTP ranges."""

import io
import urllib.request
import zipfile
from urllib.parse import quote

from .index import Version

ARCHIVE = "https://download.mooncakes.io/user/{name}/{version}.zip"
USER_AGENT = "mooncakes-health (+https://github.com/gaato/mooncakes-health)"

NEW, LEGACY, NO_MANIFEST = "new", "legacy", "none"


class RangeFile(io.RawIOBase):
    """A read-only, seekable view of a remote file, fetched with Range requests."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        with self._get(0, 0) as r:
            self.size = int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def _get(self, start: int, end: int):
        req = urllib.request.Request(
            self.url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
        )
        return urllib.request.urlopen(req, timeout=30)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        with self._get(self.pos, self.pos + n - 1) as r:
            data = r.read()
        self.pos += len(data)
        return data


def classify_names(names: set[str], source: str) -> str:
    """NEW, LEGACY (a moon.mod.json, or a package with only moon.pkg.json) or NO_MANIFEST."""
    if "moon.mod" in names:
        mod = NEW
    elif "moon.mod.json" in names:
        return LEGACY
    else:
        return NO_MANIFEST
    prefix = f"{source}/" if source else ""
    pkg_files: dict[str, set[str]] = {}
    for n in names:
        if not n.startswith(prefix) or "/.mooncakes/" in f"/{n}" or "/_build/" in f"/{n}":
            continue
        d, _, base = n.rpartition("/")
        if base in ("moon.pkg", "moon.pkg.json"):
            pkg_files.setdefault(d, set()).add(base)
    if any(files == {"moon.pkg.json"} for files in pkg_files.values()):
        return LEGACY
    return mod


def fetch(v: Version) -> str:
    """Raises on network errors; the caller decides what an unknown format means."""
    url = ARCHIVE.format(name=quote(v.name), version=quote(v.version))
    with zipfile.ZipFile(RangeFile(url)) as z:
        return classify_names(set(z.namelist()), v.source)
