"""Lightweight smoke tests for the bocpy public C ABI.

Compile-time and runtime behaviour of the ABI is covered by the
standalone ``templates/c_abi_consumer`` extension, which CI builds and
imports separately. The tests here cover only what does not need a
C compiler:

  * ``get_include`` / ``get_sources`` shape.
  * The wheel allow-list (no internal ``.h`` / ``.c`` leaks).
  * Byte-identity between the MSVC atomic bodies in ``boc_compat.c`` and
    ``bocpy_msvc.c``.
  * Static parameter-signature parity between the prototypes in
    ``bocpy.h`` and the bodies in ``bocpy_msvc.c``.

See :ref:`c-abi` for the full usage contract.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import textwrap

import pytest

import bocpy


EXPECTED_PUBLIC_C_FILES = {"bocpy.h", "xidata.h", "bocpy_msvc.c"}

# Filename extensions a wheel install of bocpy is allowed to ship.
_ALLOWED_SHIPPED_EXTS = {
    ".py",       # source modules
    ".pyc",      # bytecode in __pycache__
    ".pyi",      # type stubs
    ".so",       # Linux/BSD compiled extensions
    ".pyd",      # Windows compiled extensions
    ".dylib",    # macOS dynamic libraries (defensive)
    ".dll",      # Windows dynamic libraries (defensive)
    ".txt",      # bocpy.examples ships menu.txt / cheese.txt
}
# Filenames (full basename, no extension) that are allowed even
# though they don't match _ALLOWED_SHIPPED_EXTS.
_ALLOWED_SHIPPED_NAMES = {"py.typed"}

EXPECTED_ATOMIC_NAMES = {
    "atomic_load",
    "atomic_store",
    "atomic_fetch_add",
    "atomic_compare_exchange_strong",
}


# ---------------------------------------------------------------------------
# get_include / get_sources
# ---------------------------------------------------------------------------


def test_get_include_points_at_headers():
    inc = bocpy.get_include()
    assert os.path.isabs(inc)
    assert os.path.isfile(os.path.join(inc, "bocpy", "bocpy.h"))
    assert os.path.isfile(os.path.join(inc, "bocpy", "xidata.h"))


def test_get_sources_shape():
    sources = bocpy.get_sources()
    if sys.platform == "win32":
        assert len(sources) == 1
        assert sources[0].endswith("bocpy_msvc.c")
        assert os.path.isfile(sources[0])
    else:
        assert sources == []


# ---------------------------------------------------------------------------
# Wheel allow-list (no internal .h / .c leaks)
# ---------------------------------------------------------------------------


def _assert_only_public_artefacts(package_dir: str) -> None:
    """Walk ``package_dir`` and assert every shipped file is allowed.

    ``.c`` / ``.h`` files must appear in :data:`EXPECTED_PUBLIC_C_FILES`.
    Every other file must either match :data:`_ALLOWED_SHIPPED_EXTS`
    by extension or :data:`_ALLOWED_SHIPPED_NAMES` by exact basename.
    Anything else is treated as an internal-implementation leak.
    """
    forbidden_c = set()
    forbidden_other = set()
    for _root, _dirs, files in os.walk(package_dir, followlinks=True):
        for name in files:
            ext = os.path.splitext(name)[1]
            if ext in (".c", ".h"):
                if name not in EXPECTED_PUBLIC_C_FILES:
                    forbidden_c.add(name)
            elif ext in _ALLOWED_SHIPPED_EXTS:
                continue
            elif name in _ALLOWED_SHIPPED_NAMES:
                continue
            else:
                forbidden_other.add(name)
    assert not forbidden_c, (
        f"forbidden internal C/H files shipped: {sorted(forbidden_c)}")
    assert not forbidden_other, (
        f"forbidden internal files shipped (unknown extension): "
        f"{sorted(forbidden_other)}")


@pytest.mark.skipif(
    os.environ.get("BOCPY_TEST_WHEEL") != "1",
    reason="set BOCPY_TEST_WHEEL=1 to run wheel-content checks")
def test_wheel_ships_no_internal_files():
    package_dir = os.path.dirname(bocpy.__file__)
    _assert_only_public_artefacts(package_dir)


def test_wheel_allowlist_assertion_actually_fires(tmp_path):
    fake = tmp_path / "fake_pkg"
    fake.mkdir()
    (fake / "bocpy.h").write_text("/* allowed */\n")
    (fake / "compat.h").write_text("/* forbidden */\n")
    with pytest.raises(AssertionError) as exc_info:
        _assert_only_public_artefacts(str(fake))
    assert "compat.h" in str(exc_info.value)


def test_wheel_allowlist_rejects_unknown_extension(tmp_path):
    """An internal artefact with a non-C/H extension must also be flagged."""
    fake = tmp_path / "fake_pkg"
    fake.mkdir()
    (fake / "bocpy.h").write_text("/* allowed */\n")
    (fake / "secrets.json").write_text("{}\n")
    with pytest.raises(AssertionError) as exc_info:
        _assert_only_public_artefacts(str(fake))
    assert "secrets.json" in str(exc_info.value)


# ---------------------------------------------------------------------------
# MSVC atomic bodies in lockstep (boc_compat.c vs bocpy_msvc.c)
# ---------------------------------------------------------------------------


_MARKER_BEGIN = "/* @atomic-bodies-begin */"
_MARKER_END = "/* @atomic-bodies-end */"


def _extract_marker_region(path: str) -> str:
    text = pathlib.Path(path).read_text()
    begin = text.find(_MARKER_BEGIN)
    end = text.find(_MARKER_END)
    assert begin != -1, f"begin marker missing in {path}"
    assert end != -1, f"end marker missing in {path}"
    assert begin < end, f"markers out of order in {path}"
    return text[begin + len(_MARKER_BEGIN):end]


def test_msvc_bodies_in_lockstep():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    compat_c = repo_root / "src" / "bocpy" / "boc_compat.c"
    msvc_c = (repo_root / "src" / "bocpy" / "include" / "bocpy"
              / "bocpy_msvc.c")
    if not compat_c.is_file() or not msvc_c.is_file():
        pytest.skip(
            "source files not present (running against installed wheel)")
    a = _extract_marker_region(str(compat_c))
    b = _extract_marker_region(str(msvc_c))
    assert a == b, "marker regions differ — atomic bodies have drifted"


# ---------------------------------------------------------------------------
# Static prototype/body parameter-signature parity (bocpy.h vs bocpy_msvc.c)
# ---------------------------------------------------------------------------


def _extract_atomic_signatures(text: str) -> dict[str, str]:
    """Return the parameter list of every atomic declaration in ``text``.

    The result is a mapping ``{name: parenthesised-param-list}`` with
    one entry per occurrence of an :data:`EXPECTED_ATOMIC_NAMES` name
    followed by a ``(``. The match walks balanced parentheses forward
    from the opening ``(`` so multi-line declarations (e.g. the three-
    line CAS prototype) are captured intact. Whitespace inside the
    captured signature is normalised so single-line and multi-line
    shapes compare equal.
    """
    out: dict[str, str] = {}
    name_re = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in EXPECTED_ATOMIC_NAMES)
        + r")\s*\(")
    for m in name_re.finditer(text):
        name = m.group(1)
        i = m.end() - 1
        depth = 0
        j = i
        while j < len(text):
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        out[name] = " ".join(text[i:j].split())
    return out


def test_msvc_prototypes_match_bodies():
    inc = bocpy.get_include()
    bocpy_h = pathlib.Path(inc, "bocpy", "bocpy.h").read_text()
    msvc_c = pathlib.Path(inc, "bocpy", "bocpy_msvc.c").read_text()
    d_h = _extract_atomic_signatures(bocpy_h)
    d_msvc = _extract_atomic_signatures(msvc_c)
    assert len(d_h) == 4, (
        f"bocpy.h: expected 4 atomic decls, got {sorted(d_h)}")
    assert len(d_msvc) == 4, (
        f"bocpy_msvc.c: expected 4 atomic decls, got {sorted(d_msvc)}")
    assert set(d_h.keys()) == EXPECTED_ATOMIC_NAMES
    assert set(d_msvc.keys()) == EXPECTED_ATOMIC_NAMES
    assert d_h == d_msvc, (
        "prototype/body parameter signatures diverge between bocpy.h and "
        "bocpy_msvc.c")


def test_msvc_prototype_extraction_actually_fires():
    """Lock the extractor against vacuous-pass and multi-line regression."""
    _cas_one_line = (
        "bool atomic_compare_exchange_strong("
        "atomic_int_least64_t *ptr, "
        "atomic_int_least64_t *expected, "
        "int_least64_t desired);")
    single_line_all_four = textwrap.dedent("""\
        int_least64_t atomic_load(atomic_int_least64_t *ptr);
        void atomic_store(atomic_int_least64_t *ptr, int_least64_t value);
        int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value);
    """) + _cas_one_line + "\n"
    d = _extract_atomic_signatures(single_line_all_four)
    assert len(d) == 4
    assert set(d.keys()) == EXPECTED_ATOMIC_NAMES

    multi_line_cas = textwrap.dedent("""\
        int_least64_t atomic_load(atomic_int_least64_t *ptr);
        void atomic_store(atomic_int_least64_t *ptr, int_least64_t value);
        int_least64_t atomic_fetch_add(atomic_int_least64_t *ptr, int_least64_t value);
        bool atomic_compare_exchange_strong(atomic_int_least64_t *ptr,
                                            atomic_int_least64_t *expected,
                                            int_least64_t desired);
    """)
    d2 = _extract_atomic_signatures(multi_line_cas)
    assert len(d2) == 4, f"multi-line CAS not captured: {sorted(d2)}"
    assert set(d2.keys()) == EXPECTED_ATOMIC_NAMES

    no_atomics = "int unrelated(int x) { return x + 1; }\n"
    assert _extract_atomic_signatures(no_atomics) == {}
