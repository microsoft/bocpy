# SPDX-License-Identifier: Apache-2.0
#
# Verbatim vendor of the wheel-validation primitives PyPI itself
# runs at upload time. Keeping a byte-for-byte copy in tree means
# local pre-flight matches what Warehouse will do, with zero risk
# of drift from a reimplementation.
#
# Upstream:  https://github.com/pypi/warehouse
# Source:    warehouse/utils/wheel.py
# Commit:    c91f60560e908b81b94f144b2a77a95437455765   (main, 2026-05-26)
# License:   Apache-2.0  (https://www.apache.org/licenses/LICENSE-2.0)
# Synced:    2026-05-29
#
# Refresh procedure:
#   1. Pull the same three functions and two exceptions verbatim from
#      the upstream URL above at HEAD of `main`.
#   2. Update the Commit / Synced lines.
#   3. Re-run ``pytest test/test_validate_wheel.py``.
#
# Only the slices needed for local wheel pre-flight are vendored:
# ``validate_record`` and ``validate_entrypoints`` (the two checks
# Warehouse runs on every upload), their helper ``_zip_filename_is_dir``,
# and the three internal exception classes they raise. Everything else
# in upstream wheel.py (platform-tag prettifying, etc.) is irrelevant
# to the upload-rejection check and is omitted.

import configparser
import csv
import os
import re
import zipfile


class MissingWheelRecordError(Exception):
    """Internal exception used by this module"""


class InvalidWheelRecordError(Exception):
    """Internal exception used by this module"""


class InvalidWheelEntryPointsError(Exception):
    """Internal exception used by this module"""


def _zip_filename_is_dir(filename: str) -> bool:
    """Return True if this ZIP archive member is a directory."""
    return filename.endswith(("/", "\\"))


def validate_record(wheel_filepath: str) -> bool:
    """
    Extract RECORD file from a wheel and check the ZIP archive contents
    against the files listed in the RECORD. Mismatches are reported via email.
    """
    filename = os.path.basename(wheel_filepath)
    name, version, _ = filename.split("-", 2)
    record_filename = f"{name}-{version}.dist-info/RECORD"
    # Files that must be missing from 'RECORD',
    # so we ignore them when cross-checking.
    record_exemptions = {
        f"{name}-{version}.dist-info/RECORD.jws",
        f"{name}-{version}.dist-info/RECORD.p7s",
    }
    try:
        with zipfile.ZipFile(wheel_filepath) as zfp:
            wheel_record_contents = zfp.read(record_filename).decode()
            record_entries = {
                fn.replace("\\", "/")  # Normalize Windows path separators.
                for fn, *_ in csv.reader(wheel_record_contents.splitlines())
            }
            wheel_entries = {
                fn
                for fn in zfp.namelist()
                if not _zip_filename_is_dir(fn) and fn not in record_exemptions
            }
    except (UnicodeError, KeyError, csv.Error):
        raise MissingWheelRecordError
    if record_entries != wheel_entries:
        record_is_missing = wheel_entries - record_entries
        wheel_is_missing = record_entries - wheel_entries
        raise InvalidWheelRecordError(
            (f"Record is missing {record_is_missing})" if record_is_missing else "")
            + ("; " if record_is_missing and wheel_is_missing else "")
            + (f"Wheel is missing {wheel_is_missing})" if wheel_is_missing else "")
        )
    return True

# See:
# https://packaging.python.org/en/latest/specifications/entry-points/#data-model
_ENTRY_POINT_NAME_RE = re.compile(r"[\w.-]+")


def _validate_section(section: configparser.SectionProxy):
    """
    Validate the entry point names in a single section.
    """
    _ENTRY_POINT_NAME_RE = re.compile(r"[\w.-]+")
    for ep_name in section:
        if _ENTRY_POINT_NAME_RE.fullmatch(ep_name) is None:
            raise InvalidWheelEntryPointsError(
                f"Invalid entry point name {ep_name!r} in {section.name!r}"
            )


def validate_entrypoints(wheel_filepath: str) -> bool:
    """
    Extract `entry_points.txt` from a wheel and check that it is valid.

    Current validity checks include being a well-formed INI file
    (matching the Entry Points specification's constraints) and
    that all `console_scripts` and `gui_scripts` entry points have names
    that do not contain absolute or relative path components.

    Validation errors are not currently reported via email.
    """

    # See:
    # <https://packaging.python.org/en/latest/specifications/entry-points/#file-format>
    class CaseSensitiveConfigParser(configparser.ConfigParser):
        optionxform = staticmethod(str)  # type: ignore[assignment]

    filename = os.path.basename(wheel_filepath)
    name, version, _ = filename.split("-", 2)
    entry_points_filename = f"{name}-{version}.dist-info/entry_points.txt"

    # A wheel might not have an `entry_points.txt` file.
    try:
        with zipfile.ZipFile(wheel_filepath) as zfp:
            entry_points_contents = zfp.read(entry_points_filename).decode()
    except KeyError:
        return True
    except UnicodeError:
        # `entry_points.txt` must be decodable as UTF-8.
        raise InvalidWheelEntryPointsError("entry_points.txt is not decodable as UTF-8")

    # The Entry Points specification requires `=` as the delimiter.
    parser = CaseSensitiveConfigParser(delimiters=("=",))
    try:
        parser.read_string(entry_points_contents)
    except configparser.Error as error:
        raise InvalidWheelEntryPointsError(
            f"entry_points.txt is not a valid INI file: {error!r}"
        )

    for section_name in ("console_scripts", "gui_scripts"):
        try:
            section = parser[section_name]
        except KeyError:
            # `entry_points.txt` might not have these sections.
            continue
        _validate_section(section)

        # TODO: We could consider validating the entry point value as well.
        # See:
        # https://packaging.python.org/en/latest/specifications/entry-points/#data-model

    return True
