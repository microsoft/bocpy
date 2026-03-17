---
name: version-bump
description: "Bump the bocpy version across all required files. Use when: incrementing the version, releasing a new version, updating version strings, preparing a release, changing the version number, or adding a changelog entry."
---

# Version Bump

When asked to bump the version to a specific version (e.g. `0.3.0`), propose
edits to **all** of the following files. Do not skip any.

## Files to Update

### 1. `pyproject.toml`

Update the `version` field under `[project]`:

```toml
[project]
name = "bocpy"
version = "<NEW_VERSION>"
```

### 2. `sphinx/source/conf.py`

Update the `release` variable:

```python
release = '<NEW_VERSION>'
```

### 3. `CITATION.cff`

Update both `version` and `date-released`:

```yaml
version: <NEW_VERSION>
date-released: <TODAY YYYY-MM-DD>
```

### 4. `CHANGELOG.md`

Prepend a new entry **at the top** of the file with today's date, the new
version, and a placeholder for the user to fill in:

```markdown
## <TODAY YYYY-MM-DD> - Version <NEW_VERSION>
TODO: Add release notes.

```

## Procedure

1. Read the current version from `pyproject.toml` to confirm the old value.
2. Propose all four edits as a single changeset.
3. Use today's date (from context) for the CHANGELOG and CITATION entries.
4. Do **not** commit — the user handles git operations.
