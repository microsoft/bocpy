"""Tests for ``scripts/build_sbom.py``.

The script is not part of the installed bocpy package — it lives at
``scripts/`` and is invoked by ``cibuildwheel`` via
``CIBW_REPAIR_WHEEL_COMMAND``. ``scripts/`` is added to ``sys.path``
for the test session via ``pythonpath`` in ``pyproject.toml``'s
``[tool.pytest.ini_options]`` block, so it imports like any other
third-party module here.
"""

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

import build_sbom
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


DIST = "bocpy"
VERSION = "0.6.0"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"

PROBE_METADATA = (
    "Metadata-Version: 2.1\n"
    f"Name: {DIST}\n"
    f"Version: {VERSION}\n"
).encode("utf-8")

PROBE_WHEEL_META = (
    "Wheel-Version: 1.0\n"
    "Generator: bocpy-test\n"
    "Root-Is-Purelib: false\n"
    "Tag: cp314-cp314-manylinux_2_28_x86_64\n"
).encode("utf-8")

PROBE_PAYLOAD = b"# placeholder for _core.so\n"


def _record_row(arcname: str, data: bytes) -> tuple[str, str, str]:
    digest = hashlib.sha256(data).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return arcname, f"sha256={b64}", str(len(data))


def _build_probe_wheel(path: Path) -> None:
    """Create a minimal but valid wheel at ``path``."""
    entries = [
        (f"{DIST}/__init__.py", b'__version__ = "0.6.0"\n'),
        (f"{DIST}/_core.so", PROBE_PAYLOAD),
        (f"{DIST_INFO}/METADATA", PROBE_METADATA),
        (f"{DIST_INFO}/WHEEL", PROBE_WHEEL_META),
    ]
    record_buf = io.StringIO()
    writer = csv.writer(record_buf, lineterminator="\n")
    for arcname, data in entries:
        writer.writerow(_record_row(arcname, data))
    writer.writerow((f"{DIST_INFO}/RECORD", "", ""))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for arcname, data in entries:
            wheel.writestr(arcname, data)
        wheel.writestr(f"{DIST_INFO}/RECORD", record_buf.getvalue())


def _read_record_rows(wheel_path: Path) -> list[tuple[str, str, str]]:
    with zipfile.ZipFile(wheel_path, "r") as wheel:
        record_arc = f"{DIST_INFO}/RECORD"
        text = wheel.read(record_arc).decode("utf-8")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        path, h, size = row
        rows.append((path, h, size))
    return rows


def test_build_sbom_document_minimal_shape():
    """A no-extras document carries the required CycloneDX fields."""
    doc = build_sbom.build_sbom_document(
        name="bocpy",
        version="0.6.0",
        description="desc",
        license_id="MIT",
        homepage_url="https://example.invalid/",
        vcs_url="https://example.invalid/vcs",
        git_commit=None,
        wheel_filename=None,
    )

    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.6"
    assert doc["version"] == 1
    assert doc["serialNumber"].startswith("urn:uuid:")

    root = doc["metadata"]["component"]
    assert root["name"] == "bocpy"
    assert root["version"] == "0.6.0"
    assert root["purl"] == "pkg:pypi/bocpy@0.6.0"
    assert root["bom-ref"] == "pkg:pypi/bocpy@0.6.0"
    assert root["licenses"] == [{"license": {"id": "MIT"}}]
    assert "properties" not in root

    assert doc["components"] == []
    assert doc["dependencies"] == [
        {"ref": "pkg:pypi/bocpy@0.6.0", "dependsOn": []}
    ]


def test_build_sbom_document_attaches_properties_when_supplied():
    """Git SHA and wheel filename land as root-component properties."""
    doc = build_sbom.build_sbom_document(
        name="bocpy",
        version="0.6.0",
        description="desc",
        license_id="MIT",
        homepage_url="https://example.invalid/",
        vcs_url="https://example.invalid/vcs",
        git_commit="deadbeef" * 5,
        wheel_filename="bocpy-0.6.0-cp314-cp314-linux_x86_64.whl",
    )
    props = doc["metadata"]["component"]["properties"]
    by_name = {p["name"]: p["value"] for p in props}
    assert by_name["cdx:python:git_commit"] == "deadbeef" * 5
    assert (
        by_name["cdx:python:wheel_filename"]
        == "bocpy-0.6.0-cp314-cp314-linux_x86_64.whl"
    )


def test_build_sbom_document_serialises_to_stable_json():
    """``json.dumps`` with the documented settings round-trips cleanly."""
    doc = build_sbom.build_sbom_document(
        name="bocpy",
        version="0.6.0",
        description="desc",
        license_id="MIT",
        homepage_url="https://example.invalid/",
        vcs_url="https://example.invalid/vcs",
        git_commit=None,
        wheel_filename=None,
    )
    serialised = json.dumps(doc, indent=2, sort_keys=True)
    reloaded = json.loads(serialised)
    assert doc == reloaded


def test_inject_sbom_round_trip(tmp_path: Path) -> None:
    """Injecting an SBOM lands at the PEP 770 path with a correct RECORD."""
    wheel_path = tmp_path / f"{DIST}-{VERSION}-cp314-cp314-linux_x86_64.whl"
    _build_probe_wheel(wheel_path)

    doc = build_sbom.build_sbom_document(
        name="bocpy",
        version="0.6.0",
        description="desc",
        license_id="MIT",
        homepage_url="https://example.invalid/",
        vcs_url="https://example.invalid/vcs",
        git_commit="cafebabe" * 5,
        wheel_filename=wheel_path.name,
    )
    sbom_bytes = (
        json.dumps(doc, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    build_sbom.inject_sbom_into_wheel(wheel_path, sbom_bytes)

    sbom_arc = f"{DIST_INFO}/sboms/bocpy.cdx.json"

    with zipfile.ZipFile(wheel_path, "r") as wheel:
        names = wheel.namelist()
        assert sbom_arc in names
        assert f"{DIST}/__init__.py" in names
        assert f"{DIST}/_core.so" in names
        assert wheel.read(f"{DIST}/_core.so") == PROBE_PAYLOAD
        assert wheel.read(sbom_arc) == sbom_bytes

    rows = _read_record_rows(wheel_path)
    paths = [r[0] for r in rows]
    assert sbom_arc in paths
    assert rows[-1] == (f"{DIST_INFO}/RECORD", "", "")
    with zipfile.ZipFile(wheel_path, "r") as wheel:
        for path, hash_spec, size in rows[:-1]:
            data = wheel.read(path)
            digest = hashlib.sha256(data).digest()
            expected = (
                "sha256="
                + base64.urlsafe_b64encode(digest)
                .rstrip(b"=")
                .decode("ascii")
            )
            assert hash_spec == expected, path
            assert int(size) == len(data), path


def test_inject_sbom_replaces_existing_sbom(tmp_path: Path) -> None:
    """Re-running ``inject`` overwrites a previously-embedded SBOM."""
    wheel_path = tmp_path / f"{DIST}-{VERSION}-cp314-cp314-linux_x86_64.whl"
    _build_probe_wheel(wheel_path)

    build_sbom.inject_sbom_into_wheel(wheel_path, b'{"first": true}\n')
    build_sbom.inject_sbom_into_wheel(wheel_path, b'{"second": true}\n')

    with zipfile.ZipFile(wheel_path, "r") as wheel:
        sbom_arc = f"{DIST_INFO}/sboms/bocpy.cdx.json"
        assert wheel.namelist().count(sbom_arc) == 1
        assert wheel.read(sbom_arc) == b'{"second": true}\n'


def test_inject_sbom_missing_wheel_raises(tmp_path: Path) -> None:
    """A missing wheel surfaces a ``FileNotFoundError``."""
    with pytest.raises(FileNotFoundError):
        build_sbom.inject_sbom_into_wheel(
            tmp_path / "absent.whl", b"{}"
        )


def test_inject_sbom_does_not_leave_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If injection fails midway, no ``*.whl.tmp`` is left behind."""
    wheel_path = tmp_path / f"{DIST}-{VERSION}-cp314-cp314-linux_x86_64.whl"
    _build_probe_wheel(wheel_path)

    def _boom(src: str, dst: str) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(build_sbom.shutil, "move", _boom)

    with pytest.raises(OSError, match="disk full"):
        build_sbom.inject_sbom_into_wheel(wheel_path, b'{"x": 1}')

    leftover = list(tmp_path.glob("*.whl.tmp"))
    assert leftover == [], f"tmp files leaked: {leftover}"


def test_read_pyproject_metadata_round_trip() -> None:
    """The metadata reader returns the fields the SBOM generator needs."""
    meta = build_sbom._read_pyproject_metadata(REPO_ROOT)
    assert meta["name"] == "bocpy"
    assert meta["version"]
    assert meta["description"]
    assert meta["license"] == "MIT"
    assert meta["homepage"].startswith("https://")
    assert meta["vcs"].startswith("https://")


def test_cli_inject_copy_to_leaves_original_alone(tmp_path: Path) -> None:
    """``inject --copy-to DIR`` copies, injects into the copy, and leaves the source pristine."""
    src_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    src_dir.mkdir()

    wheel_path = src_dir / f"{DIST}-{VERSION}-cp314-cp314-linux_x86_64.whl"
    _build_probe_wheel(wheel_path)
    original_bytes = wheel_path.read_bytes()

    rc = build_sbom.main(
        [
            "--project-root",
            str(REPO_ROOT),
            "inject",
            str(wheel_path),
            "--copy-to",
            str(dest_dir),
            "--git-commit",
            "0" * 40,
        ]
    )
    assert rc == 0

    assert wheel_path.read_bytes() == original_bytes

    copied = dest_dir / wheel_path.name
    assert copied.is_file()
    with zipfile.ZipFile(copied, "r") as wheel:
        names = wheel.namelist()
        assert f"{DIST_INFO}/sboms/bocpy.cdx.json" in names
        sbom_doc = json.loads(
            wheel.read(f"{DIST_INFO}/sboms/bocpy.cdx.json")
        )
    assert sbom_doc["bomFormat"] == "CycloneDX"
    assert sbom_doc["specVersion"] == "1.6"
    props = {
        p["name"]: p["value"]
        for p in sbom_doc["metadata"]["component"].get("properties", [])
    }
    assert props["cdx:python:git_commit"] == "0" * 40
    assert props["cdx:python:wheel_filename"] == wheel_path.name


def _build_attr_probe_wheel(path: Path) -> None:
    """Build a wheel whose entries exercise the preservation contract.

    Entries:

    * a Unix symlink (``S_IFLNK`` in the high bits of ``external_attr``)
      whose payload is the link target;
    * an executable shared object (``0o755`` mode bits) stored
      uncompressed (``ZIP_STORED``);
    * a regular module marked with ``create_system = 3`` (Unix) and a
      pinned ``date_time``;
    * the minimal dist-info needed to look like a wheel.
    """
    pinned_dt = (2024, 1, 1, 12, 0, 0)

    init_info = zipfile.ZipInfo(filename=f"{DIST}/__init__.py", date_time=pinned_dt)
    init_info.compress_type = zipfile.ZIP_DEFLATED
    init_info.create_system = 3
    init_info.external_attr = (0o644 & 0xFFFF) << 16

    so_info = zipfile.ZipInfo(filename=f"{DIST}/_core.so", date_time=pinned_dt)
    so_info.compress_type = zipfile.ZIP_STORED
    so_info.create_system = 3
    so_info.external_attr = (0o755 & 0xFFFF) << 16

    link_info = zipfile.ZipInfo(
        filename=f"{DIST}/libprobe.so.1", date_time=pinned_dt
    )
    link_info.compress_type = zipfile.ZIP_STORED
    link_info.create_system = 3
    link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
    link_target = b"libprobe.so.1.2.3"

    metadata_info = zipfile.ZipInfo(
        filename=f"{DIST_INFO}/METADATA", date_time=pinned_dt
    )
    metadata_info.compress_type = zipfile.ZIP_DEFLATED
    metadata_info.create_system = 3
    metadata_info.external_attr = (0o644 & 0xFFFF) << 16

    wheel_meta_info = zipfile.ZipInfo(
        filename=f"{DIST_INFO}/WHEEL", date_time=pinned_dt
    )
    wheel_meta_info.compress_type = zipfile.ZIP_DEFLATED
    wheel_meta_info.create_system = 3
    wheel_meta_info.external_attr = (0o644 & 0xFFFF) << 16

    entries: list[tuple[zipfile.ZipInfo, bytes]] = [
        (init_info, b'__version__ = "0.6.0"\n'),
        (so_info, PROBE_PAYLOAD),
        (link_info, link_target),
        (metadata_info, PROBE_METADATA),
        (wheel_meta_info, PROBE_WHEEL_META),
    ]
    record_buf = io.StringIO()
    writer = csv.writer(record_buf, lineterminator="\n")
    for info, data in entries:
        writer.writerow(_record_row(info.filename, data))
    writer.writerow((f"{DIST_INFO}/RECORD", "", ""))
    record_info = zipfile.ZipInfo(
        filename=f"{DIST_INFO}/RECORD", date_time=pinned_dt
    )
    record_info.compress_type = zipfile.ZIP_DEFLATED
    record_info.create_system = 3
    record_info.external_attr = (0o644 & 0xFFFF) << 16

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for info, data in entries:
            wheel.writestr(info, data)
        wheel.writestr(record_info, record_buf.getvalue())


def test_inject_sbom_preserves_per_entry_zip_attributes(tmp_path: Path) -> None:
    """``external_attr``/``create_system``/``compress_type``/``date_time`` round-trip.

    A naive injector that rebuilt every entry with
    ``writestr(arcname, data)`` would silently drop all four fields.
    For symlinked SONAMEs left by auditwheel/delocate, that turns
    the link into a regular file containing the link's text.
    """
    wheel_path = tmp_path / f"{DIST}-{VERSION}-cp314-cp314-manylinux_2_28_x86_64.whl"
    _build_attr_probe_wheel(wheel_path)

    with zipfile.ZipFile(wheel_path, "r") as src:
        before = {info.filename: info for info in src.infolist()}

    build_sbom.inject_sbom_into_wheel(wheel_path, b'{"v": 1}\n')

    with zipfile.ZipFile(wheel_path, "r") as wheel:
        after = {info.filename: info for info in wheel.infolist()}

    for arcname in before:
        assert arcname in after, f"entry {arcname!r} dropped by injector"

    sym = after[f"{DIST}/libprobe.so.1"]
    sym_mode = (sym.external_attr >> 16) & 0xFFFF
    assert stat.S_ISLNK(sym_mode), (
        f"S_IFLNK lost on symlink entry: external_attr=0x{sym.external_attr:08x}, "
        f"high-bits mode=0o{sym_mode:o}"
    )
    assert (sym_mode & 0o777) == 0o777

    so = after[f"{DIST}/_core.so"]
    assert ((so.external_attr >> 16) & 0o777) == 0o755
    assert so.compress_type == zipfile.ZIP_STORED, (
        f"ZIP_STORED entry was recompressed: got {so.compress_type!r}"
    )

    init = after[f"{DIST}/__init__.py"]
    init_src = before[f"{DIST}/__init__.py"]
    assert ((init.external_attr >> 16) & 0o777) == 0o644
    assert init.create_system == init_src.create_system == 3
    assert init.date_time == init_src.date_time == (2024, 1, 1, 12, 0, 0)

    sbom_arc = f"{DIST_INFO}/sboms/bocpy.cdx.json"
    assert sbom_arc in after
    with zipfile.ZipFile(wheel_path, "r") as wheel:
        assert wheel.read(sbom_arc) == b'{"v": 1}\n'

    rows = _read_record_rows(wheel_path)
    paths = [r[0] for r in rows]
    assert f"{DIST}/libprobe.so.1" in paths
    assert sbom_arc in paths
    assert rows[-1] == (f"{DIST_INFO}/RECORD", "", "")


def _sbom_inputs() -> dict:
    """Fixed input set for the determinism tests."""
    return dict(
        name="bocpy",
        version="0.6.0",
        description="probe",
        license_id="MIT",
        homepage_url="https://example.org",
        vcs_url="https://example.org/repo",
        git_commit="deadbeef" * 5,
        wheel_filename="bocpy-0.6.0-cp314-cp314-linux_x86_64.whl",
    )


def test_build_sbom_document_is_byte_identical_for_same_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same inputs + ``SOURCE_DATE_EPOCH`` => byte-identical bytes.

    A naive generator using ``uuid.uuid4()`` and ``now()`` would
    produce different SBOM payloads for two builds of byte-identical
    source, drifting the wheel hash across rebuilds. The deterministic
    UUIDv5 serial + ``SOURCE_DATE_EPOCH`` path keep them byte-stable.
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1704110400")

    doc1 = build_sbom.build_sbom_document(**_sbom_inputs())
    doc2 = build_sbom.build_sbom_document(**_sbom_inputs())

    bytes1 = (json.dumps(doc1, indent=2, sort_keys=True) + "\n").encode()
    bytes2 = (json.dumps(doc2, indent=2, sort_keys=True) + "\n").encode()
    assert bytes1 == bytes2, (
        "two SBOMs built from identical inputs must be byte-identical; "
        f"diverged at serialNumber={doc1['serialNumber']!r} vs "
        f"{doc2['serialNumber']!r}, timestamp={doc1['metadata']['timestamp']!r} "
        f"vs {doc2['metadata']['timestamp']!r}"
    )

    assert doc1["metadata"]["timestamp"] == "2024-01-01T12:00:00Z"

    serial = doc1["serialNumber"]
    assert serial.startswith("urn:uuid:")
    assert serial[23] == "5", (
        f"expected UUIDv5 serial, got version digit {serial[23]!r} in {serial!r}"
    )


def test_build_sbom_document_serial_changes_with_inputs() -> None:
    """A different git_commit produces a different serialNumber.

    The whole point of deriving the serial from the inputs is that the
    UUID changes when meaningful inputs change (so two SBOMs that
    *should* be distinguishable have distinct serial numbers).
    """
    base = _sbom_inputs()
    doc_a = build_sbom.build_sbom_document(**base)
    doc_b = build_sbom.build_sbom_document(
        **{**base, "git_commit": "cafebabe" * 5}
    )
    assert doc_a["serialNumber"] != doc_b["serialNumber"]

    doc_c = build_sbom.build_sbom_document(
        **{**base, "wheel_filename": "bocpy-0.6.0-cp314-cp314-win_amd64.whl"}
    )
    assert doc_a["serialNumber"] != doc_c["serialNumber"]


def test_build_sbom_document_falls_back_to_now_without_source_date_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``SOURCE_DATE_EPOCH`` => current UTC timestamp.

    Dev / interactive runs must still produce a valid SBOM even
    without the reproducible-build env var. We check the shape, not
    the exact value (which is `now()`).
    """
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    doc = build_sbom.build_sbom_document(**_sbom_inputs())
    ts = doc["metadata"]["timestamp"]
    assert len(ts) == 20
    assert ts[4] == ts[7] == "-"
    assert ts[10] == "T"
    assert ts[13] == ts[16] == ":"
    assert ts.endswith("Z")


def test_build_sbom_document_rejects_malformed_source_date_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-integer ``SOURCE_DATE_EPOCH`` raises rather than silently fall back.

    The reproducible-build spec mandates a hard error so CI surfaces a
    misconfigured env var instead of producing a non-deterministic
    SBOM under the user's nose.
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    with pytest.raises(ValueError, match="SOURCE_DATE_EPOCH"):
        build_sbom.build_sbom_document(**_sbom_inputs())


def test_validate_sbom_accepts_deterministic_serial() -> None:
    """End-to-end: generator output round-trips through the validator.

    Without this, the generator's deterministic UUIDv5 serial and the
    validator's matching regex could drift apart silently; CI's
    ``verify_sboms`` job runs the validator over every produced SBOM,
    so a regression here would break the release pipeline.
    """
    import validate_sbom

    doc = build_sbom.build_sbom_document(**_sbom_inputs())
    validate_sbom.validate_sbom_document(doc)


def test_cli_generate_requires_distinguisher_when_neither_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """``generate`` must fail when neither --git-commit nor --wheel-filename is set.

    Without the guard, ``python scripts/build_sbom.py generate`` invoked
    with no flags and no ``$GITHUB_SHA`` produces a UUIDv5 that
    collapses to the same value for every wheel of the same name+version
    — defeating the per-wheel-identifier purpose of the
    deterministic serial.
    """
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    out_path = tmp_path / "sbom.cdx.json"

    rc = build_sbom.main(
        [
            "--project-root",
            str(REPO_ROOT),
            "generate",
            "--output",
            str(out_path),
        ]
    )

    assert rc == 2, "expected non-zero exit when no distinguisher is supplied"
    assert not out_path.exists(), (
        "generate must not produce an SBOM when the distinguisher guard fires"
    )
    captured = capsys.readouterr()
    assert "wheel_filename" in captured.err.lower() or "wheel-filename" in captured.err.lower()
    assert "git-commit" in captured.err.lower() or "git_commit" in captured.err.lower()


def test_cli_generate_accepts_git_commit_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--git-commit`` alone is a sufficient distinguisher."""
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    out_path = tmp_path / "sbom.cdx.json"

    rc = build_sbom.main(
        [
            "--project-root",
            str(REPO_ROOT),
            "generate",
            "--output",
            str(out_path),
            "--git-commit",
            "deadbeef" * 5,
        ]
    )
    assert rc == 0
    assert out_path.is_file()
    doc = json.loads(out_path.read_text())
    assert doc["serialNumber"].startswith("urn:uuid:")


def test_cli_generate_accepts_github_sha_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``$GITHUB_SHA`` from the environment counts as a distinguisher."""
    monkeypatch.setenv("GITHUB_SHA", "feedface" * 5)
    out_path = tmp_path / "sbom.cdx.json"

    rc = build_sbom.main(
        [
            "--project-root",
            str(REPO_ROOT),
            "generate",
            "--output",
            str(out_path),
        ]
    )
    assert rc == 0
    assert out_path.is_file()


def test_cli_generate_accepts_wheel_filename_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--wheel-filename`` alone is a sufficient distinguisher."""
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    out_path = tmp_path / "sbom.cdx.json"

    rc = build_sbom.main(
        [
            "--project-root",
            str(REPO_ROOT),
            "generate",
            "--output",
            str(out_path),
            "--wheel-filename",
            f"{DIST}-{VERSION}-cp314-cp314-linux_x86_64.whl",
        ]
    )
    assert rc == 0
    assert out_path.is_file()


def _build_probe_wheel_with_dir_entries(path: Path) -> None:
    """Build a probe wheel that includes explicit ZIP directory entries.

    ``auditwheel`` (and ``delocate``) emit one ZIP entry per directory
    in the repaired wheel — ``bocpy/``, ``bocpy/include/``,
    ``bocpy-X.Y.Z.dist-info/`` and so on. ``zipfile.writestr(str, ...)``
    does not produce these, so the default ``_build_probe_wheel`` did
    not exercise the code path that broke 0.7.0. This helper does.
    """
    file_entries = [
        (f"{DIST}/__init__.py", b'__version__ = "0.6.0"\n'),
        (f"{DIST}/_core.so", PROBE_PAYLOAD),
        (f"{DIST_INFO}/METADATA", PROBE_METADATA),
        (f"{DIST_INFO}/WHEEL", PROBE_WHEEL_META),
    ]
    dir_entries = [
        f"{DIST}/",
        f"{DIST_INFO}/",
    ]

    record_buf = io.StringIO()
    writer = csv.writer(record_buf, lineterminator="\n")
    for arcname, data in file_entries:
        writer.writerow(_record_row(arcname, data))
    for arcname in dir_entries:
        writer.writerow((arcname, "", "0"))
    writer.writerow((f"{DIST_INFO}/RECORD", "", ""))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for arcname in dir_entries:
            info = zipfile.ZipInfo(filename=arcname)
            info.external_attr = (0o040755 << 16) | 0x10
            wheel.writestr(info, b"")
        for arcname, data in file_entries:
            wheel.writestr(arcname, data)
        wheel.writestr(f"{DIST_INFO}/RECORD", record_buf.getvalue())


def test_inject_sbom_strips_directory_entries(tmp_path: Path) -> None:
    """Regression: an SBOM-injected wheel must pass PyPI's validate_record.

    A wheel produced by ``auditwheel``/``delocate`` carries explicit
    ZIP directory entries. The pre-fix injector copied every entry —
    directories included — into the new RECORD with empty SHA-256
    rows. PyPI's ``validate_record`` strips trailing-slash entries
    from the ZIP side before comparing as a set, so those rows
    became phantom RECORD entries and triggered
    ``send_wheel_record_mismatch_email`` for every 0.7.0 wheel.

    After the fix, the injector drops directory entries entirely.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from _vendored_warehouse_wheel import (  # type: ignore
            InvalidWheelRecordError,
            validate_record,
        )
    finally:
        sys.path.pop(0)

    wheel_path = (
        tmp_path
        / f"{DIST}-{VERSION}-cp314-cp314-manylinux_2_28_x86_64.whl"
    )
    _build_probe_wheel_with_dir_entries(wheel_path)

    with pytest.raises(InvalidWheelRecordError):
        validate_record(str(wheel_path))

    build_sbom.inject_sbom_into_wheel(wheel_path, b'{"v": 1}\n')

    validate_record(str(wheel_path))

    with zipfile.ZipFile(wheel_path, "r") as wheel:
        for info in wheel.infolist():
            assert not info.is_dir(), (
                f"directory entry survived injection: {info.filename!r}"
            )
        record_text = wheel.read(
            f"{DIST_INFO}/RECORD"
        ).decode("utf-8")

    for row in csv.reader(io.StringIO(record_text)):
        if not row:
            continue
        assert not row[0].endswith("/"), (
            f"RECORD still lists a directory row: {row[0]!r}"
        )
