"""Structural validator for the PEP 770 SBOMs that ``build_sbom.py`` emits.

bocpy ships one CycloneDX 1.6 JSON SBOM per wheel under
``<dist>-<version>.dist-info/sboms/bocpy.cdx.json``. The build_wheels
CI workflow extracts every embedded SBOM and runs this validator over
the extracted set as a defence-in-depth check: if a refactor of
``build_sbom.py`` produces something that ``grype`` would silently
ignore (e.g. missing ``$schema``-equivalent ``bomFormat`` or wrong
``specVersion``), the verification job fails before the wheels are
ever published.

The validator is deliberately stdlib-only — it asserts the structural
invariants that bocpy itself commits to, not the full CycloneDX 1.6
schema. The full schema check is handed off to ``grype`` (which
exercises the document as a real consumer would). The combination
gives us:

* fast, dependency-free shape verification (this script), and
* third-party tool consumability proof (grype).

Usage::

    python scripts/validate_sbom.py path/to/one.cdx.json ...
    python scripts/validate_sbom.py sboms/

Exit status is 0 if every SBOM passes, 1 otherwise; per-file failures
are reported on stderr.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from typing import Iterable


SBOM_GLOB = "*.cdx.json"

# Match what ``build_sbom.py`` emits — these invariants must hold for
# every SBOM bocpy ships. Drift here will be caught at CI time.
EXPECTED_BOM_FORMAT = "CycloneDX"
EXPECTED_SPEC_VERSION = "1.6"
EXPECTED_TOOL_NAME = "build_sbom.py"
EXPECTED_PURL_PREFIX = "pkg:pypi/bocpy@"

# UUIDv5 serial number per CycloneDX 1.6 (the spec requires the
# ``urn:uuid:`` prefix). ``build_sbom.py`` derives the serial as
# ``uuid.uuid5(namespace, "<name>@<version>+<git>+<wheel>")`` so the
# value is byte-identical across rebuilds of the same source tree
# (reproducible-build contract). UUIDv5's version digit is ``5`` and
# the variant nibble is the standard ``[89ab]``.
_URN_UUID_RE = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}"
    r"-[0-9a-f]{12}$"
)

# ISO 8601 UTC timestamp, ``YYYY-MM-DDTHH:MM:SSZ`` — what
# ``_sbom_timestamp`` emits.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ValidationError(Exception):
    """One specific invariant was violated."""


def _require(condition: bool, message: str) -> None:
    """Raise :class:`ValidationError` with ``message`` if ``condition`` is false."""
    if not condition:
        raise ValidationError(message)


def _require_str(doc: dict[str, Any], key: str) -> str:
    """Return ``doc[key]`` as a non-empty string or raise."""
    value = doc.get(key)
    _require(isinstance(value, str) and value, f"missing or empty string field {key!r}")
    return value  # type: ignore[return-value]


def validate_sbom_document(doc: Any) -> None:
    """Assert that ``doc`` is one of bocpy's PEP 770 CycloneDX 1.6 SBOMs.

    Raises :class:`ValidationError` on the first violation encountered.

    :param doc: Parsed JSON document (typically from ``json.loads``).
    """
    _require(isinstance(doc, dict), "top-level value must be a JSON object")

    # --- Header invariants --------------------------------------------------
    _require(
        doc.get("bomFormat") == EXPECTED_BOM_FORMAT,
        f"bomFormat must be {EXPECTED_BOM_FORMAT!r}, got {doc.get('bomFormat')!r}",
    )
    _require(
        doc.get("specVersion") == EXPECTED_SPEC_VERSION,
        f"specVersion must be {EXPECTED_SPEC_VERSION!r}, got {doc.get('specVersion')!r}",
    )

    serial = _require_str(doc, "serialNumber")
    _require(
        bool(_URN_UUID_RE.match(serial)),
        f"serialNumber must be a urn:uuid:<UUIDv5>, got {serial!r}",
    )

    _require(
        isinstance(doc.get("version"), int) and doc["version"] >= 1,
        f"version must be a positive integer, got {doc.get('version')!r}",
    )

    # --- metadata ----------------------------------------------------------
    metadata = doc.get("metadata")
    _require(isinstance(metadata, dict), "metadata must be an object")
    assert isinstance(metadata, dict)  # for type-checkers

    timestamp = _require_str(metadata, "timestamp")
    _require(
        bool(_TIMESTAMP_RE.match(timestamp)),
        f"metadata.timestamp must match YYYY-MM-DDTHH:MM:SSZ, got {timestamp!r}",
    )

    tools = metadata.get("tools")
    _require(isinstance(tools, dict), "metadata.tools must be an object")
    assert isinstance(tools, dict)
    tool_components = tools.get("components")
    _require(
        isinstance(tool_components, list) and len(tool_components) >= 1,
        "metadata.tools.components must be a non-empty list",
    )
    assert isinstance(tool_components, list)
    tool_names = {
        t.get("name") for t in tool_components if isinstance(t, dict)
    }
    _require(
        EXPECTED_TOOL_NAME in tool_names,
        f"metadata.tools.components must include {EXPECTED_TOOL_NAME!r}, "
        f"got names={sorted(n for n in tool_names if isinstance(n, str))!r}",
    )

    component = metadata.get("component")
    _require(isinstance(component, dict), "metadata.component must be an object")
    assert isinstance(component, dict)

    bom_ref = _require_str(component, "bom-ref")
    _require(
        bom_ref.startswith(EXPECTED_PURL_PREFIX),
        f"metadata.component.bom-ref must start with {EXPECTED_PURL_PREFIX!r}, got {bom_ref!r}",
    )
    _require(
        component.get("type") == "library",
        f"metadata.component.type must be 'library', got {component.get('type')!r}",
    )
    _require_str(component, "name")
    _require_str(component, "version")
    purl = _require_str(component, "purl")
    _require(
        purl == bom_ref,
        f"metadata.component.purl ({purl!r}) must equal bom-ref ({bom_ref!r})",
    )
    _require(
        purl.startswith(EXPECTED_PURL_PREFIX),
        f"metadata.component.purl must start with {EXPECTED_PURL_PREFIX!r}, got {purl!r}",
    )

    # --- components & dependencies ----------------------------------------
    components = doc.get("components")
    _require(isinstance(components, list), "components must be a list")

    dependencies = doc.get("dependencies")
    _require(isinstance(dependencies, list), "dependencies must be a list")
    assert isinstance(dependencies, list)
    _require(
        any(isinstance(d, dict) and d.get("ref") == bom_ref for d in dependencies),
        f"dependencies must contain an entry with ref={bom_ref!r}",
    )


def validate_sbom_file(path: Path) -> None:
    """Load and structurally validate the SBOM at ``path``.

    :raises ValidationError: If the file is not valid JSON or fails any
        of bocpy's PEP 770 invariants.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"could not read {path}: {exc}") from exc
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{path}: invalid JSON: {exc}") from exc
    validate_sbom_document(doc)


def _expand_inputs(inputs: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """Expand directories to ``*.cdx.json`` files; pass files through.

    Directories are not recursed — the convention is one flat
    ``sboms/`` directory produced by the merge job in
    ``build_wheels.yml``.
    """
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(p.glob(SBOM_GLOB)))
        else:
            files.append(p)
    return files


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on any failure."""
    parser = argparse.ArgumentParser(
        prog="validate_sbom.py",
        description=(
            "Structurally validate bocpy's PEP 770 CycloneDX 1.6 SBOMs. "
            "Accepts SBOM files and/or directories containing *.cdx.json."
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="SBOM file paths or directories to validate.",
    )
    args = parser.parse_args(argv)

    files = _expand_inputs(args.inputs)
    if not files:
        print("validate_sbom.py: no SBOM files found", file=sys.stderr)
        return 1

    failed = 0
    for path in files:
        try:
            validate_sbom_file(path)
        except ValidationError as exc:
            print(f"FAIL {path}: {exc}", file=sys.stderr)
            failed += 1
        else:
            print(f"OK   {path}")

    if failed:
        print(
            f"validate_sbom.py: {failed} / {len(files)} SBOM(s) failed validation",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
