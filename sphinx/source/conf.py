# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import ast
import inspect
import os
import textwrap

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'bocpy'
copyright = '2026, Microsoft'
author = 'Microsoft'
release = '0.2.1'

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "enum_tools.autoenum",
    "sphinx_autodoc_typehints"
]

napoleon_use_param = True

templates_path = ['_templates']
exclude_patterns = []

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'alabaster'
html_static_path = ['_static']

html_logo = "_static/logo-200.svg"

# ---------------------------------------------------------------------------
# Stub-aware autodoc: pull docstrings and signatures from __init__.pyi
# ---------------------------------------------------------------------------

def _parse_pyi_stub():
    """Parse bocpy/__init__.pyi and return a nested dict of docstrings/signatures.

    Returns a dict like::

        {
            "send": {"doc": "...", "sig": "(tag: str, contents: Any)"},
            "Matrix": {
                "doc": "...", "sig": None,
                "members": {
                    "__init__": {"doc": "...", "sig": "(self, rows, ...)"},
                    "sum":      {"doc": "...", "sig": "(self, axis=None)"},
                    ...
                }
            },
            ...
        }
    """
    import bocpy
    pyi_path = os.path.join(os.path.dirname(bocpy.__file__), "__init__.pyi")
    if not os.path.exists(pyi_path):
        return {}

    with open(pyi_path) as f:
        tree = ast.parse(f.read())

    def _sig_from_args(node):
        """Build a signature string from an ast.FunctionDef."""
        args = node.args
        parts = []
        # positional args (skip 'self'/'cls')
        all_args = args.args
        defaults = args.defaults
        n_defaults = len(defaults)
        n_args = len(all_args)
        for i, arg in enumerate(all_args):
            if arg.arg in ("self", "cls"):
                continue
            ann = ast.unparse(arg.annotation) if arg.annotation else None
            default_idx = i - (n_args - n_defaults)
            if default_idx >= 0:
                default = ast.unparse(defaults[default_idx])
                if ann:
                    parts.append(f"{arg.arg}: {ann} = {default}")
                else:
                    parts.append(f"{arg.arg}={default}")
            else:
                if ann:
                    parts.append(f"{arg.arg}: {ann}")
                else:
                    parts.append(arg.arg)

        # *args
        if args.vararg:
            va = args.vararg
            ann = ast.unparse(va.annotation) if va.annotation else None
            parts.append(f"*{va.arg}: {ann}" if ann else f"*{va.arg}")

        # **kwargs
        if args.kwarg:
            kw = args.kwarg
            ann = ast.unparse(kw.annotation) if kw.annotation else None
            parts.append(f"**{kw.arg}: {ann}" if ann else f"**{kw.arg}")

        ret = ast.unparse(node.returns) if node.returns else None
        sig = "(" + ", ".join(parts) + ")"
        if ret:
            sig += f" -> {ret}"
        return sig

    result = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=True)
            result[node.name] = {
                "doc": doc,
                "sig": _sig_from_args(node),
                "members": {},
            }
        elif isinstance(node, ast.ClassDef):
            cls_doc = ast.get_docstring(node, clean=True)
            members = {}
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_doc = ast.get_docstring(child, clean=True)
                    members[child.name] = {
                        "doc": child_doc,
                        "sig": _sig_from_args(child),
                    }
            result[node.name] = {
                "doc": cls_doc,
                "sig": None,
                "members": members,
            }
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # Module-level annotated variables (e.g. TIMEOUT: str)
            # Check for a string expression following it (docstring-as-next-stmt)
            pass

    return result


_STUB_DATA = _parse_pyi_stub()


def _is_c_extension(obj):
    """Return True if obj is a C extension type or builtin function."""
    if isinstance(obj, type) and not hasattr(obj, "__module__"):
        return True
    if inspect.isbuiltin(obj):
        return True
    mod = getattr(obj, "__module__", None)
    if mod and mod.startswith("bocpy._"):
        return True
    return False


def _stub_docstring_hook(app, what, name, obj, options, lines):
    """Inject docstrings from .pyi when the runtime object has none."""
    # Determine the stub key
    parts = name.split(".")
    if len(parts) >= 2 and parts[0] == "bocpy":
        parts = parts[1:]

    stub_doc = None
    if len(parts) == 1:
        entry = _STUB_DATA.get(parts[0])
        if entry:
            stub_doc = entry.get("doc")
    elif len(parts) == 2:
        cls_entry = _STUB_DATA.get(parts[0])
        if cls_entry:
            member = cls_entry.get("members", {}).get(parts[1])
            if member:
                stub_doc = member.get("doc")

    if stub_doc and (not lines or all(not l.strip() for l in lines)):
        lines.clear()
        lines.extend(stub_doc.splitlines())


def _stub_signature_hook(app, what, name, obj, options, sig, return_annotation):
    """Inject signatures from .pyi for C extension objects."""
    parts = name.split(".")
    if len(parts) >= 2 and parts[0] == "bocpy":
        parts = parts[1:]

    stub_sig = None
    if len(parts) == 1:
        entry = _STUB_DATA.get(parts[0])
        if entry:
            stub_sig = entry.get("sig")
    elif len(parts) == 2:
        cls_entry = _STUB_DATA.get(parts[0])
        if cls_entry:
            member = cls_entry.get("members", {}).get(parts[1])
            if member:
                stub_sig = member.get("sig")

    if stub_sig:
        # Split off return annotation if present
        if " -> " in stub_sig:
            sig_part, ret_part = stub_sig.rsplit(" -> ", 1)
            return (sig_part, ret_part)
        return (stub_sig, None)

    return None


def setup(app):
    """Register autodoc hooks for .pyi stub injection."""
    app.connect("autodoc-process-docstring", _stub_docstring_hook)
    app.connect("autodoc-process-signature", _stub_signature_hook)
