"""AST transformers that export when-decorated functions as behaviors."""

import ast
import copy
import os
import sys
from typing import Mapping, NamedTuple, Set


class CapturedVariableFinder(ast.NodeVisitor):
    """Finds captured variables in a FunctionDef."""

    def __init__(self, known_vars: Set[str]):
        """Initialize the captured variable finder.

        :param known_vars: Any known identifiers (imports, global functions/classes)
        :type known_vars: Set[str]
        """
        self.local_vars: Set[str] = set()
        self.used_vars: Set[str] = set()
        self.captured_vars: Set[str] = set()
        self.known_vars: Set[str] = known_vars

    def clear(self):
        """Reset the tracked state between function visits."""
        self.local_vars.clear()
        self.used_vars.clear()
        self.captured_vars.clear()

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        """Collect locals and recurse to find captured variables."""
        for arg in node.args.args:
            self.local_vars.add(arg.arg)

        if node.args.vararg:
            self.local_vars.add(node.args.vararg.arg)

        if node.args.kwarg:
            self.local_vars.add(node.args.kwarg.arg)

        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef):
                self.local_vars.add(stmt.name)
                continue

            self.generic_visit(stmt)

        self.captured_vars = self.used_vars - self.local_vars - self.known_vars

    def visit_Name(self, node: ast.Name):  # noqa: N802
        """Track variable usage to determine captures."""
        if isinstance(node.ctx, ast.Load):
            self.used_vars.add(node.id)
        elif isinstance(node.ctx, ast.Store):
            self.local_vars.add(node.id)

        self.generic_visit(node)


class BOCModuleTransformer(ast.NodeTransformer):
    """Prepares a main module for transpiling.

    This transformer collects the names of classes, functions, imports,
    and filters out everything else at the root level.
    """

    def __init__(self):
        """Track discovered classes, functions, and imports."""
        self.classes = set()
        self.functions = set()
        self.imports = set()

    def known_vars(self):
        """Return identifiers known at module scope for capture exclusion."""
        return self.classes | self.functions | self.imports

    def visit_Import(self, node: ast.Import):  # noqa: N802
        """Record imported names and keep the node."""
        for name in node.names:
            self.imports.add(name.asname if name.asname else name.name)

        return node

    def visit_ImportFrom(self, node: ast.ImportFrom):  # noqa: N802
        """Record imported names and ensure whencall is available."""
        for name in node.names:
            self.imports.add(name.asname if name.asname else name.name)

        if node.module == "bocpy" and not any((a.asname or a.name) == "whencall" for a in node.names):
            node.names.append(ast.alias(name="whencall"))
            self.imports.add("whencall")

        ast.fix_missing_locations(node)

        return node

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: N802
        """Record class definitions and retain the node."""
        self.classes.add(node.name)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        """Record non-when functions for later capture resolution."""
        when_dec = None
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "when":
                when_dec = dec
                break

        if when_dec is None:
            self.functions.add(node.name)

        return node

    def visit_Assign(self, node: ast.Assign):  # noqa: N802
        """Add module-level constants."""
        if isinstance(node.value, ast.Constant):
            return node

        if len(node.targets) > 1:
            return None

        name = node.targets[0]

        if isinstance(name, ast.Name):
            # use naming convention to allow some non-constant values as well
            if name.id.isupper():
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


BehaviorInfo = NamedTuple("BehaviorInfo", [("name", str), ("captures", list[str])])


class WhenTransformer(ast.NodeTransformer):
    """Transforms functions marked with @when into behaviors.

    Every time a function with the when decorator is encountered, this transformer
    creates a new behavior function with a unique name for that and replaces
    the function with a call to `whencall` for that behavior.
    """

    def __init__(self, known_vars: set, path: str):
        """Prepare behavior extraction with known identifiers and file path."""
        self.known_vars = known_vars
        self.cap_finder = CapturedVariableFinder(known_vars)
        self.nodes = []
        self.behaviors = {}
        self.path = path

    def visit_Module(self, node: ast.Module):  # noqa: N802
        """Remove when-call expressions and append generated behaviors."""
        new_body = []
        for old_value in node.body:
            new_value = self.visit(old_value)
            if isinstance(new_value, ast.Expr):
                continue

            new_body.append(new_value)

        node.body[:] = new_body

    def visit_Name(self, node: ast.Name):  # noqa: N802
        """Rewrite __file__ to refer to the exported path."""
        if node.id == "__file__":
            return ast.Constant(value=os.path.abspath(self.path))

        return node

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        """Transform @when functions into exported behaviors."""
        when_dec: ast.Expr = None
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue

            if isinstance(dec.func, ast.Name) and dec.func.id == "when":
                when_dec = dec
                break

        if when_dec is None:
            return self.generic_visit(node)

        # first create a deep copy of the function
        behavior_node = copy.deepcopy(node)
        ast.copy_location(behavior_node, node)

        # find all the captured variables. These will need to be passed
        # to the behavior as additional arguments, as the closure will
        # no longer function properly.
        self.cap_finder.clear()
        self.cap_finder.visit(behavior_node)
        # __file__ is rewritten to a string constant by visit_Name below,
        # so it must not be added to the parameter list as a capture.
        captures = [c for c in self.cap_finder.captured_vars if c != "__file__"]

        # add the additional arguments to the function
        for name in captures:
            behavior_node.args.args.append(ast.Name(id=name))

        # strip the @when decorator (and any other decorators, they are
        # not supported)
        behavior_node.decorator_list.clear()

        # deal with any recursive behaviors within this behavior
        behavior_node = self.visit(behavior_node)

        # assign a unique name
        behavior_node.name = f"__behavior__{len(self.behaviors)}"

        # add the node to our list of behavior function nodes
        ast.fix_missing_locations(behavior_node)
        self.nodes.append(behavior_node)

        # this allows name and capture lookup for execution of behaviors
        # from the primary interpreter (using source line numbers from the
        # frame)
        self.behaviors[when_dec.lineno] = BehaviorInfo(behavior_node.name, captures)

        args = [ast.Constant(value=behavior_node.name),
                ast.Tuple(when_dec.args),
                ast.Tuple([ast.Name(id=capture)
                           for capture in captures])]

        when_call = ast.Call(func=ast.Name(id="whencall"), args=args, keywords=[])
        ast.copy_location(when_call, node)
        ast.fix_missing_locations(when_call)
        return ast.Expr(ast.Assign([ast.Name(id=node.name)], when_call))


ExportResult = NamedTuple("ExportResult", [("code", str), ("classes", Set[str]),
                                           ("functions", Set[str]),
                                           ("behaviors", Mapping[int, BehaviorInfo])])


# Module-level dunders (__name__, __doc__, __package__, __spec__, __loader__)
# are exposed via __builtins__, but inside a behavior they should refer to the
# *user* module's value, not the worker's exported module. Removing them from
# `known_vars` lets the capture mechanism pick them up from the call-site
# frame's globals at runtime. __file__ is handled separately via inlining in
# WhenTransformer.visit_Name.
MODULE_DUNDERS = {"__name__", "__doc__", "__package__",
                  "__spec__", "__loader__"}


def export_module(tree: ast.Module, path: str = None) -> ExportResult:
    """Extract an AST as a BOC-enlightened module with generated behaviors.

    :param tree: The source tree
    :type tree: ast.Module
    :return: An export result with code and metadata
    :rtype: ExportResult
    """
    builtins = set(globals()["__builtins__"].keys()) - MODULE_DUNDERS

    boc_export = BOCModuleTransformer()
    boc_export.visit(tree)

    when_transformer = WhenTransformer(boc_export.known_vars() | builtins, path)
    when_transformer.visit(tree)

    tree.body.extend(when_transformer.nodes)

    ast.fix_missing_locations(tree)

    code = ast.unparse(tree)

    return ExportResult(code, boc_export.classes, boc_export.functions, when_transformer.behaviors)


def export_module_from_file(path: str) -> ExportResult:
    """Parse a Python file and export it with behavior metadata."""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()

    try:
        tree: ast.Module = ast.parse(source, filename=path)
    except SyntaxError as e:
        raise SyntaxError(f"Error parsing {path}: {e}")

    return export_module(tree, path)


def export_main() -> ExportResult:
    """Export the currently running __main__ module."""
    return export_module_from_file(sys.modules["__main__"].__file__)
