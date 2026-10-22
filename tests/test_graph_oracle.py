"""Read the live index both here and with gaato/mooncakes-graph, an
independent reader, and compare. Runs when MOONCAKES_GRAPH names its CLI."""

import json
import os
import subprocess
import unittest
from pathlib import Path

from health.index import read

CLI = os.environ.get("MOONCAKES_GRAPH")
INDEX = Path(os.environ.get("MOON_HOME", Path.home() / ".moon")) / "registry/index"


@unittest.skipUnless(CLI, "set MOONCAKES_GRAPH to the mooncakes-graph CLI")
class AgreesWithMooncakesGraph(unittest.TestCase):
    def test_versions(self):
        out = subprocess.run([CLI, "versions", "--index", str(INDEX)], capture_output=True, text=True, check=True).stdout
        theirs = {}
        for line in out.splitlines():
            r = json.loads(line)
            deps = {}
            for d, spec in r["deps"].items():
                v = spec if isinstance(spec, str) else (spec or {}).get("version")
                if v:
                    deps[d] = v
            theirs[(r["name"], r["version"])] = (deps, r["yanked"], r.get("created_at") or "")
        ours = {
            (v.name, v.version): (v.deps, v.yanked, v.created_at)
            for versions in read(INDEX).modules.values()
            for v in versions
        }
        # mooncakes-graph leaves out the test/ owner; everything else must match.
        extra = {k for k in ours.keys() - theirs.keys() if not k[0].startswith("test/")}
        self.assertEqual(extra, set())
        self.assertEqual(theirs.keys() - ours.keys(), set())
        diff = [k for k in theirs if theirs[k] != ours[k]]
        self.assertEqual(diff[:5], [])


if __name__ == "__main__":
    unittest.main()
