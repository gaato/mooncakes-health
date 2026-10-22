import json
import unittest

from health import formats as fm
from health.index import Index, compatible, parse_row, vkey
from health.plan import job_key, run, toolchain_id
from health.probe import summarize, verdict_check, verdict_resolve
from health.publish import merge
from health.resolve import predict, resolve
from health.schema import validate_result

MOON = "moonc v0.10.16+b1a0e846e; moon 1af328f"
NOW = "2026-10-23T00:00:00+00:00"


def row(name, version, deps=None, **extra):
    return {"name": name, "version": version, "deps": deps or {}, "created_at": "2026-10-01T00:00:00Z", **extra}


def index_of(*rows):
    idx = Index()
    for r in rows:
        idx.modules.setdefault(r["name"], []).append(parse_row(r))
    return idx


# a/app depends on b/lib@0.3.5 (JSON); b/lib@0.3.6 is in the new format;
# c/old is JSON itself; d/uses-x needs x/x@0.4.0 whose only fix is 0.5.0.
INDEX = index_of(
    row("b/lib", "0.3.5"),
    row("b/lib", "0.3.6"),
    row("b/lib", "0.4.0"),
    row("a/app", "1.0.0", {"b/lib": "0.3.5", "moonbitlang/core": "0.1.0"}),
    row("c/old", "0.1.0"),
    row("x/x", "0.4.0"),
    row("x/x", "0.5.0"),
    row("d/uses-x", "0.1.0", {"x/x": "0.4.0"}),
    row("e/stuck", "0.1.0", {"c/old": "0.1.0"}),
    row("f/gone", "0.1.0", {"b/lib": "9.9.9"}),
    row("g/fine", "0.1.0", {"b/lib": "0.3.6"}),
    row("h/yanked", "0.1.0", yanked=True),
    row("test/fixture", "0.1.0"),
)
FORMATS = {
    "b/lib@0.3.5": fm.LEGACY, "b/lib@0.3.6": fm.NEW, "b/lib@0.4.0": fm.NEW,
    "a/app@1.0.0": fm.NEW, "c/old@0.1.0": fm.LEGACY, "x/x@0.4.0": fm.LEGACY, "x/x@0.5.0": fm.NEW,
    "d/uses-x@0.1.0": fm.NEW, "test/fixture@0.1.0": fm.NEW, "e/stuck@0.1.0": fm.NEW, "f/gone@0.1.0": fm.NEW, "g/fine@0.1.0": fm.NEW,
}


def fmt(name, version):
    return FORMATS.get(f"{name}@{version}")


class Versions(unittest.TestCase):
    def test_order(self):
        self.assertLess(vkey("0.3.5"), vkey("0.3.6"))
        self.assertLess(vkey("0.10.0"), vkey("0.11.0"))
        self.assertLess(vkey("1.0.0-rc.1"), vkey("1.0.0"))
        self.assertLess(vkey("1.0.0-rc.2"), vkey("1.0.0-rc.10"))

    def test_compatible(self):
        self.assertTrue(compatible("0.3.5", "0.3.9"))
        self.assertFalse(compatible("0.3.5", "0.4.0"))
        self.assertTrue(compatible("1.2.0", "1.9.0"))
        self.assertFalse(compatible("1.2.0", "2.0.0"))

    def test_both_target_spellings(self):
        self.assertEqual(parse_row(row("a/b", "1.0.0", **{"preferred-target": "js"})).target, "js")
        self.assertEqual(parse_row(row("a/b", "1.0.0", preferred_target="native")).target, "native")

    def test_path_only_deps_are_left_out(self):
        r = parse_row(row("a/b", "1.0.0", {"c/d": {"path": "../d"}, "e/f": {"version": "0.1.0", "alias": "f"}}))
        self.assertEqual(r.deps, {"e/f": "0.1.0"})


class Resolve(unittest.TestCase):
    def test_highest_required_wins_and_core_is_skipped(self):
        res = resolve(INDEX, "a/app", "1.0.0", {"b/lib": "0.3.6"})
        self.assertEqual(res.selected, {"b/lib": "0.3.6"})

    def test_predictions(self):
        def p(name, version="0.1.0"):
            return predict(INDEX, name, version, fmt)

        self.assertEqual(p("g/fine").kind, "check")
        self.assertEqual(p("c/old").kind, "own_manifest")
        app = p("a/app", "1.0.0")
        # The same release line is preferred over the newest version.
        self.assertEqual((app.kind, app.blockers, app.add), ("workaround", ("b/lib@0.3.5",), ("b/lib@0.3.6",)))
        self.assertEqual(p("d/uses-x").add, ("x/x@0.5.0",))  # outside the line when nothing inside fits
        self.assertEqual(p("e/stuck").kind, "dep_manifest")
        self.assertEqual(p("f/gone").kind, "unresolvable")

    def test_unknown_format_gives_no_prediction(self):
        self.assertIsNone(predict(INDEX, "a/app", "1.0.0", lambda n, v: None if n == "b/lib" else fmt(n, v)))


class Classify(unittest.TestCase):
    def test_names(self):
        self.assertEqual(fm.classify_names({"moon.mod", "src/moon.pkg"}, "src"), fm.NEW)
        self.assertEqual(fm.classify_names({"moon.mod.json"}, ""), fm.LEGACY)
        self.assertEqual(fm.classify_names({"moon.mod", "src/a/moon.pkg.json"}, "src"), fm.LEGACY)
        # Packages outside the source directory, and vendored ones, do not count.
        self.assertEqual(fm.classify_names({"moon.mod", "examples/moon.pkg.json", "src/moon.pkg"}, "src"), fm.NEW)
        self.assertEqual(fm.classify_names({"moon.mod", ".mooncakes/x/y/moon.pkg.json"}, ""), fm.NEW)
        self.assertEqual(fm.classify_names({"README.md"}, ""), fm.NO_MANIFEST)


class Plan(unittest.TestCase):
    def test_toolchain_id(self):
        out = ("moon 0.1.20261016 (1af328f 2026-10-16) /opt/moon/bin/moon\n"
               "moonc v0.10.16+b1a0e846e (2026-10-16) /opt/moon/bin/moonc\n")
        self.assertEqual(toolchain_id(out), MOON)

    def test_settled_reused_and_checked(self):
        known = {k: v for k, v in FORMATS.items()}
        reused = job_key("g/fine@0.1.0", (), MOON)
        previous = {"modules": {"g/fine": {"version": "0.1.0", "kind": "ok", "key": reused}}}
        decided, jobs, fallback, shards, _ = run(INDEX, MOON, previous, known, NOW)
        self.assertEqual(decided["g/fine"]["kind"], "ok")  # same key: not checked again
        self.assertEqual(decided["c/old"]["kind"], "own_manifest")
        self.assertEqual(decided["e/stuck"]["add"], [])
        self.assertNotIn("h/yanked", decided)
        self.assertNotIn("test/fixture", decided)
        self.assertEqual({j["name"] for j in jobs}, {"a/app", "b/lib", "d/uses-x", "x/x"})
        self.assertEqual(shards, 1)

    def test_new_toolchain_checks_again(self):
        previous = {"modules": {"g/fine": {"version": "0.1.0", "kind": "ok",
                                           "key": job_key("g/fine@0.1.0", (), "moonc old")}}}
        decided, jobs, fallback, _, _ = run(INDEX, MOON, previous, dict(FORMATS), NOW)
        self.assertIn("g/fine", {j["name"] for j in jobs})
        self.assertIn("g/fine", fallback)


ASSIGNED = {
    "a/app@1.0.0": {"name": "a/app", "version": "1.0.0", "created_at": "", "target": "wasm-gc",
                    "add": ["b/lib@0.3.6"], "blockers": ["b/lib@0.3.5"], "key": "k1"},
    "g/fine@0.1.0": {"name": "g/fine", "version": "0.1.0", "created_at": "", "target": "wasm-gc",
                     "add": [], "blockers": [], "key": "k2"},
}


class Validate(unittest.TestCase):
    def good(self, **over):
        return {"module": "g/fine@0.1.0", "kind": "ok", "errors": [], "message": "", "log": "",
                "moon": MOON, "seconds": 1.2, "imported": 3, **over}

    def test_accepts_a_good_row(self):
        self.assertEqual(validate_result(self.good(), ASSIGNED)["kind"], "ok")

    def test_rejects_what_the_plan_did_not_ask(self):
        for bad in (
            self.good(module="z/other@1.0.0"),
            self.good(kind="own_manifest"),  # only plan decides this
            self.good(kind="workaround"),  # nothing was added
            self.good(module="a/app@1.0.0", kind="ok"),  # versions were added
            self.good(errors=["<img>"]),
            self.good(message=["not", "a", "string"]),
            self.good(seconds=True),
            self.good(imported=-1),
            "a string",
        ):
            self.assertIsNone(validate_result(bad, ASSIGNED), bad)

    def test_clips_long_text(self):
        out = validate_result(self.good(log="x" * 10_000), ASSIGNED)
        self.assertEqual(len(out["log"]), 2000)

    def test_drops_unknown_fields(self):
        out = validate_result(self.good(html="<script>"), ASSIGNED)
        self.assertNotIn("html", out)


class Publish(unittest.TestCase):
    def plan(self):
        return {"moon": MOON, "index_fetched_at": NOW, "decided": {}, "assigned": ASSIGNED,
                "fallback": {"g/fine": {"name": "g/fine", "version": "0.1.0", "kind": "ok", "key": "old"}}}

    def test_missing_result_falls_back_or_is_unknown(self):
        status = merge(self.plan(), {}, NOW)
        self.assertEqual(status["modules"]["g/fine"]["key"], "old")  # previous verdict kept
        self.assertEqual(status["modules"]["a/app"]["kind"], "unknown")
        self.assertEqual(status["counts"]["unknown"], 1)

    def test_result_is_used(self):
        r = validate_result({"module": "a/app@1.0.0", "kind": "workaround", "errors": [], "message": "",
                             "log": "", "moon": MOON, "seconds": 2, "imported": 1}, ASSIGNED)
        status = merge(self.plan(), {"a/app@1.0.0": r}, NOW)
        m = status["modules"]["a/app"]
        self.assertEqual((m["kind"], m["add"]), ("workaround", ["b/lib@0.3.6"]))
        json.dumps(status)


class Probe(unittest.TestCase):
    def s(self, log, code=1):
        from pathlib import Path
        return summarize(code, log, Path("/tmp/w/abc"))

    def test_rejected_dependency(self):
        log = ("Error: Failed to calculate build plan\n    2: `moon.mod.json` at "
               "'/tmp/w/abc/.mooncakes/moonbitlang/regexp' is no longer supported. Run `moon fmt` to migrate.")
        v = verdict_resolve(self.s(log), [])
        self.assertEqual(v["kind"], "dep_manifest")
        self.assertIn("moonbitlang/regexp", v["message"])
        self.assertNotIn("/tmp/w/abc", v["log"])

    def test_missing_import_is_build(self):
        log = "2: Cannot find import 'moonbitlang/quickcheck/gen' in moonbit-community/XMLParser@0.2.6\n"
        v = verdict_resolve(self.s(log), [])
        self.assertEqual(v["kind"], "build")
        self.assertIn("moonbit-community/XMLParser@0.2.6 imports moonbitlang/quickcheck/gen", v["message"])
        self.assertEqual(verdict_resolve(self.s(log), ["x/y@1.0.0"])["kind"], "dep_manifest")

    def test_failed_install_script_is_unknown(self):
        log = "2: failed to execute postadd script in probe/.mooncakes/mizchi/v8,\n       command: bash\n"
        v = verdict_resolve(self.s(log), [])
        self.assertEqual(v["kind"], "unknown")
        self.assertIn("mizchi/v8", v["message"])

    def test_download_failure_is_unknown(self):
        self.assertEqual(verdict_resolve(self.s("error: failed to download x/y"), [])["kind"], "unknown")

    def test_compile_error_is_build(self):
        v = verdict_check(1, self.s("Error: [4021]\nsomething"), [], 2, "wasm-gc")
        self.assertEqual((v["kind"], v["errors"]), ("build", ["4021"]))

    def test_added_versions_that_fail_stay_dep_manifest(self):
        self.assertEqual(verdict_check(1, self.s("Error: [4021]"), ["b/lib@0.3.6"], 1, "js")["kind"], "dep_manifest")

    def test_timeout_is_unknown(self):
        self.assertEqual(verdict_check(None, self.s("", code=None), [], 1, "js")["kind"], "unknown")


if __name__ == "__main__":
    unittest.main()
