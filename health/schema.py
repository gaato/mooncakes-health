"""The shapes passed between jobs, and the check of what the untrusted job
returns. Rows from the check job may have been written by a module's build
script, so they are validated field by field and never trusted as code or
markup."""

import re

# Final verdicts shown on the page.
OK = "ok"  # usable as is
WORKAROUND = "workaround"  # usable after adding the listed dependency versions
OWN_MANIFEST = "own_manifest"  # the module's own manifest is JSON
DEP_MANIFEST = "dep_manifest"  # a dependency's manifest is JSON and nothing tried helps
UNRESOLVABLE = "unresolvable"  # a required dependency version is not in the index
BUILD = "build"  # manifests are fine but moon check fails
UNKNOWN = "unknown"  # we could not tell: a download failed, a timeout, a bad result

KINDS = (OK, WORKAROUND, OWN_MANIFEST, DEP_MANIFEST, UNRESOLVABLE, BUILD, UNKNOWN)
# The check job never decides OWN_MANIFEST: plan does, from the archive listing.
CHECK_KINDS = (OK, WORKAROUND, DEP_MANIFEST, UNRESOLVABLE, BUILD, UNKNOWN)

MAX_MESSAGE = 300
MAX_LOG = 2000
MAX_MOON = 200
ERROR_CODE = re.compile(r"\d{4}")


def clip(text: str, limit: int) -> str:
    """Keep the end, which is where moon prints the cause."""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def validate_result(row: object, assigned: dict[str, dict]) -> dict | None:
    """A cleaned result, or None when the row cannot be trusted.

    `assigned` maps "name@version" to the job the plan gave out, so a row can
    only speak about a module it was asked about, with the adds it was given.
    """
    if not isinstance(row, dict):
        return None
    module = row.get("module")
    if not isinstance(module, str) or module not in assigned:
        return None
    job = assigned[module]
    kind = row.get("kind")
    if kind not in CHECK_KINDS:
        return None
    # Only a probe with added versions can call it a workaround, and the
    # other way round.
    if (kind == WORKAROUND) != bool(job["add"]) and kind in (OK, WORKAROUND):
        return None
    errors = row.get("errors", [])
    if not isinstance(errors, list) or len(errors) > 20 or not all(
        isinstance(e, str) and ERROR_CODE.fullmatch(e) for e in errors
    ):
        return None
    out = {"module": module, "kind": kind, "errors": errors}
    for field, limit in (("message", MAX_MESSAGE), ("log", MAX_LOG), ("moon", MAX_MOON)):
        value = row.get(field, "")
        if not isinstance(value, str):
            return None
        out[field] = clip(value, limit)
    seconds = row.get("seconds", 0)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds < 86400:
        return None
    out["seconds"] = round(float(seconds), 1)
    imported = row.get("imported", 0)
    if not isinstance(imported, int) or isinstance(imported, bool) or not 0 <= imported < 10000:
        return None
    out["imported"] = imported
    return out
