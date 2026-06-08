"""AST transformers that export when-decorated functions as behaviors."""

import ast
import copy
import os
import sys
from typing import Mapping, NamedTuple, Set


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


class CapturedVariableFinder(ast.NodeVisitor):
    """Finds captured variables in a FunctionDef."""

    def __init__(self, known_vars: Set[str],
                 when_aliases: Set[str] = frozenset({"when"}),
                 bocpy_module_aliases: Set[str] = frozenset()):
        """Initialize the captured variable finder.

        :param known_vars: Any known identifiers (imports, global functions/classes)
        :type known_vars: Set[str]
        :param when_aliases: Names that bind to ``bocpy.when`` (defaults
            to the bare name ``"when"``).
        :type when_aliases: Set[str]
        :param bocpy_module_aliases: Names that bind to the ``bocpy``
            module so ``alias.when(...)`` is recognised.
        :type bocpy_module_aliases: Set[str]
        """
        self.local_vars: Set[str] = set()
        self.used_vars: Set[str] = set()
        self.captured_vars: Set[str] = set()
        self.known_vars: Set[str] = known_vars
        self.when_aliases: Set[str] = when_aliases
        self.bocpy_module_aliases: Set[str] = bocpy_module_aliases

    def clear(self):
        """Reset the tracked state between function visits."""
        self.local_vars.clear()
        self.used_vars.clear()
        self.captured_vars.clear()

    def visit_FunctionDef(self, node):  # noqa: N802
        """Collect locals and recurse to find captured variables."""
        for arg in node.args.args:
            self.local_vars.add(arg.arg)

        if node.args.vararg:
            self.local_vars.add(node.args.vararg.arg)

        if node.args.kwarg:
            self.local_vars.add(node.args.kwarg.arg)

        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.local_vars.add(stmt.name)
                # A nested @when is rewritten by WhenTransformer into a
                # whencall(...) at this position. The cown arguments and the
                # capture tuple are evaluated in *this* (outer) frame, so any
                # free names they reference must appear in the outer
                # behavior's captures. Plain nested def's keep their normal
                # opaque treatment because Python's own closure handles them.
                if _has_when_decorator(stmt, self.when_aliases,
                                       self.bocpy_module_aliases):
                    inner = CapturedVariableFinder(
                        self.known_vars,
                        when_aliases=self.when_aliases,
                        bocpy_module_aliases=self.bocpy_module_aliases,
                    )
                    inner.visit(stmt)
                    self.used_vars |= inner.captured_vars
                    for dec in stmt.decorator_list:
                        if _is_when_call(dec, self.when_aliases,
                                         self.bocpy_module_aliases):
                            for arg in dec.args:
                                self.visit(arg)
                continue

            self.generic_visit(stmt)

        self.captured_vars = self.used_vars - self.local_vars - self.known_vars

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Name(self, node: ast.Name):  # noqa: N802
        """Track variable usage to determine captures."""
        if isinstance(node.ctx, ast.Load):
            self.used_vars.add(node.id)
        elif isinstance(node.ctx, ast.Store):
            self.local_vars.add(node.id)

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):  # noqa: N802
        """Treat ``except ... as X`` binding as a local, not a capture."""
        # ``except ... as X`` (and ``try ... except* ... as X``) bind X
        # on ``ExceptHandler.name`` as a plain identifier, not an
        # ``ast.Name(Store)`` node, so the Name visitor never sees it.
        if node.name:
            self.local_vars.add(node.name)
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
        self.constants = set()
        # Names that bind to ``bocpy.when`` (populated by
        # ``visit_ImportFrom``). Always starts with the bare name
        # ``"when"`` so a synthetic test or partial source still
        # matches the historical literal-name spelling; the import
        # visitor adds any explicit ``as`` alias to the set.
        self.when_aliases: set = {"when"}
        # Names that bind to the ``bocpy`` module (populated by
        # ``visit_Import``). Used so ``@alias.when(...)`` is
        # recognised as a behavior decorator.
        self.bocpy_module_aliases: set = set()

    def known_vars(self):
        """Return identifiers known at module scope for capture exclusion."""
        return self.classes | self.functions | self.imports

    def module_scope_names(self):
        """Return all names available at module scope in the exported module.

        This is a superset of ``known_vars`` that also includes
        UPPERCASE constants and literal assignments kept by
        ``visit_Assign``. It is used for decorator name-resolution
        validation only — NOT for capture exclusion.
        """
        return self.classes | self.functions | self.imports | self.constants

    def visit_Import(self, node: ast.Import):  # noqa: N802
        """Record imported names and keep the node."""
        for name in node.names:
            self.imports.add(name.asname if name.asname else name.name)
            if name.name == "bocpy":
                self.bocpy_module_aliases.add(name.asname or name.name)

        return node

    def visit_ImportFrom(self, node: ast.ImportFrom):  # noqa: N802
        """Record imported names and ensure whencall is available."""
        for name in node.names:
            self.imports.add(name.asname if name.asname else name.name)

        if node.module == "bocpy":
            for n in node.names:
                if n.name == "when":
                    self.when_aliases.add(n.asname or n.name)
            if not any((a.asname or a.name) == "whencall" for a in node.names):
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
        if not _has_when_decorator(node, self.when_aliases,
                                   self.bocpy_module_aliases):
            self.functions.add(node.name)

        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def _record_constant_targets(self, targets):
        """Record every ``Name`` (including nested in tuple targets) as a constant."""
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                self.constants.add(tgt.id)
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                for elt in tgt.elts:
                    if isinstance(elt, ast.Name):
                        self.constants.add(elt.id)

    def visit_Assign(self, node: ast.Assign):  # noqa: N802
        """Add module-level constants."""
        if isinstance(node.value, ast.Constant):
            # Constant assignments survive in the export. Record every
            # target name (including chained ``A = B = 1`` and tuple
            # ``A, B = 1, 2``) so the decorator validator can resolve
            # them.
            self._record_constant_targets(node.targets)
            return node

        if len(node.targets) > 1:
            return None

        name = node.targets[0]

        if isinstance(name, ast.Name):
            # use naming convention to allow some non-constant values as well
            if name.id.isupper():
                self.constants.add(name.id)
                return node

        if isinstance(name, (ast.Tuple, ast.List)) and all(
                isinstance(e, ast.Name) and e.id.isupper() for e in name.elts):
            for elt in name.elts:
                self.constants.add(elt.id)
            return node

        return None

    def visit_AnnAssign(self, node: ast.AnnAssign):  # noqa: N802
        """Keep annotated module-level constants and uppercase names."""
        if isinstance(node.target, ast.Name):
            is_constant = isinstance(node.value, ast.Constant)
            is_upper = node.target.id.isupper()
            if is_constant or is_upper:
                self.constants.add(node.target.id)
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

        # If the user only spelled ``import bocpy [as alias]`` we never
        # injected ``whencall`` into a ``from bocpy import`` statement,
        # but the generated ``__behavior__N`` rewrite still emits
        # ``whencall(...)`` as a bare ``Name``. Prepend an explicit
        # import so worker resolution succeeds. No-op when ``whencall``
        # is already imported or when no bocpy import is present (in
        # which case nothing in the exported module would call it).
        if (self.bocpy_module_aliases and "whencall" not in self.imports):
            inject = ast.ImportFrom(
                module="bocpy",
                names=[ast.alias(name="whencall")],
                level=0,
            )
            ast.fix_missing_locations(inject)
            new_body.insert(0, inject)
            self.imports.add("whencall")

        node.body[:] = new_body


BehaviorInfo = NamedTuple("BehaviorInfo", [("name", str), ("captures", list[str])])


class WhenTransformer(ast.NodeTransformer):
    """Transforms functions marked with @when into behaviors.

    Every time a function with the when decorator is encountered, this transformer
    creates a new behavior function with a unique name for that and replaces
    the function with a call to `whencall` for that behavior.
    """

    # Best-effort early warning for stdlib decorators that produce
    # non-callable descriptors at module scope (``staticmethod``,
    # ``classmethod``, ``property``). Applied below ``@when``, these
    # would silently break worker dispatch — the generated
    # ``__behavior__N`` is invoked as a plain function on the worker,
    # but the descriptor is not callable that way; ``property`` even
    # raises ``TypeError`` at import time.
    #
    # This is **not** a correctness guarantee. The transpiler can only
    # see decorator *syntax*, not what the expression evaluates to at
    # import time on the worker, so any third-party decorator with the
    # same shape (e.g., ``functools.cached_property``, custom
    # descriptor factories) will slip through. Treat the set below as a
    # convenience: a precise, actionable error for the few stdlib names
    # we can recognise from the AST. Users applying exotic decorators
    # below ``@when`` are on their own.
    _BANNED_BELOW_DECORATORS = frozenset({"staticmethod", "classmethod", "property"})

    def __init__(self, known_vars: set, path: str, module_scope_names: set,
                 when_aliases: Set[str] = frozenset({"when"}),
                 bocpy_module_aliases: Set[str] = frozenset()):
        """Prepare behavior extraction with known identifiers and file path."""
        self.known_vars = known_vars
        self.module_scope_names = module_scope_names
        self.when_aliases = when_aliases
        self.bocpy_module_aliases = bocpy_module_aliases
        self.cap_finder = CapturedVariableFinder(
            known_vars,
            when_aliases=when_aliases,
            bocpy_module_aliases=bocpy_module_aliases,
        )
        self.nodes = []
        self.behaviors = {}
        self.path = path

    def _validate_decorator_names(self, dec: ast.AST):
        """Reject free names in ``dec`` that the worker cannot resolve.

        Walks the decorator subtree honoring lexical scope: parameters
        of ``Lambda`` and target names of comprehensions / generator
        expressions are *local* to those forms and must not be flagged.
        Free ``Name(Load)`` references must appear in
        ``module_scope_names`` (imports, classes, functions, constants,
        builtins) so they resolve when the exported module is imported
        on a worker.
        """
        bound_stack: list[set] = []

        def is_bound(name: str) -> bool:
            return any(name in s for s in bound_stack)

        def lambda_locals(args: ast.arguments) -> set:
            local = set()
            for grp in (args.posonlyargs, args.args, args.kwonlyargs):
                for a in grp:
                    local.add(a.arg)
            if args.vararg:
                local.add(args.vararg.arg)
            if args.kwarg:
                local.add(args.kwarg.arg)
            return local

        def collect_targets(target: ast.AST, into: set) -> None:
            if isinstance(target, ast.Name):
                into.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for elt in target.elts:
                    collect_targets(elt, into)
            elif isinstance(target, ast.Starred):
                collect_targets(target.value, into)

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.Lambda):
                # Defaults are evaluated in the *outer* scope.
                for d in node.args.defaults:
                    visit(d)
                for d in node.args.kw_defaults:
                    if d is not None:
                        visit(d)
                bound_stack.append(lambda_locals(node.args))
                visit(node.body)
                bound_stack.pop()
                return

            if isinstance(node, (ast.ListComp, ast.SetComp,
                                 ast.GeneratorExp, ast.DictComp)):
                local: set = set()
                for i, gen in enumerate(node.generators):
                    # The *first* iter is evaluated in the enclosing
                    # scope; later iters see prior targets.
                    if i == 0:
                        visit(gen.iter)
                    else:
                        bound_stack.append(local)
                        visit(gen.iter)
                        bound_stack.pop()
                    collect_targets(gen.target, local)
                    bound_stack.append(local)
                    for if_ in gen.ifs:
                        visit(if_)
                    bound_stack.pop()
                bound_stack.append(local)
                if isinstance(node, ast.DictComp):
                    visit(node.key)
                    visit(node.value)
                else:
                    visit(node.elt)
                bound_stack.pop()
                return

            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if not is_bound(node.id) and node.id not in self.module_scope_names:
                    raise SyntaxError(
                        f"Decorator references '{node.id}' which is "
                        f"not defined as an import, class, function, or "
                        f"constant at module level. Ensure it is "
                        f"importable in the worker.",
                        (self.path, node.lineno, node.col_offset, None),
                    )

            for child in ast.iter_child_nodes(node):
                visit(child)

        visit(dec)

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
            if _is_when_call(dec, self.when_aliases,
                             self.bocpy_module_aliases):
                when_dec = dec
                break

        if when_dec is None:
            return self.generic_visit(node)

        # Reject async functions — there is no event loop on workers.
        if isinstance(node, ast.AsyncFunctionDef):
            raise SyntaxError(
                "@when does not support async functions",
                (self.path, node.lineno, node.col_offset, None),
            )

        # Reject decorators above @when — they would wrap the
        # scheduling call (a Cown), not the behavior body.
        when_idx = node.decorator_list.index(when_dec)
        if when_idx > 0:
            bad = node.decorator_list[0]
            above = [ast.unparse(d) for d in node.decorator_list[:when_idx]]
            raise SyntaxError(
                "Decorators above @when are not supported — move them "
                "below @when to apply them to the behavior body: "
                + ", ".join(above),
                (self.path, bad.lineno, bad.col_offset, None),
            )

        # first create a deep copy of the function
        behavior_node = copy.deepcopy(node)
        ast.copy_location(behavior_node, node)

        # Extras-as-captures: positional parameters declared beyond the
        # cown count are captured by name from the caller's frame. This
        # supports two idioms transparently:
        #   * the canonical Python loop-snapshot ``def b(c, i=i)`` —
        #     defaults align with the *tail* of ``args.args``; the
        #     default name becomes the capture source.
        #   * the rename form ``def b(c, x=y)`` — capture by ``y``,
        #     bind into param ``x``.
        # Undefaulted trailing positionals (``def b(c, factor)``) are
        # captured by the parameter's own name. Non-Name defaults and
        # defaults landing on cown positions are rejected up front so a
        # broken signature surfaces at export time, not as a confusing
        # worker TypeError.
        extras_captures: list[str] = []
        n_cowns = len(when_dec.args)
        all_params = behavior_node.args.args
        defaults = behavior_node.args.defaults

        if len(defaults) > len(all_params) - n_cowns:
            raise SyntaxError(
                "Default arguments on @when behavior cown positions are "
                "not supported — defaults are allowed only on trailing "
                "parameters beyond the @when cown count.",
                (self.path, node.lineno, node.col_offset, None),
            )

        extras = all_params[n_cowns:]
        n_undefaulted = len(extras) - len(defaults)
        for arg in extras[:n_undefaulted]:
            extras_captures.append(arg.arg)
        for arg, dflt in zip(extras[n_undefaulted:], defaults):
            if not isinstance(dflt, ast.Name):
                raise SyntaxError(
                    f"Default for @when behavior parameter '{arg.arg}' "
                    f"must be a plain name (e.g. ``{arg.arg}={arg.arg}``). "
                    f"Compute the value before the @when call and "
                    f"capture the resulting name.",
                    (self.path, dflt.lineno, dflt.col_offset, None),
                )
            extras_captures.append(dflt.id)

        # Strip defaults so the worker never tries to evaluate them.
        # The captured values are passed positionally by ``whencall``.
        behavior_node.args.defaults = []

        # find all the captured variables. These will need to be passed
        # to the behavior as additional arguments, as the closure will
        # no longer function properly. Extras (already in args.args) are
        # in ``local_vars`` thanks to the finder's param walk, so they
        # will not be re-classified as body free-vars.
        self.cap_finder.clear()
        self.cap_finder.visit(behavior_node)
        # __file__ is rewritten to a string constant by visit_Name below,
        # so it must not be added to the parameter list as a capture.
        body_captures = [c for c in self.cap_finder.captured_vars
                         if c != "__file__"]

        # add the body captures as trailing parameters; extras are
        # already part of the user's signature.
        for name in body_captures:
            behavior_node.args.args.append(ast.Name(id=name))

        captures = extras_captures + body_captures

        # Remove only @when decorators; other decorators compose with
        # the behavior body and are preserved in the exported module.
        behavior_node.decorator_list = [
            d for d in behavior_node.decorator_list
            if not _is_when_call(d, self.when_aliases,
                                 self.bocpy_module_aliases)
        ]

        # Reject descriptor-producing decorators that would silently
        # break worker dispatch when applied to a module-level
        # ``__behavior__N`` (the worker calls it as a plain function).
        for dec in behavior_node.decorator_list:
            banned = None
            if isinstance(dec, ast.Name) and dec.id in self._BANNED_BELOW_DECORATORS:
                banned = dec.id
            elif (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                    and dec.func.id in self._BANNED_BELOW_DECORATORS):
                banned = dec.func.id
            if banned is not None:
                raise SyntaxError(
                    f"@{banned} is not supported below @when — the generated "
                    f"behavior runs as a module-level function on the worker, "
                    f"where {banned} produces a non-callable descriptor.",
                    (self.path, dec.lineno, dec.col_offset, None),
                )

        # Validate that remaining decorator expressions only reference
        # names available at module scope in the worker. Walk only
        # *free* variables — names bound by ``Lambda`` /
        # comprehension / generator-expression scopes inside the
        # decorator are local and must not be flagged.
        for dec in behavior_node.decorator_list:
            self._validate_decorator_names(dec)

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
        assign = ast.Assign(
            targets=[ast.Name(id=node.name, ctx=ast.Store())],
            value=when_call,
        )
        ast.copy_location(assign, node)
        ast.fix_missing_locations(assign)
        return assign

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815


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

    when_transformer = WhenTransformer(
        boc_export.known_vars() | builtins,
        path,
        module_scope_names=boc_export.module_scope_names() | builtins,
        when_aliases=boc_export.when_aliases,
        bocpy_module_aliases=boc_export.bocpy_module_aliases,
    )
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
