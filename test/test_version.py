"""Tests for ``bocpy.__version__``."""

from pathlib import Path
import re

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

import bocpy


def test_version_attribute_is_a_nonempty_string():
    assert isinstance(bocpy.__version__, str)
    assert bocpy.__version__


def test_version_matches_pyproject_toml():
    """Importing bocpy must report the same version as ``pyproject.toml``.

    The runtime value is resolved through :func:`importlib.metadata.version`,
    which reads the installed distribution's metadata. As long as bocpy is
    installed (editable or wheel) into the active environment, the value
    must match the ``[project].version`` field in ``pyproject.toml``.

    Falling back to ``"0.0.0+unknown"`` only happens when bocpy is imported
    from a source checkout that has never been installed; in that case
    every other test in the suite would also fail to find the C extensions,
    so the strict assertion below is safe.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert bocpy.__version__ == declared, (
        f"bocpy.__version__ = {bocpy.__version__!r} but pyproject.toml says {declared!r}"
    )


def test_version_is_pep440_shaped():
    """A loose PEP 440 sanity check (not a full grammar match)."""
    assert re.match(r"^\d+(\.\d+)*([a-zA-Z0-9.+-]*)$", bocpy.__version__), \
        f"{bocpy.__version__!r} does not look PEP 440-shaped"


def test_version_in_dunder_all():
    assert "__version__" in bocpy.__all__


def test_version_fallback_emits_warning(tmp_path):
    """When ``_metadata.version`` raises, the fallback path logs a WARNING."""
    import subprocess
    import sys
    import textwrap

    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent("""
        import io
        import logging

        # Install a capturing handler BEFORE importing bocpy so the
        # warning emitted during ``import bocpy`` is recorded.
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        logging.getLogger("bocpy").addHandler(handler)
        logging.getLogger("bocpy").setLevel(logging.WARNING)

        # Force the metadata lookup to raise an unexpected exception.
        # ``bocpy.__init__`` calls ``_metadata.version("bocpy")`` where
        # ``_metadata`` is the same module object, so patching here
        # affects the upcoming import.
        import importlib.metadata as md
        def _explode(name):
            raise RuntimeError("simulated metadata corruption")
        md.version = _explode

        import bocpy

        captured = buf.getvalue()
        print("VERSION=" + bocpy.__version__)
        print("LOG_START")
        print(captured)
        print("LOG_END")
    """))

    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "VERSION=0.0.0+unknown" in result.stdout, (
        f"expected fallback version string in subprocess output; got:\n{result.stdout}"
    )
    assert "bocpy package metadata unavailable" in result.stdout
    assert "RuntimeError" in result.stdout
    assert "0.0.0+unknown" in result.stdout
