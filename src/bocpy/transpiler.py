"""AST transformer that reduces __main__ to the bindings a worker needs."""

import ast
import os
import sys
import types
from typing import NamedTuple, Set

# REPL-injected noise no behavior references; denied from live bindings
# because pre-3.12 ``import readline`` (libedit/macOS) hangs a worker.
_LIVE_BINDING_DENY = frozenset({"readline", "rlcompleter"})


def _is_dotted_identifier(name: str) -> bool:
    """Return True iff ``name`` is a non-empty dotted Python identifier."""
    parts = name.split(".")
    return all(parts) and all(part.isidentifier() for part in parts)


def _is_when_call(node: ast.AST,
                  when_aliases: Set[str],
                  bocpy_module_aliases: Set[str]) -> bool:
    """Return True iff ``node`` is a ``@when(...)`` decorator call.

    Matches three spellings:
    - bare ``Name`` whose id is in ``when_aliases``
      (``from bocpy import when [as alias]``)
    - ``Attribute`` on a ``Name`` whose id is in
      ``bocpy_module_aliases`` and whose ``attr`` is ``"when"``
      (``import bocpy [as alias]`` then ``@alias.when(...)``)
    The literal name ``"when"`` is always treated as an alias when a
    ``from bocpy import when`` statement is present.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in when_aliases
    if isinstance(func, ast.Attribute):
        return (isinstance(func.value, ast.Name)
                and func.value.id in bocpy_module_aliases
                and func.attr == "when")
    return False


def _has_when_decorator(node: ast.FunctionDef,
                        when_aliases: Set[str],
                        bocpy_module_aliases: Set[str]) -> bool:
    """Return True if the function carries an ``@when(...)`` decorator."""
    for dec in node.decorator_list:
        if _is_when_call(dec, when_aliases, bocpy_module_aliases):
            return True
    return False


class MainModuleBinder(ast.NodeTransformer):
    """Prepares a main module for transpiling.

    This transformer collects the names of classes, functions, imports,
    and filters out everything else at the root level.
    """

    def __init__(self):
        """Track discovered classes, functions, and imports."""
        self.classes = set()
        self.functions = set()
        self.imports = set()
        self.when_aliases: set = {"when"}
        self.bocpy_module_aliases: set = set()

    def visit_Import(self, node: ast.Import):  # noqa: N802
        """Record imported names and keep the node."""
        for name in node.names:
            self.imports.add(name.asname if name.asname else name.name)
            if name.name == "bocpy":
                self.bocpy_module_aliases.add(name.asname or name.name)

        return node

    def visit_ImportFrom(self, node: ast.ImportFrom):  # noqa: N802
        """Record imported names and the ``when`` decorator aliases."""
        for name in node.names:
            self.imports.add(name.asname if name.asname else name.name)

        if node.module == "bocpy":
            for n in node.names:
                if n.name == "when":
                    self.when_aliases.add(n.asname or n.name)

        ast.fix_missing_locations(node)

        return node

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: N802
        """Record class definitions and retain the node."""
        self.classes.add(node.name)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        """Record non-when functions; drop module-level @when defs.

        A module-level @when def is scheduled at runtime by the
        decorator itself. Keeping it in the worker bindings module would
        re-run the decorator at import time and re-schedule the behavior,
        so it is filtered out of the bindings module here.
        """
        if _has_when_decorator(node, self.when_aliases,
                               self.bocpy_module_aliases):
            return None

        self.functions.add(node.name)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Assign(self, node: ast.Assign):  # noqa: N802
        """Keep module-level assignments so behaviors can resolve their globals.

        Sub-interpreters cannot share a Python object, so a kept
        module-level binding is **re-evaluated once per worker** at
        bindings-import time. Two consequences a maintainer must keep in
        mind:

        - The RHS runs in every worker, so any side effect (opening a
          connection, spawning a thread, mutating external state) happens
          *N* times, not once.
        - The bound object is a distinct per-interpreter instance, so a
          mutable module-level singleton is **not** shared across
          behaviors. Treat kept globals as per-worker immutable
          initialisers; cross-behavior shared mutable state belongs in a
          :class:`~bocpy.Cown` or the noticeboard, never a module global.

        Both UPPERCASE constants and lowercase singletons are kept
        regardless of case; only the bound name (not its spelling)
        matters for resolution. Multi-target chained assignments and
        non-``Name`` targets are still dropped.
        """
        if isinstance(node.value, ast.Constant):
            return node

        if len(node.targets) > 1:
            return None

        name = node.targets[0]

        if isinstance(name, ast.Name):
            return node

        if isinstance(name, (ast.Tuple, ast.List)) and all(
                isinstance(e, ast.Name) for e in name.elts):
            return node

        return None

    def visit_AnnAssign(self, node: ast.AnnAssign):  # noqa: N802
        """Keep annotated module-level assignments regardless of case."""
        if isinstance(node.target, ast.Name):
            return node
        return None

    def generic_visit(self, node):
        """Ignore unknown top-level nodes."""
        return None

    def visit_Module(self, node: ast.Module):  # noqa: N802
        """Filter the module body to only relevant constructs."""
        new_body = []
        for old_value in node.body:
            new_value = self.visit(old_value)
            if new_value is None:
                continue

            new_body.append(new_value)

        node.body[:] = new_body


MainBindings = NamedTuple("MainBindings", [("code", str), ("classes", Set[str]),
                                           ("functions", Set[str])])


def bind_module(tree: ast.Module) -> MainBindings:
    """Reduce a module to the bindings a worker needs (no behavior extraction).

    The ``@when`` decorator schedules behaviors at runtime via the
    marshalled-code registry, so the worker bindings module must **not**
    contain ``@when`` defs or behavior thunks. This pass keeps only the
    module-level imports, classes, functions, and constants a worker
    needs in order to resolve a behavior's globals.

    :param tree: The source tree
    :type tree: ast.Module
    :return: A bindings result with the reduced module code and
        class/function metadata.
    :rtype: MainBindings
    """
    binder = MainModuleBinder()
    binder.visit(tree)

    ast.fix_missing_locations(tree)

    code = ast.unparse(tree)

    return MainBindings(code, binder.classes, binder.functions)


def bind_file(path: str) -> MainBindings:
    """Parse a Python file and export it with behavior metadata."""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    try:
        tree: ast.Module = ast.parse(source, filename=path)
    except SyntaxError as e:
        raise SyntaxError(f"Error parsing {path}: {e}")

    return bind_module(tree)


def bind_live_module(module: types.ModuleType) -> MainBindings:
    """Reduce a sourceless module to bindings from its live namespace.

    Used for a module with no source file on disk (the REPL, ``python
    -c``, or an ``exec``-ed ``__main__``), where there is nothing to
    parse. An interactively-defined behavior is validated to reference
    only builtins, imported modules, and explicit captures, so the
    bindings a worker needs are exactly the modules currently imported
    in the namespace -- synthesize an ``import`` statement for each.

    Modules in :data:`_LIVE_BINDING_DENY` are dropped (REPL noise that
    can hang a worker); every other import is wrapped in ``try/except
    ImportError`` so a module that refuses to load on a sub-interpreter
    is skipped rather than aborting the whole bindings load -- it would
    surface later as a ``NameError`` on the worker instead.

    The synthesized import statement is compiled and executed on the
    worker, so both the namespace key and the module's ``__name__`` must
    be dotted Python identifiers. An entry whose key or ``__name__`` is
    not (a module object bound under a non-identifier key, or one whose
    ``__name__`` is missing, non-``str``, or not import-syntax-safe) is
    skipped: it cannot be named in a behavior anyway, and emitting it
    raw would inject malformed source that fails the worker ``compile``.

    :param module: The live module whose namespace to reduce.
    :type module: types.ModuleType
    :return: A bindings result whose code re-imports the namespace's
        modules defensively; ``classes`` and ``functions`` are empty.
    :rtype: MainBindings
    """
    blocks = []
    for name, value in vars(module).items():
        if name.startswith("__") or not isinstance(value, types.ModuleType):
            continue
        if not name.isidentifier():
            continue
        real = getattr(value, "__name__", None)
        if not isinstance(real, str) or not _is_dotted_identifier(real):
            continue
        if real in _LIVE_BINDING_DENY:
            continue
        stmt = f"import {real}" if name == real else f"import {real} as {name}"
        blocks.append(f"try:\n    {stmt}\nexcept ImportError:\n    pass")
    code = "\n".join(blocks) + ("\n" if blocks else "")
    return MainBindings(code, set(), set())


def bind_main(module_name: str = "__main__") -> MainBindings:
    """Export the live module named ``module_name`` (default ``__main__``).

    Falls back to a live-namespace reduction when the module has no
    source file on disk (the REPL, ``python -c``, or an ``exec``-ed
    module): a missing ``__file__`` or one that is not a real file
    (e.g. ``<stdin>``) is treated as sourceless. ``module_name`` selects
    *which* loaded module to reduce so a sourceless first ``@when``
    scheduled from a non-``__main__`` module binds that module's
    namespace, not ``__main__``'s. A ``module_name`` not present in
    ``sys.modules`` falls back to ``__main__``.
    """
    mod = sys.modules.get(module_name) or sys.modules["__main__"]
    path = getattr(mod, "__file__", None)
    if path is None or not os.path.isfile(path):
        return bind_live_module(mod)
    return bind_file(path)
