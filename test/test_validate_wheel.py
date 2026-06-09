"""Tests for ``scripts/validate_wheel.py``.

The script wraps the verbatim-vendored PyPI/Warehouse functions
``validate_record`` and ``validate_entrypoints``. These tests
exercise the CLI driver end-to-end so a regression in either the
vendored layer or the driver is caught locally before CI.

The key regression captured here is the PEP 376 RECORD bug that
caused PyPI to send ``send_wheel_record_mismatch_email`` for every
bocpy-0.7.0 wheel: a wheel whose RECORD lists ZIP directory entries
(paths ending in ``/``) used to slip through our injector. The
matching test is :func:`test_record_with_directory_entries_is_rejected`.
"""

import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile

from _vendored_warehouse_wheel import (
    InvalidWheelEntryPointsError,
    InvalidWheelRecordError,
    MissingWheelRecordError,
)
import pytest
import validate_wheel

DIST = "bocpy"
VERSION = "0.6.0"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"
WHEEL_NAME = f"{DIST}-{VERSION}-cp314-cp314-manylinux_2_28_x86_64.whl"


def _record_row(arcname: str, data: bytes) -> tuple[str, str, str]:
    digest = hashlib.sha256(data).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return arcname, f"sha256={b64}", str(len(data))


def _wheel_metadata_entries() -> list[tuple[str, bytes]]:
    return [
        (f"{DIST}/__init__.py", b'__version__ = "0.6.0"\n'),
        (f"{DIST}/_core.so", b"# placeholder _core.so\n"),
        (
            f"{DIST_INFO}/METADATA",
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {VERSION}\n".encode("utf-8"),
        ),
        (
            f"{DIST_INFO}/WHEEL",
            (
                "Wheel-Version: 1.0\n"
                "Generator: bocpy-test\n"
                "Root-Is-Purelib: false\n"
                "Tag: cp314-cp314-manylinux_2_28_x86_64\n"
            ).encode("utf-8"),
        ),
    ]


def _write_wheel(
    path: Path,
    entries: list[tuple[str, bytes]],
    record_rows: list[tuple[str, str, str]] | None = None,
) -> None:
    """Write ``entries`` to ``path`` and generate a matching RECORD.

    If ``record_rows`` is provided, RECORD is built from it verbatim
    (used by negative-case tests to inject deliberate mismatches);
    otherwise RECORD is generated from ``entries`` honestly.
    """
    record_arc = f"{DIST_INFO}/RECORD"
    record_buf = io.StringIO()
    writer = csv.writer(record_buf, lineterminator="\n")
    if record_rows is None:
        for arcname, data in entries:
            writer.writerow(_record_row(arcname, data))
    else:
        for row in record_rows:
            writer.writerow(row)
    writer.writerow((record_arc, "", ""))

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for arcname, data in entries:
            wheel.writestr(arcname, data)
        wheel.writestr(record_arc, record_buf.getvalue())


def _write_wheel_raw(
    path: Path,
    zip_entries: list[tuple[str, bytes]],
    record_text: str,
) -> None:
    """Lowest-level builder: write ``zip_entries`` and RECORD verbatim.

    Use this when a test needs both sides of the cross-check
    controlled independently (e.g. directory entries in the ZIP and
    directory rows in RECORD).
    """
    record_arc = f"{DIST_INFO}/RECORD"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for arcname, data in zip_entries:
            wheel.writestr(arcname, data)
        wheel.writestr(record_arc, record_text)


def test_clean_wheel_passes(tmp_path):
    """A faithfully-built probe wheel passes both checks."""
    wheel_path = tmp_path / WHEEL_NAME
    _write_wheel(wheel_path, _wheel_metadata_entries())

    validate_wheel.validate_wheel_file(wheel_path)


def test_main_returns_zero_on_clean_wheel(tmp_path, capsys):
    wheel_path = tmp_path / WHEEL_NAME
    _write_wheel(wheel_path, _wheel_metadata_entries())

    rc = validate_wheel.main([str(wheel_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_main_accepts_directory_input(tmp_path, capsys):
    """Passing a directory expands to every *.whl in it."""
    (tmp_path / WHEEL_NAME).touch()
    wheel_a = tmp_path / WHEEL_NAME
    wheel_b = tmp_path / f"{DIST}-{VERSION}-cp314-cp314-linux_aarch64.whl"
    _write_wheel(wheel_a, _wheel_metadata_entries())
    _write_wheel(wheel_b, _wheel_metadata_entries())

    rc = validate_wheel.main([str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("OK") == 2


def test_record_with_directory_entries_is_rejected(tmp_path):
    """Regression for the 0.7.0 PyPI warning.

    A wheel whose RECORD lists a directory entry (path ending ``/``)
    is what PyPI's ``validate_record`` flags. PyPI strips directory
    entries from the wheel side before set comparison; the RECORD
    side keeps them; the sets diverge ⇒ rejection.
    """
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries()
    bad_rows = [_record_row(arc, data) for arc, data in entries]
    empty_hash = (
        "sha256="
        + base64.urlsafe_b64encode(hashlib.sha256(b"").digest()).rstrip(b"=").decode("ascii")
    )
    bad_rows.append((f"{DIST}/", empty_hash, "0"))
    _write_wheel(wheel_path, entries, record_rows=bad_rows)

    with pytest.raises(InvalidWheelRecordError) as excinfo:
        validate_wheel.validate_wheel_file(wheel_path)
    assert f"{DIST}/" in str(excinfo.value)


def test_record_missing_an_entry_is_rejected(tmp_path):
    """An honest file present in the ZIP but absent from RECORD fails."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries()
    short_rows = [_record_row(arc, data) for arc, data in entries[:-1]]
    _write_wheel(wheel_path, entries, record_rows=short_rows)

    with pytest.raises(InvalidWheelRecordError):
        validate_wheel.validate_wheel_file(wheel_path)


def test_record_with_phantom_entry_is_rejected(tmp_path):
    """An entry listed in RECORD but absent from the ZIP fails."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries()
    rows = [_record_row(arc, data) for arc, data in entries]
    rows.append((f"{DIST}/ghost.py", _record_row("x", b"")[1], "0"))
    _write_wheel(wheel_path, entries, record_rows=rows)

    with pytest.raises(InvalidWheelRecordError):
        validate_wheel.validate_wheel_file(wheel_path)


def test_missing_record_file_raises(tmp_path):
    """A wheel with no RECORD raises MissingWheelRecordError."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries()
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for arcname, data in entries:
            wheel.writestr(arcname, data)

    with pytest.raises(MissingWheelRecordError):
        validate_wheel.validate_wheel_file(wheel_path)


def test_record_with_jws_signature_is_exempt(tmp_path):
    """RECORD.jws is in the ZIP but must NOT appear in RECORD; this passes."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries() + [
        (f"{DIST_INFO}/RECORD.jws", b"<signature bytes>"),
    ]
    visible_entries = entries[:-1]
    _write_wheel(wheel_path, visible_entries, record_rows=None)
    with zipfile.ZipFile(wheel_path, "a", zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr(f"{DIST_INFO}/RECORD.jws", b"<signature bytes>")

    validate_wheel.validate_wheel_file(wheel_path)


def test_main_returns_nonzero_on_failure(tmp_path, capsys):
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries()
    bad_rows = [_record_row(arc, data) for arc, data in entries]
    bad_rows.append((f"{DIST}/", "sha256=" + "A" * 43, "0"))
    _write_wheel(wheel_path, entries, record_rows=bad_rows)

    rc = validate_wheel.main([str(wheel_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "RECORD mismatch" in err


def test_valid_entry_points_pass(tmp_path):
    """A well-formed entry_points.txt is accepted."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries() + [
        (
            f"{DIST_INFO}/entry_points.txt",
            b"[console_scripts]\nbocpy-tool = bocpy.cli:main\n",
        ),
    ]
    _write_wheel(wheel_path, entries)

    validate_wheel.validate_wheel_file(wheel_path)


def test_malformed_entry_points_rejected(tmp_path):
    """A non-INI entry_points.txt raises InvalidWheelEntryPointsError."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries() + [
        (f"{DIST_INFO}/entry_points.txt", b"this is not ini at all"),
    ]
    _write_wheel(wheel_path, entries)

    with pytest.raises(InvalidWheelEntryPointsError):
        validate_wheel.validate_wheel_file(wheel_path)


def test_entry_point_with_invalid_name_rejected(tmp_path):
    """Entry-point names with path separators are forbidden."""
    wheel_path = tmp_path / WHEEL_NAME
    entries = _wheel_metadata_entries() + [
        (
            f"{DIST_INFO}/entry_points.txt",
            b"[console_scripts]\n../escape = bocpy.cli:main\n",
        ),
    ]
    _write_wheel(wheel_path, entries)

    with pytest.raises(InvalidWheelEntryPointsError):
        validate_wheel.validate_wheel_file(wheel_path)
