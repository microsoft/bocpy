---
name: update-website
description: "Refresh the microsoft.github.io/bocpy site to match the current state of `main`. Use when: updating the website, refreshing the GitHub Pages branch, syncing the tutorial after a library release, regenerating the Sphinx HTML under `docs/sphinx/`, or when asked to update / refresh / republish the website. Bumps every version reference on the landing page, rebuilds Sphinx into `docs/sphinx/`, and proposes new tutorial sections for any library work that has landed since the previous website publish."
argument-hint: "Optional: a git ref (tag or SHA) marking the previous website publish, if auto-detection from `docs/index.html` fails"
---

# Update Website

Refreshes `docs/index.html`, `docs/sphinx/`, and any associated assets so the
published GitHub Pages site reflects the current state of `main`. The skill
proposes additions to the tutorial but never edits `docs/index.html` for
prose changes — the user owns wording and structure decisions.

## When to Use

- Pulling the latest library work onto the `pages` branch
- After a new bocpy release tag has been pushed
- Tutorial has fallen behind a recently-landed feature
- Sphinx API reference needs to be regenerated against the latest `__init__.pyi`

For a multi-perspective code review of the library, use `branch-review` on
the library branch *before* the website refresh — not on this branch.

## Prerequisites

Before invoking this skill the user has already:

- Rebased the `pages` branch on `main` so the only commit between `main` and
  `HEAD` is the website diff itself (`git log --oneline main..HEAD` shows one
  commit).
- Activated `.env` at the repo root (`source .env/bin/activate`). The skill
  uses this venv for `sphinx-build` and any `import bocpy` inspection.

If either is not true, stop and surface the gap before doing anything else.

## Scratch Directory

All intermediate artifacts go under `.copilot/website/`:

```
.copilot/website/
├── 00-baseline.md               # Step 1 output
├── 10-version-targets.txt       # Step 2: files containing the old version
├── 15-highlightjs-decision.json # Step 3: helper-script decision record
├── 20-install.log               # Step 4: pip install -e .[docs] output
├── 20-sphinx-build.log          # Step 4: sphinx-build output
├── 20-sphinx-sync.log           # Step 4: rsync / cp diff vs. docs/sphinx/
├── 30-library-log.txt           # Step 5: git log <prev>..main
├── 30-library-diff.stat         # Step 5: git diff <prev>..main --stat
├── 30-changelog-window.md       # Step 5: relevant CHANGELOG sections
├── 30-tutorial-proposal.md      # Step 5: written proposal (the primary output)
└── snippets/                    # Step 5: ready-to-paste HTML/SVG snippets
    ├── <section-id>.html
    └── <section-id>-nav.html
```

Create `.copilot/website/` with `mkdir -p` if it does not exist. Reuse
existing files when re-running a step; ask the user before overwriting
an artifact that already has hand-edits in it.

## Procedure

Run the steps in order. Pause between every step for user review — this is
a publishing workflow, not an autonomous one.

### Step 1 — Establish the Baseline

Gather the facts the rest of the skill depends on. Write the summary to
`.copilot/website/00-baseline.md`.

1. Confirm the working tree is the `pages` branch on top of `main`:

   ```bash
   git rev-parse --abbrev-ref HEAD
   git log --oneline main..HEAD
   ```

   There must be exactly one commit. If not, stop and ask the user to
   re-rebase.

2. Read the **target version** from `[project].version` in `pyproject.toml`
   on `main`. This is the version the website will advertise after the
   refresh:

   ```bash
   git show main:pyproject.toml | grep -E '^version\s*=' | head -1
   ```

3. Read the **current website version** from `docs/index.html` — the
   `Currently <strong>vX.Y.Z</strong>` string in the hero block is the
   canonical marker. Record it; it determines the previous-publish ref
   used in Step 4.

4. Identify the **previous-publish ref** for the library-change window:

   - First try the git tag matching the current website version
     (`v<X.Y.Z>`). If it exists, use it.
   - If no matching tag exists, ask the user for an explicit ref (the
     skill's `argument-hint` is exactly this fallback).

5. Record all of the above in `00-baseline.md`. The file is the source of
   truth for every later step.

Present the baseline to the user. **Do not proceed without explicit
confirmation** — getting the target version or the previous-publish ref
wrong corrupts every later step.

### Step 2 — Update Version References

Update every "current version" marker on the landing page. Generated files
(under `docs/sphinx/`) are *not* edited here; Step 3 regenerates them.

#### 2.1 Inventory

List every file containing the old version string anywhere under `docs/`,
excluding `docs/sphinx/` and `docs/scripts/highlight/` (third-party):

```bash
grep -rn --include='*.html' --include='*.css' --include='*.js' \
    -E 'v?<OLD_X>\.<OLD_Y>\.<OLD_Z>' docs/ \
  | grep -vE '^docs/(sphinx|scripts/highlight)/' \
  | tee .copilot/website/10-version-targets.txt
```

Review the list. The expected hits in a clean checkout are limited to
`docs/index.html` (typically three: the hero "Currently vX.Y.Z" string,
the footer "Currently vX.Y.Z" string, and the benchmark caption
"bocpy vX.Y.Z").

#### 2.2 Classify each hit

For each match, classify it as either:

| Class | What it means | Action |
|-------|---------------|--------|
| **current-version** | "this is the version of bocpy this page describes" | Bump to the new version |
| **provenance** | "this benchmark / chart / asset was produced on bocpy vX.Y.Z" | **Do not bump.** Surface to the user as an open question — the value is tied to when the artifact was actually regenerated, not to the current release |
| **third-party** | A version string belonging to a vendored library (Bootstrap, highlight.js, Chart.js) | Do not bump here. highlight.js drift is checked separately in Step 3; Bootstrap and Chart.js are intentionally pinned to known-good CDN releases |

The "Currently vX.Y.Z" strings in the hero and footer are always
**current-version**. The "bocpy vX.Y.Z" caption under the scaling chart is
always **provenance** — it documents the benchmark run, not the live
release. Treat any new hit conservatively: if you cannot tell, ask.

#### 2.3 Apply the bumps

Edit only the **current-version** hits. After editing:

```bash
grep -rn --include='*.html' -E 'v?<OLD_X>\.<OLD_Y>\.<OLD_Z>' docs/ \
  | grep -vE '^docs/(sphinx|scripts/highlight)/'
```

The only remaining hits in `docs/index.html` should be the provenance
strings you flagged in 2.2. Anything else means a class was missed.

Surface the provenance hits to the user as a follow-up question — the
skill does not republish benchmark numbers on its own.

### Step 3 — Check Vendored highlight.js for a Minor Release

The website ships a vendored copy of highlight.js under
`docs/scripts/highlight/` to avoid a runtime CDN dependency. Without
periodic refreshes the vendored version drifts toward the abandoned-fork
zone, where the rest of the web has moved on and security fixes stop
arriving. This step is a **check-and-propose**: it never re-vendors
automatically, but it does flag when a refresh is overdue.

#### 3.1 Run the drift-check helper

Use the bundled helper script — stdlib only, no `jq` or `requests`
required:

```bash
python .github/skills/update-website/check_highlightjs.py \
  --output .copilot/website/15-highlightjs-decision.json
```

The script reads the vendored version from the three sources that
actually carry one (`docs/scripts/highlight/package.json`, plus the
header comments in `highlight.js` and `highlight.min.js`), queries
`api.github.com` for the latest highlight.js release, classifies the
drift per the table in §3.2, and writes a JSON decision record.

**Do not** treat `docs/scripts/highlight/es/package.json` as a version
source — it is an ESM-marker stub (`{"type": "module"}`) with no
`version` field. The helper deliberately skips it.

Exit codes:

| Code | Meaning | Skill action |
|------|---------|-------------|
| `0` | Skip — versions equal, vendored newer, or patch-only drift. | Note in the Step 6 summary that the check ran and was clean; jump to Step 4. |
| `1` | Propose a re-vendor — minor or major drift. | Continue to §3.3. |
| `2` | Error — vendored-source mismatch, network failure, or malformed payload. | Surface the stderr message to the user before continuing. |

Offline or rate-limited? Pass `--upstream-version X.Y.Z` to bypass the
network call, or set `GITHUB_TOKEN` in the environment to use an
authenticated request. Do **not** fall back to "assume no drift" —
drift detection is the whole point of this step.

If the script reports a vendored-source mismatch (exit 2), that is the
first finding: surface to the user before doing anything else. A
mismatch means a prior partial re-vendor.

#### 3.2 Decision table

The helper applies this table internally; it is reproduced here so the
skill author can sanity-check the JSON output.

| Diff | Decision string | Exit | Action |
|------|----------------|------|--------|
| Vendored == upstream | `skip-equal` | 0 | Note in summary. |
| Vendored > upstream | `skip-vendored-newer` | 0 | Note in summary, flag as anomaly. |
| Same MAJOR.MINOR, newer patch upstream | `skip-patch` | 0 | Note in summary; patch bumps are not worth churn. |
| Same major, newer minor upstream | `propose-minor` | 1 | Continue to §3.3 (re-vendor manifest). |
| Newer major upstream | `propose-major` | 1 | Continue to §3.3 **with a warning** — major bumps can rename CSS classes, drop languages, or change the public API. |

The JSON record at `.copilot/website/15-highlightjs-decision.json`
captures: `vendored_version`, `vendored_sources` (per-file map),
`upstream_version`, `upstream_published_at`, `upstream_url` (release
page), `decision`, and `rationale`.

#### 3.3 If a refresh is proposed, outline what to re-vendor

The skill does **not** download or replace the vendored bundle. That is a
manual user action, because re-vendoring touches dozens of files and
should be reviewed asset-by-asset. The proposal tells the user:

- **What the site uses:** `core` + `highlight` plus the `bash`, `python`,
  and `python-repl` language bundles. The themes under
  `docs/scripts/highlight/styles/` come along for the ride; do not prune
  them unless the user explicitly asks.
- **Where to get a matching build:** cdnjs
  (`https://cdnjs.cloudflare.com/ajax/libs/highlight.js/<NEW>/`) or a
  custom build from `https://highlightjs.org/download/`. Prefer the
  upstream-signed artifacts over a manual rebuild.
- **What to replace:** every file under `docs/scripts/highlight/`
  *except* `LICENSE` and the hand-maintained `README.md` / `DIGESTS.md`
  (the user re-records the SRI hashes there by hand after the swap).
- **Verification:** open `docs/index.html` locally and confirm bash and
  python code blocks still highlight correctly. The
  `cooking-an-omelette` and `cowns` sections both contain Python blocks;
  the **Getting Started** section has a bash block.

#### 3.4 Surface, do not apply

Even if the user verbally approves the re-vendor, **the skill stops at
the proposal**. The actual file swap happens in a follow-up turn so the
re-vendor diff stays isolated from the version-bump and Sphinx-regen
diffs in this run — that isolation is what makes the re-vendor
reviewable.

### Step 4 — Regenerate Sphinx Documentation

Rebuild the API reference and sync it into `docs/sphinx/`. Sphinx autodoc
imports `bocpy` to read `__init__.pyi`, so the in-venv `bocpy` must be
recent enough to expose every symbol the docs reference.

#### 4.1 Verify the venv

```bash
python -c "import bocpy, sys; print(sys.executable); print(bocpy.__file__)"
```

If `bocpy` is not importable or is an older version than the one in
`pyproject.toml`, install it before building. The `docs` extra pulls in
the Sphinx toolchain:

```bash
pip install -e .[docs] --no-build-isolation \
  2>&1 | tee .copilot/website/20-install.log
```

The `release = '<X.Y.Z>'` in `sphinx/source/conf.py` is bumped by the
`finalize-pr` skill on the library side. **Do not edit it here** — if it
disagrees with the target version from Step 1, that is a library-side bug
and should be raised, not patched on the `pages` branch.

#### 4.2 Clean build

A clean build avoids stale autodoc output from a previous bocpy install:

```bash
make -C sphinx clean
make -C sphinx html SPHINXOPTS='-W --keep-going -n' \
  2>&1 | tee .copilot/website/20-sphinx-build.log
```

- `-W` promotes warnings to errors so broken cross-references fail the
  build instead of silently shipping.
- `--keep-going` collects every warning rather than stopping at the
  first.
- `-n` enables nitpicky mode — surfaces missing type references etc.

The build must exit zero. If `-W` is too strict for an unrelated stale
warning, drop it (with explicit user approval) and triage the warning as
a follow-up rather than papering over it on the website branch.

#### 4.3 Sync into `docs/sphinx/`

The site serves whatever lives under `docs/sphinx/`. Mirror the freshly
built tree, excluding the build-metadata file that the site does not
need:

```bash
rsync -av --delete --exclude='.buildinfo' \
    sphinx/build/html/ docs/sphinx/ \
  2>&1 | tee .copilot/website/20-sphinx-sync.log
```

`--delete` removes any pages that no longer exist in the freshly built
output (e.g., a `.rst` file that was removed from the toctree). If you do
not have `rsync`, fall back to a `find docs/sphinx -mindepth 1 -delete`
followed by `cp -a sphinx/build/html/. docs/sphinx/` and a manual delete
of `docs/sphinx/.buildinfo`.

#### 4.4 Verify

```bash
git status docs/sphinx/ | head -30
grep -RE 'VERSION:|bocpy [0-9]+\.[0-9]+\.[0-9]+ documentation' \
    docs/sphinx/_static/documentation_options.js \
    docs/sphinx/index.html
```

- The first command shows the regenerated files.
- The second confirms that `documentation_options.js` and the page
  `<title>` carry the new version. Both are written by Sphinx from
  `conf.py`'s `release`; if they disagree, the venv built against a
  stale install — re-run 4.1 and 4.2.

### Step 5 — Propose Tutorial Updates

Identify what landed on `main` since the previous website publish, then
write a proposal for any new tutorial sections. **This step never edits
`docs/index.html`**. It writes a proposal plus ready-to-paste snippets;
the user makes the editorial call.

#### 5.1 Gather the library-change window

Using the previous-publish ref from Step 1:

```bash
PREV=<previous-publish-ref>   # e.g. v0.8.0

git log --oneline "$PREV"..main \
  | tee .copilot/website/30-library-log.txt

git diff "$PREV"..main --stat \
  -- 'src/bocpy/**' 'examples/**' 'templates/c_abi_consumer/**' \
  | tee .copilot/website/30-library-diff.stat
```

If `PREV` is not a tag, this still works for any commit ref.

#### 5.2 Extract the relevant CHANGELOG window

The CHANGELOG is the easiest signal for "what is publishable" — Step 4 of
`finalize-pr` curates each release for exactly this audience. Pull every
release entry between the previous-publish version and the target version:

```bash
awk '/^## / { p = ($0 ~ /Version <NEW_X>\.<NEW_Y>\.<NEW_Z>|Version <OLD_X>\.<OLD_Y>\.<OLD_Z>/) ? !p : p; if (p) print }' \
    CHANGELOG.md > .copilot/website/30-changelog-window.md
```

If a single release entry covers the window, prefer it over the raw
`git log` — its headings already group changes the way users care
about (New Features / Bug Fixes / Breaking Changes / Documentation).
Use the raw git log for anything the changelog under-documents.

#### 5.3 Classify what should reach the tutorial

The tutorial is the narrative entry point for newcomers. **Most library
changes do not belong there.** Use this table:

| Change kind | Tutorial action |
|-------------|-----------------|
| New top-level public symbol (e.g. a new `bocpy.*` function, a new `Cown` API) used by typical code | **Propose a new section** if the existing tutorial does not already cover the concept it represents |
| New tunable or config knob | At most a paragraph inside an existing section |
| Bug fix, internal refactor, perf work | **No tutorial change.** Belongs in the changelog and (sometimes) the Sphinx pages |
| New example script (`bocpy-*` entry point) | Mention as a link; full walkthroughs live in `docs/index.html` only if the concept is otherwise undocumented |
| New C ABI surface | **Not tutorial.** Lives in `sphinx/source/c_abi.rst`; regenerated by Step 4 |
| Breaking change | Propose a callout in the affected existing section; do not invent a new section just for it |

When unsure, default to **no proposal**. It is cheaper for the user to
add a section later than to remove one that was added on impulse.

#### 5.4 Map proposals to existing tutorial structure

The current tutorial has these top-level sections (read them straight
from `docs/index.html`; do not assume the list below is current):

- `cooking-an-omelette` — motivating example
- `cowns` — concept of cowns
- `behaviors` — `@when` and behaviors
- `cooking-with-boc` — the example reworked
- `noticeboard` — global key-value store
- `matrix` — `bocpy.Matrix`
- `scaling` — throughput chart

Before proposing a new section, confirm the topic is **not already
covered** by one of the above. If it overlaps, propose a paragraph
*inside* the existing section rather than a new top-level entry.

#### 5.5 Write the proposal

Write `.copilot/website/30-tutorial-proposal.md`. For each proposed
addition include:

1. **Title and proposed section id** (the `id="..."` for the `<section>`
   anchor; kebab-case, matches the navbar `href`).
2. **Why it belongs in the tutorial** — one paragraph tying it to the
   library change(s) from 5.1 / 5.2.
3. **Where it goes** — which existing section it follows, or whether
   it expands an existing one.
4. **Outline** — bullet list of the points the section should make.
5. **Suggested code sample** — copy a real example from `examples/`
   wherever possible rather than inventing new code. Cite the source
   file path.
6. **Navigation impact** — which navbar entries need updating
   (`docs/index.html` has both a sticky-top mobile navbar and a
   sidebar; both must list the same anchors).

For each proposal also write a ready-to-paste HTML snippet under
`.copilot/website/snippets/<section-id>.html`. The snippet must:

- Use the same Bootstrap classes and structure as neighbouring sections
  (`<section id="..." class="mb-5">`, `<h1 class="lhd-2 fw-semibold text-center">`, etc.).
- Reference real code (verbatim from `examples/` or `src/bocpy/`).
- Not assume any new SVG or chart asset; if one would help, flag it as
  an open question rather than producing a placeholder.

Write a companion `<section-id>-nav.html` containing the two `<li>`
entries (one for the sticky mobile navbar, one for the sidebar) that
need to be added.

#### 5.6 Surface, do not apply

Present the proposal in chat with the file paths. The user decides which
proposals to accept, reorder, or drop. **Do not edit `docs/index.html`
in this step.**

### Step 6 — Summarize

Present a single summary to the user with:

- **Version:** old → new (the marker from Step 1).
- **Files edited by this skill:**
  - The version-bump edits to `docs/index.html` (Step 2).
  - The regenerated tree under `docs/sphinx/` (Step 4) — link the
    `git status docs/sphinx/` count rather than enumerating files.
- **Files written but not committed:** every artifact under
  `.copilot/website/`.
- **highlight.js drift check (Step 3):** vendored version, latest
  upstream version, and the classification (skip / propose-refresh).
  If a refresh was proposed, state that no files under
  `docs/scripts/highlight/` were touched and point at
  `15-highlightjs-decision.json`.
- **Tutorial proposals:** count and the path to
  `30-tutorial-proposal.md`. Make clear that none of these have been
  applied to `docs/index.html`.
- **Open questions:**
  - Provenance version strings from Step 2.2 that the skill deliberately
    left alone (e.g. benchmark captions).
  - Anything the proposal flagged that needs a new asset
    (SVG, benchmark re-run, chart).
  - Any Sphinx warnings that were downgraded from errors in Step 4.2.
  - Whether to do the highlight.js re-vendor in a follow-up turn, if
    Step 3 proposed one.

**Do not commit.** Every git operation belongs to the user.

## Guardrails

- **Never commit, push, tag, or open a PR.** The user owns every git
  operation on the `pages` branch.
- **Never edit `docs/index.html` for prose changes.** Step 2 only
  bumps current-version strings. Step 5 proposes — the user applies.
- **Never edit `sphinx/source/conf.py`.** The `release` value there is
  the responsibility of the `finalize-pr` skill on the library branch.
  If it disagrees with the target version, raise it as a bug — do not
  patch around it on the `pages` branch.
- **Never re-vendor `docs/scripts/highlight/` in the same run as a
  version bump or Sphinx regen.** Step 3 stops at a written proposal
  precisely so the re-vendor diff stays isolated and reviewable.
  Re-vendoring happens in a follow-up turn, after the user approves
  the proposed version.
- **Never bump a highlight.js version string in place.** The version
  appears in `package.json`, the `es/` variant, file header comments,
  and the `DIGESTS.md` SRI hashes — editing one without the others
  silently corrupts the bundle. A re-vendor replaces all of them
  atomically.
- **Never bump a provenance version string** (Step 2.2) silently.
  Always surface to the user — the underlying artifact (benchmark
  number, chart, screenshot) may need to be regenerated first.
- **Never disable Sphinx `-W` (warning-as-error) without explicit user
  approval.** Warnings on the docs site become broken links on
  `microsoft.github.io/bocpy/sphinx/`.
- **Never invent new tutorial code samples** when a real one exists in
  `examples/`. The tutorial is meant to be runnable.
