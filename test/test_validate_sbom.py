"""Pytest coverage for ``scripts/validate_sbom.py``.

The validator backs CI's ``verify_sboms`` job in
``build_wheels.yml``. These tests pin the structural invariants the
generator commits to so that ``build_sbom.py`` cannot silently drift
into producing SBOMs that ``grype`` would still parse but that no
longer match the contract bocpy ships.

``scripts/`` is added to ``sys.path`` for the test session via
``pythonpath`` in ``pyproject.toml``'s ``[tool.pytest.ini_options]``
block, so ``build_sbom`` and ``validate_sbom`` import like any other
third-party module here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import build_sbom
import pytest
import validate_sbom


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_doc() -> dict:
    """A freshly built CycloneDX 1.6 SBOM produced by ``build_sbom.py``.

    Returned as a deep-copyable dict so individual tests can mutate one
    field without contaminating other tests.
    """
    return build_sbom.build_sbom_document(
        name="bocpy",
        version="0.6.0",
        description="probe",
        license_id="MIT",
        homepage_url="https://example.org",
        vcs_url="https://example.org/repo",
        git_commit="deadbeef",
        wheel_filename="bocpy-0.6.0-cp314-cp314-linux_x86_64.whl",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_validate_sbom_document_accepts_build_sbom_output():
    """The generator and validator must agree on the wire format."""
    validate_sbom.validate_sbom_document(_good_doc())


def test_validate_sbom_file_accepts_round_trip(tmp_path: Path):
    sbom = tmp_path / "bocpy.cdx.json"
    sbom.write_text(json.dumps(_good_doc()), encoding="utf-8")
    validate_sbom.validate_sbom_file(sbom)


# ---------------------------------------------------------------------------
# Header invariants (bomFormat / specVersion / serialNumber / version)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        # (key, replacement_value, expected_substring_in_error)
        ("bomFormat", "SPDX", "bomFormat"),
        ("specVersion", "1.5", "specVersion"),
        ("serialNumber", "urn:uuid:not-a-uuid", "serialNumber"),
        # version-1 UUID still rejected by the UUIDv5 regex
        ("serialNumber", "urn:uuid:11111111-1111-1111-1111-111111111111",
         "serialNumber"),
        ("version", 0, "version"),
        ("version", "1", "version"),
    ],
)
def test_validate_sbom_document_rejects_bad_header(mutation):
    key, value, needle = mutation
    doc = _good_doc()
    doc[key] = value
    with pytest.raises(validate_sbom.ValidationError, match=needle):
        validate_sbom.validate_sbom_document(doc)


# ---------------------------------------------------------------------------
# Metadata invariants
# ---------------------------------------------------------------------------


def test_metadata_must_be_object():
    doc = _good_doc()
    doc["metadata"] = []
    with pytest.raises(validate_sbom.ValidationError, match="metadata"):
        validate_sbom.validate_sbom_document(doc)


def test_timestamp_must_match_iso_z_format():
    doc = _good_doc()
    # Missing the ``Z`` suffix and using ``+00:00`` instead — same instant,
    # different lexical form. The validator pins the format because
    # build_sbom.py commits to a specific shape.
    doc["metadata"]["timestamp"] = "2026-05-28T12:00:00+00:00"
    with pytest.raises(validate_sbom.ValidationError, match="timestamp"):
        validate_sbom.validate_sbom_document(doc)


def test_tools_must_include_build_sbom_py():
    doc = _good_doc()
    doc["metadata"]["tools"]["components"] = [
        {"type": "application", "name": "something-else", "version": "1.0"}
    ]
    with pytest.raises(validate_sbom.ValidationError, match="build_sbom.py"):
        validate_sbom.validate_sbom_document(doc)


def test_root_component_bom_ref_and_purl_must_agree(tmp_path: Path):
    doc = _good_doc()
    doc["metadata"]["component"]["purl"] = "pkg:pypi/bocpy@0.6.1"
    with pytest.raises(validate_sbom.ValidationError, match="purl"):
        validate_sbom.validate_sbom_document(doc)


def test_root_component_purl_must_be_pypi_bocpy():
    doc = _good_doc()
    doc["metadata"]["component"]["bom-ref"] = "pkg:npm/bocpy@0.6.0"
    doc["metadata"]["component"]["purl"] = "pkg:npm/bocpy@0.6.0"
    with pytest.raises(validate_sbom.ValidationError, match="bom-ref"):
        validate_sbom.validate_sbom_document(doc)


def test_root_component_type_must_be_library():
    doc = _good_doc()
    doc["metadata"]["component"]["type"] = "application"
    with pytest.raises(validate_sbom.ValidationError, match="library"):
        validate_sbom.validate_sbom_document(doc)


# ---------------------------------------------------------------------------
# components / dependencies invariants
# ---------------------------------------------------------------------------


def test_dependencies_must_reference_root_component():
    doc = _good_doc()
    # Replace with a dependencies block that points at a different ref.
    doc["dependencies"] = [{"ref": "pkg:pypi/other@1.0", "dependsOn": []}]
    with pytest.raises(validate_sbom.ValidationError, match="dependencies"):
        validate_sbom.validate_sbom_document(doc)


def test_components_field_must_be_list():
    doc = _good_doc()
    doc["components"] = None
    with pytest.raises(validate_sbom.ValidationError, match="components"):
        validate_sbom.validate_sbom_document(doc)


# ---------------------------------------------------------------------------
# File-level + CLI
# ---------------------------------------------------------------------------


def test_validate_sbom_file_reports_invalid_json(tmp_path: Path):
    bad = tmp_path / "bad.cdx.json"
    bad.write_text("{this is not json", encoding="utf-8")
    with pytest.raises(validate_sbom.ValidationError, match="invalid JSON"):
        validate_sbom.validate_sbom_file(bad)


def test_main_accepts_directory_and_returns_zero_on_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    sboms = tmp_path / "sboms"
    sboms.mkdir()
    for tag in ("cp310", "cp314"):
        sbom = sboms / f"bocpy-0.6.0-{tag}.cdx.json"
        sbom.write_text(json.dumps(_good_doc()), encoding="utf-8")

    rc = validate_sbom.main([str(sboms)])
    assert rc == 0

    out = capsys.readouterr()
    assert "bocpy-0.6.0-cp310.cdx.json" in out.out
    assert "bocpy-0.6.0-cp314.cdx.json" in out.out


def test_main_returns_nonzero_when_any_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    good = tmp_path / "good.cdx.json"
    good.write_text(json.dumps(_good_doc()), encoding="utf-8")

    bad_doc = _good_doc()
    bad_doc["bomFormat"] = "wrong"
    bad = tmp_path / "bad.cdx.json"
    bad.write_text(json.dumps(bad_doc), encoding="utf-8")

    rc = validate_sbom.main([str(good), str(bad)])
    assert rc == 1

    err = capsys.readouterr().err
    assert "bad.cdx.json" in err
    assert "1 / 2 SBOM(s) failed validation" in err


def test_main_returns_nonzero_when_no_files_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = validate_sbom.main([str(empty)])
    assert rc == 1
    assert "no SBOM files found" in capsys.readouterr().err


def test_good_doc_helper_is_deep_copyable():
    """Sanity check that the helper returns fresh dicts, not aliased ones."""
    a = _good_doc()
    b = _good_doc()
    a["metadata"]["component"]["name"] = "mutated"
    assert b["metadata"]["component"]["name"] == "bocpy"
    # And the validator still accepts the unmutated copy:
    validate_sbom.validate_sbom_document(b)
    # While rejecting nothing here — exercising copy semantics only.
    _ = copy.deepcopy(a)
