"""Generate and inject a PEP 770 SBOM into a bocpy wheel.

This script has two responsibilities:

1. **Generate** a CycloneDX 1.6 JSON document describing the bocpy
   distribution being built. The schema reflects what is actually
   in the wheel: bocpy itself plus zero third-party runtime Python
   dependencies. Auditwheel / delocate / delvewheel may also bundle
   shared system libraries; for the moment we record them as
   freeform properties when discoverable, but do *not* try to map
   each ``.so`` / ``.dylib`` / ``.dll`` back to its source package
   (that requires platform-specific ``ldd`` / ``otool`` parsing).

2. **Inject** the generated SBOM into a built wheel under
   ``<dist>-<version>.dist-info/sboms/bocpy.cdx.json`` per PEP 770
   and regenerate ``RECORD``. The injection step uses only the
   Python standard library so it can run inside any cibuildwheel
   environment without adding an install-time dependency.

The script is intentionally self-contained (stdlib only). Run
``python scripts/build_sbom.py --help`` for the available
subcommands.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as _dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
import uuid
import zipfile

# Bumping this version invalidates the ``tools.components`` entry in
# the SBOM. Keep it in sync with significant changes to the schema or
# the injection algorithm.
SBOM_GENERATOR_VERSION = "0.1.0"
SBOM_FILENAME = "bocpy.cdx.json"
PEP770_SBOM_SUBDIR = "sboms"

# Stable namespace used to derive a deterministic UUIDv5 ``serialNumber``
# from the (name, version, git_commit, wheel_filename) tuple. The
# specific URL string is what makes this namespace stable across builds
# and across machines — do NOT change it without a coordinated
# generator-version bump, since every existing SBOM's serial number
# would change shape.
_BOCPY_SBOM_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/microsoft/bocpy/sboms"
)


def _sbom_timestamp() -> str:
    """Return the SBOM ``metadata.timestamp`` as an ISO 8601 UTC string.

    Honours the freedesktop reproducible-build convention: if
    ``SOURCE_DATE_EPOCH`` is set in the environment to a parseable
    integer number of seconds since the Unix epoch, that value is used
    verbatim; otherwise we fall back to the current UTC time.

    Setting ``SOURCE_DATE_EPOCH`` (e.g. to the commit timestamp) is the
    cibuildwheel-friendly way to produce byte-identical SBOMs across
    rebuilds of the same source tree, which keeps wheel hashes stable
    and lets downstream consumers cross-check the embedded SBOM
    against the released artefact.
    """
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is not None:
        try:
            epoch = int(raw)
        except ValueError:
            # Match the upstream reproducible-build spec: a malformed
            # value is a hard error rather than a silent fall-through,
            # so that CI surfaces the misconfiguration loudly.
            raise ValueError(
                f"SOURCE_DATE_EPOCH must be an integer, got {raw!r}"
            ) from None
        dt = _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)
    else:
        dt = _dt.datetime.now(_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sbom_serial_number(
    name: str,
    version: str,
    git_commit: str | None,
    wheel_filename: str | None,
) -> str:
    """Derive a deterministic ``urn:uuid:<UUIDv5>`` serial number.

    The serial number is a UUIDv5 (SHA-1 based) computed under
    :data:`_BOCPY_SBOM_NAMESPACE` from the canonical input tuple
    ``name@version+git_commit+wheel_filename``. Missing optional
    fields are encoded as empty strings so the input shape is fixed
    and the resulting UUID is byte-stable.

    Same inputs => same UUID, on every machine and every rebuild.
    """
    payload = (
        f"{name}@{version}+{git_commit or ''}+{wheel_filename or ''}"
    )
    return f"urn:uuid:{uuid.uuid5(_BOCPY_SBOM_NAMESPACE, payload)}"


def _wheel_purl(name: str, version: str) -> str:
    """Build a Package URL for the bocpy wheel itself.

    PyPI purls do not encode the wheel tag; consumers wanting the
    exact wheel filename should use the ``cdx:python:wheel_filename``
    property attached to the root component.
    """
    return f"pkg:pypi/{name}@{version}"


def build_sbom_document(
    name: str,
    version: str,
    description: str,
    license_id: str,
    homepage_url: str,
    vcs_url: str,
    git_commit: str | None,
    wheel_filename: str | None,
) -> dict[str, Any]:
    """Construct the CycloneDX 1.6 JSON document for a bocpy wheel.

    :param name: PEP 503-normalized distribution name (``"bocpy"``).
    :param version: Distribution version (e.g. ``"0.6.0"``).
    :param description: One-line project description.
    :param license_id: SPDX license identifier (e.g. ``"MIT"``).
    :param homepage_url: Project homepage URL.
    :param vcs_url: VCS / repository URL.
    :param git_commit: Optional git commit SHA the wheel was built
        from. Stored as a property on the root component when set.
    :param wheel_filename: Optional wheel filename (basename) the
        SBOM is being embedded in. Stored as a property on the root
        component when set.
    :return: A CycloneDX 1.6 document as a ``dict`` ready to be
        serialised with ``json.dumps(..., indent=2, sort_keys=True)``.
    """
    bom_ref = _wheel_purl(name, version)

    root_component: dict[str, Any] = {
        "bom-ref": bom_ref,
        "type": "library",
        "name": name,
        "version": version,
        "purl": bom_ref,
        "description": description,
        "licenses": [{"license": {"id": license_id}}],
        "supplier": {
            "name": "Microsoft",
            "url": [homepage_url],
        },
        "externalReferences": [
            {"type": "website", "url": homepage_url},
            {"type": "vcs", "url": vcs_url},
        ],
    }

    properties: list[dict[str, str]] = []
    if git_commit:
        properties.append(
            {"name": "cdx:python:git_commit", "value": git_commit}
        )
    if wheel_filename:
        properties.append(
            {"name": "cdx:python:wheel_filename", "value": wheel_filename}
        )
    if properties:
        root_component["properties"] = properties

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": _sbom_serial_number(
            name, version, git_commit, wheel_filename
        ),
        "version": 1,
        "metadata": {
            "timestamp": _sbom_timestamp(),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "build_sbom.py",
                        "version": SBOM_GENERATOR_VERSION,
                        "vendor": "Microsoft",
                    }
                ]
            },
            "component": root_component,
        },
        # bocpy has zero third-party runtime Python dependencies; the
        # components list is intentionally empty. System shared libraries
        # bundled by auditwheel / delocate / delvewheel are not enumerated
        # here yet (see module docstring).
        "components": [],
        "dependencies": [{"ref": bom_ref, "dependsOn": []}],
    }


def _record_row(arcname: str, data: bytes) -> tuple[str, str, str]:
    """Build a ``RECORD`` row for one entry in the wheel zip.

    :param arcname: Archive name of the entry inside the wheel.
    :param data: Raw bytes of the entry.
    :return: A ``(path, hash_spec, size)`` tuple matching the wheel
        ``RECORD`` CSV format described in PEP 427.
    """
    digest = hashlib.sha256(data).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return arcname, f"sha256={b64}", str(len(data))


def _find_dist_info_dir(wheel: zipfile.ZipFile) -> str:
    """Return the ``<dist>-<version>.dist-info/`` arcname prefix.

    Every wheel contains exactly one ``*.dist-info/`` directory.
    Raises ``ValueError`` if the wheel is malformed.
    """
    prefixes: set[str] = set()
    for name in wheel.namelist():
        head = name.split("/", 1)[0]
        if head.endswith(".dist-info"):
            prefixes.add(head)
    if len(prefixes) != 1:
        raise ValueError(
            f"wheel contains {len(prefixes)} .dist-info directories: {sorted(prefixes)!r}"
        )
    return prefixes.pop()


def inject_sbom_into_wheel(
    wheel_path: Path,
    sbom_bytes: bytes,
) -> None:
    """Insert ``sbom_bytes`` into ``wheel_path`` and rewrite ``RECORD``.

    The wheel is replaced atomically: a new ``.whl`` is built in a
    sibling temporary file and renamed over the original only after
    being fully flushed.

    :param wheel_path: Path to the existing wheel to mutate.
    :param sbom_bytes: Serialised CycloneDX JSON document.
    :raises FileNotFoundError: If ``wheel_path`` does not exist.
    :raises ValueError: If the wheel is missing or has multiple
        ``.dist-info`` directories.
    """
    if not wheel_path.is_file():
        raise FileNotFoundError(wheel_path)

    # We materialise the new wheel in a temporary file alongside the
    # original so that the final ``shutil.move`` is an atomic rename
    # on the same filesystem.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=wheel_path.stem + ".",
        suffix=".whl.tmp",
        dir=wheel_path.parent,
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        with zipfile.ZipFile(wheel_path, "r") as src:
            dist_info = _find_dist_info_dir(src)
            sbom_arcname = f"{dist_info}/{PEP770_SBOM_SUBDIR}/{SBOM_FILENAME}"
            record_arcname = f"{dist_info}/RECORD"

            # Collect every entry except the existing RECORD; we
            # rewrite it last with the new hashes.
            #
            # We carry the SOURCE ``ZipInfo`` (not just the filename)
            # for every entry we copy through, because wheels that
            # have been through ``auditwheel`` / ``delocate`` /
            # ``delvewheel`` rely on per-entry ZIP metadata that
            # ``ZipFile.writestr(arcname, data)`` would silently
            # drop:
            #
            #   * ``external_attr``  — the Unix mode bits in the
            #     upper 16 bits encode ``S_IFLNK`` for symlinked
            #     SONAMEs (``libfoo.so.1 -> libfoo.so.1.2.3``).
            #     Drop them and the install step writes a regular
            #     file whose contents are the symlink's text.
            #   * ``create_system`` — tells the reader how to
            #     interpret ``external_attr`` (Unix vs DOS vs ...).
            #   * ``compress_type`` — preserves a deliberate
            #     ``ZIP_STORED`` choice (some wheel builders leave
            #     pre-compressed ``.so`` payloads uncompressed; a
            #     naive ``writestr(arcname, data)`` would re-DEFLATE
            #     them under the destination ZIP's default
            #     compression).
            #   * ``date_time`` — reproducible-build timestamps set
            #     by the upstream wheel builder.
            #   * ``extra`` / ``internal_attr`` / ``comment`` —
            #     uncommon but harmless to preserve; some toolchains
            #     stash Unix UID/GID/mtime extras here.
            #
            # We deliberately do NOT copy ``CRC``, ``compress_size``,
            # ``file_size``, ``header_offset``, or ``flag_bits``:
            # those are stream-position metadata that ``writestr``
            # recomputes when the entry is re-emitted. Copying them
            # would either be silently overridden or, in the case of
            # ``flag_bits``, leak the source archive's
            # data-descriptor / streaming bits into a non-streaming
            # write path.
            entries: list[tuple[zipfile.ZipInfo, bytes]] = []
            sbom_already_present = False
            for info in src.infolist():
                if info.filename == record_arcname:
                    continue
                if info.filename == sbom_arcname:
                    # Tolerate an already-injected SBOM by replacing it,
                    # but log a line to stderr so re-injection in CI is
                    # observable. The reinjection path is exercised when
                    # ``build_sbom.py inject`` is re-run against an
                    # already-decorated wheel (idempotency check), so
                    # silencing it would hide a misconfigured repair
                    # command running the injector twice.
                    sbom_already_present = True
                    continue
                with src.open(info) as f:
                    data = f.read()
                new_info = zipfile.ZipInfo(
                    filename=info.filename, date_time=info.date_time
                )
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                new_info.create_system = info.create_system
                new_info.internal_attr = info.internal_attr
                new_info.extra = info.extra
                new_info.comment = info.comment
                entries.append((new_info, data))

            # The injected SBOM and the rewritten RECORD are NEW entries
            # that this script owns, so they use freshly-constructed
            # ``ZipInfo`` objects with the stdlib defaults
            # (``-rw-------`` perms, ``ZIP_DEFLATED`` compression).
            sbom_info = zipfile.ZipInfo(filename=sbom_arcname)
            sbom_info.compress_type = zipfile.ZIP_DEFLATED
            entries.append((sbom_info, sbom_bytes))

            # Build the new RECORD: every entry gets a hash row except
            # RECORD itself (which has empty hash + empty size).
            record_buf = io.StringIO()
            writer = csv.writer(
                record_buf, delimiter=",", quoting=csv.QUOTE_MINIMAL, lineterminator="\n"
            )
            for entry_info, data in entries:
                writer.writerow(_record_row(entry_info.filename, data))
            writer.writerow((record_arcname, "", ""))
            record_info = zipfile.ZipInfo(filename=record_arcname)
            record_info.compress_type = zipfile.ZIP_DEFLATED

            with zipfile.ZipFile(
                tmp_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as dst:
                for entry_info, data in entries:
                    dst.writestr(entry_info, data)
                dst.writestr(record_info, record_buf.getvalue())

        if sbom_already_present:
            print(
                f"build_sbom.py: wheel {wheel_path.name!r} already contained "
                f"{sbom_arcname!r}; replacing with freshly generated SBOM",
                file=sys.stderr,
            )

        # Atomic rename — the wheel either has the SBOM and a fresh
        # RECORD, or it is byte-identical to before.
        shutil.move(str(tmp_path), str(wheel_path))
    except BaseException:
        # Clean the side-file on any failure so we don't leak a
        # corrupted ``*.whl.tmp`` into the dest directory.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _read_pyproject_metadata(repo_root: Path) -> dict[str, str]:
    """Extract bocpy's distribution metadata from ``pyproject.toml``.

    Only the fields the SBOM needs are returned. Uses the stdlib
    ``tomllib`` (Python 3.11+); on 3.10 a small fallback parser is
    used since the SBOM script may run inside a cibuildwheel
    cp310 image.
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - 3.10 only
        import tomli as tomllib  # type: ignore[import-not-found]

    with (repo_root / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    project = data["project"]
    urls = project.get("urls", {})
    return {
        "name": project["name"],
        "version": project["version"],
        "description": project["description"],
        "license": project["license"],
        "homepage": urls.get("homepage", ""),
        "vcs": urls.get("source", ""),
    }


def _cmd_generate(args: argparse.Namespace) -> int:
    """Implement the ``generate`` subcommand."""
    # Reproducibility guard. The serialNumber is a UUIDv5 derived
    # from ``name@version+git_commit+wheel_filename``. If a caller
    # invokes ``generate`` standalone with neither ``--git-commit``
    # (defaulted to $GITHUB_SHA, often unset locally) nor
    # ``--wheel-filename``, every wheel of the same name+version
    # collapses to the same UUID — defeating the per-wheel-identifier
    # purpose of deterministic serials. ``inject`` always passes
    # ``--wheel-filename`` so it is unaffected.
    if not args.git_commit and not args.wheel_filename:
        print(
            "error: build_sbom.py generate requires at least one of: "
            "--git-commit (or $GITHUB_SHA), --wheel-filename so the "
            "deterministic serialNumber distinguishes this build from "
            "other wheels of the same name+version.",
            file=sys.stderr,
        )
        return 2
    meta = _read_pyproject_metadata(Path(args.project_root))
    doc = build_sbom_document(
        name=meta["name"],
        version=meta["version"],
        description=meta["description"],
        license_id=meta["license"],
        homepage_url=meta["homepage"],
        vcs_url=meta["vcs"],
        git_commit=args.git_commit,
        wheel_filename=args.wheel_filename,
    )
    serialised = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(serialised)
    else:
        Path(args.output).write_text(serialised, encoding="utf-8")
    return 0


def _cmd_inject(args: argparse.Namespace) -> int:
    """Implement the ``inject`` subcommand."""
    target = Path(args.target)
    if target.is_dir():
        wheels = sorted(target.glob("*.whl"))
        if len(wheels) != 1:
            print(
                f"error: expected exactly one .whl in {target}, found {len(wheels)}",
                file=sys.stderr,
            )
            return 1
        wheel_path = wheels[0]
    else:
        wheel_path = target

    # When --copy-to is given, work on a copy in the destination directory
    # and leave the original untouched. This is the pattern used on Windows
    # cibuildwheel jobs where there is no native repair tool, so the
    # script is responsible for both placing the wheel in ``{dest_dir}``
    # and injecting the SBOM into it.
    if args.copy_to is not None:
        copy_target_dir = Path(args.copy_to)
        copy_target_dir.mkdir(parents=True, exist_ok=True)
        copied = copy_target_dir / wheel_path.name
        shutil.copyfile(wheel_path, copied)
        wheel_path = copied

    meta = _read_pyproject_metadata(Path(args.project_root))
    doc = build_sbom_document(
        name=meta["name"],
        version=meta["version"],
        description=meta["description"],
        license_id=meta["license"],
        homepage_url=meta["homepage"],
        vcs_url=meta["vcs"],
        git_commit=args.git_commit,
        wheel_filename=wheel_path.name,
    )
    sbom_bytes = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    inject_sbom_into_wheel(wheel_path, sbom_bytes)
    print(f"injected SBOM into {wheel_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Top-level CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="build_sbom.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path to the bocpy repository root (default: parent of this script).",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    gen = subparsers.add_parser(
        "generate", help="Emit a CycloneDX 1.6 JSON SBOM for the wheel."
    )
    gen.add_argument(
        "--output",
        "-o",
        default="-",
        help="Output path, or '-' for stdout (default: stdout).",
    )
    gen.add_argument(
        "--git-commit",
        default=os.environ.get("GITHUB_SHA"),
        help="Commit SHA to embed (default: $GITHUB_SHA).",
    )
    gen.add_argument(
        "--wheel-filename",
        default=None,
        help="Filename of the wheel the SBOM will be embedded in.",
    )
    gen.set_defaults(func=_cmd_generate)

    inj = subparsers.add_parser(
        "inject",
        help="Generate an SBOM and inject it into an existing wheel.",
    )
    inj.add_argument(
        "target",
        help="Wheel file or directory containing exactly one wheel.",
    )
    inj.add_argument(
        "--copy-to",
        default=None,
        help=(
            "If given, copy the wheel into this directory before injecting; "
            "the original is left unchanged. Used on Windows cibuildwheel "
            "jobs where there is no native repair tool."
        ),
    )
    inj.add_argument(
        "--git-commit",
        default=os.environ.get("GITHUB_SHA"),
        help="Commit SHA to embed (default: $GITHUB_SHA).",
    )
    inj.set_defaults(func=_cmd_inject)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
