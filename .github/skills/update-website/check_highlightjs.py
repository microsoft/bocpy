#!/usr/bin/env python3
"""Check vendored highlight.js for drift against the latest upstream release.

Used by Step 3 of the `update-website` skill. Replaces an ad-hoc
``jq + curl + python`` pipeline that broke when ``jq`` was not installed
and that over-eagerly treated ``docs/scripts/highlight/es/package.json``
(an ESM-marker stub with only ``{"type": "module"}``) as a third version
source.

The script:

1. Reads the vendored version from three sources that *do* carry one:
   ``docs/scripts/highlight/package.json``, plus the headers of
   ``highlight.js`` and ``highlight.min.js``. A mismatch among these is
   itself a finding.
2. Looks up the latest upstream tag from the GitHub releases API
   (``--upstream-version`` overrides for offline runs).
3. Classifies the diff (equal / vendored-newer / patch / minor / major)
   per the Step 3.3 decision table in ``SKILL.md``.
4. Writes a machine-readable JSON record to ``--output`` and prints a
   human-readable summary to stdout.

Exit codes:

  * 0 — skip (no drift, or patch-only drift; no action required)
  * 1 — propose a re-vendor (minor or major drift)
  * 2 — error (vendored-version mismatch, network failure, or a malformed
        upstream payload)

The script never writes anywhere under ``docs/scripts/highlight/``;
re-vendoring is always a follow-up user action, per the Step 3
guardrail in ``SKILL.md``.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM_API = "https://api.github.com/repos/highlightjs/highlight.js/releases/latest"
USER_AGENT = "bocpy-pages-update-website-skill/1"
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
HEADER_RE = re.compile(r"Highlight\.js v(\d+\.\d+\.\d+)")


def parse_semver(text):
    """Parse ``vX.Y.Z`` or ``X.Y.Z`` into a ``(major, minor, patch)`` tuple."""
    m = SEMVER_RE.match(text.strip())
    if not m:
        raise ValueError(f"not a semver string: {text!r}")
    return tuple(int(p) for p in m.groups())


def read_vendored(repo_root):
    """Return ``{source_path: version_string}`` for every vendored source.

    Sources without a version are silently skipped (e.g.
    ``es/package.json`` carries only ``{"type": "module"}`` and is
    not a versioned manifest).
    """
    sources = {}
    pkg = repo_root / "docs" / "scripts" / "highlight" / "package.json"
    with pkg.open(encoding="utf-8") as f:
        data = json.load(f)
    if "version" in data:
        sources[str(pkg.relative_to(repo_root))] = data["version"]

    for name in ("highlight.js", "highlight.min.js"):
        path = repo_root / "docs" / "scripts" / "highlight" / name
        with path.open(encoding="utf-8") as f:
            head = f.read(512)
        m = HEADER_RE.search(head)
        if m:
            sources[str(path.relative_to(repo_root))] = m.group(1)
    return sources


def fetch_upstream(timeout):
    """Query the GitHub releases API for the latest tag.

    Honours ``GITHUB_TOKEN`` from the environment to dodge anonymous
    rate-limits in CI. Returns ``(tag, published_at, html_url)`` or
    raises a ``RuntimeError`` with a user-actionable message.
    """
    req = urllib.request.Request(UPSTREAM_API)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError(
                "GitHub API returned 403 (likely anonymous rate limit). "
                "Re-run with GITHUB_TOKEN set, or pass --upstream-version "
                "manually."
            ) from exc
        raise RuntimeError(f"GitHub API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc

    try:
        return payload["tag_name"], payload["published_at"], payload["html_url"]
    except KeyError as exc:
        raise RuntimeError(f"GitHub API payload missing key: {exc}") from exc


def classify(vendored, upstream):
    """Apply the Step 3.3 decision table to two semver tuples."""
    if vendored == upstream:
        return "skip-equal", "Vendored version matches upstream latest."
    if vendored > upstream:
        return "skip-vendored-newer", (
            "Vendored version is newer than the latest upstream release. "
            "This is unusual; verify the vendored bundle was not built from "
            "a pre-release tag."
        )
    if vendored[:2] == upstream[:2]:
        return "skip-patch", (
            "Patch-level drift only; per SKILL.md Step 3.3, patch bumps "
            "are not worth a re-vendor."
        )
    if vendored[0] == upstream[0]:
        return "propose-minor", (
            "Minor-version drift detected. Re-vendoring is recommended; "
            "see SKILL.md Step 3.4 for the manifest."
        )
    return "propose-major", (
        "Major-version drift detected. Re-vendoring is recommended *with "
        "extra verification*: major bumps can rename CSS classes, drop "
        "languages, or change the public API."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Path to the bocpy-pages repo root (default: cwd).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".copilot/website/15-highlightjs-decision.json"),
        help=(
            "Path (relative to --repo-root) for the JSON decision record. "
            "Pass /dev/null to skip the write."
        ),
    )
    parser.add_argument(
        "--upstream-version",
        help=(
            "Bypass the GitHub API and assume this is the latest upstream "
            "version (e.g. for offline runs)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Network timeout for the GitHub API call (seconds).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()

    try:
        vendored_sources = read_vendored(repo_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: failed to read vendored sources: {exc}", file=sys.stderr)
        return 2

    if not vendored_sources:
        print("error: no version found in any vendored source", file=sys.stderr)
        return 2

    distinct = set(vendored_sources.values())
    if len(distinct) > 1:
        print("error: vendored version mismatch among sources:", file=sys.stderr)
        for path, version in sorted(vendored_sources.items()):
            print(f"  {path}: {version}", file=sys.stderr)
        return 2

    vendored_str = next(iter(distinct))
    try:
        vendored = parse_semver(vendored_str)
    except ValueError as exc:
        print(f"error: vendored version is not semver: {exc}", file=sys.stderr)
        return 2

    upstream_published_at = None
    upstream_url = None
    if args.upstream_version:
        upstream_str = args.upstream_version
    else:
        try:
            upstream_str, upstream_published_at, upstream_url = fetch_upstream(args.timeout)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        upstream = parse_semver(upstream_str)
    except ValueError as exc:
        print(f"error: upstream version is not semver: {exc}", file=sys.stderr)
        return 2

    decision, rationale = classify(vendored, upstream)

    record = {
        "vendored_version": vendored_str,
        "vendored_sources": vendored_sources,
        "upstream_version": upstream_str,
        "upstream_published_at": upstream_published_at,
        "upstream_url": upstream_url,
        "decision": decision,
        "rationale": rationale,
    }

    output_path = args.output if args.output.is_absolute() else repo_root / args.output
    if str(output_path) != os.devnull:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.write("\n")

    print(f"vendored:  {vendored_str}")
    print(f"upstream:  {upstream_str}" + (f"  ({upstream_published_at})" if upstream_published_at else ""))
    print(f"decision:  {decision}")
    print(f"rationale: {rationale}")
    if str(output_path) != os.devnull:
        try:
            rel = output_path.relative_to(repo_root)
        except ValueError:
            rel = output_path
        print(f"written:   {rel}")

    if decision.startswith("skip"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
