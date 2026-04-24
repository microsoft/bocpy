"""Tests for the transpiler module."""

import ast
import os
import textwrap

from bocpy.transpiler import BOCModuleTransformer, CapturedVariableFinder, export_module


# ── CapturedVariableFinder ──────────────────────────────────────────────


class TestCapturedParams:
    """Function parameters must never appear as captured variables."""

    @staticmethod
    def _captures(source, known_vars=frozenset()):
        tree = ast.parse(textwrap.dedent(source))
        finder = CapturedVariableFinder(set(known_vars))
        finder.visit(tree.body[0])
        return finder.captured_vars

    def test_positional_params_excluded(self):
        assert self._captures("""\
            def f(a, b):
                return a + b
        """) == set()

    def test_vararg_excluded(self):
        assert self._captures("""\
            def f(*args):
                return args
        """) == set()

    def test_kwarg_excluded(self):
        assert self._captures("""\
            def f(**kwargs):
                return kwargs
        """) == set()

    def test_mixed_params_excluded(self):
        assert self._captures("""\
            def f(a, *args, **kwargs):
                return a, args, kwargs
        """) == set()


class TestCapturedLocals:
    """Assignments and nested function names are local, not captured."""

    @staticmethod
    def _captures(source, known_vars=frozenset()):
        tree = ast.parse(textwrap.dedent(source))
        finder = CapturedVariableFinder(set(known_vars))
        finder.visit(tree.body[0])
        return finder.captured_vars

    def test_assignment_target_excluded(self):
        assert self._captures("""\
            def f():
                x = 1
                return x
        """) == set()

    def test_nested_function_name_excluded(self):
        assert self._captures("""\
            def f():
                def helper():
                    pass
                return helper
        """) == set()


class TestCapturedFreeVars:
    """Free variables that are not params, locals, or known are captured."""

    @staticmethod
    def _captures(source, known_vars=frozenset()):
        tree = ast.parse(textwrap.dedent(source))
        finder = CapturedVariableFinder(set(known_vars))
        finder.visit(tree.body[0])
        return finder.captured_vars

    def test_single_capture(self):
        assert self._captures("""\
            def f():
                return outer
        """) == {"outer"}

    def test_multiple_captures(self):
        assert self._captures("""\
            def f(a):
                return a + x + y
        """) == {"x", "y"}

    def test_known_var_not_captured(self):
        assert self._captures("""\
            def f():
                return known
        """, known_vars={"known"}) == set()

    def test_mixed_locals_and_captures(self):
        caps = self._captures("""\
            def f(a):
                x = 1
                def h():
                    pass
                return a + x + h + captured
        """)
        assert caps == {"captured"}


class TestCapturedClear:
    """The clear() method resets state so the finder can be reused."""

    def test_clear_resets_between_visits(self):
        finder = CapturedVariableFinder(set())

        tree1 = ast.parse("def f():\n    return a")
        finder.visit(tree1.body[0])
        assert finder.captured_vars == {"a"}

        finder.clear()

        tree2 = ast.parse("def g():\n    return b")
        finder.visit(tree2.body[0])
        assert finder.captured_vars == {"b"}
        assert "a" not in finder.captured_vars


# ── BOCModuleTransformer ────────────────────────────────────────────────


class TestModuleTransformerImports:
    """Import handling: recording names and whencall injection."""

    @staticmethod
    def _transform(source):
        tree = ast.parse(textwrap.dedent(source))
        t = BOCModuleTransformer()
        t.visit(tree)
        return t, tree

    def test_import_recorded(self):
        t, _ = self._transform("import os")
        assert "os" in t.imports

    def test_from_import_recorded(self):
        t, _ = self._transform("from sys import path")
        assert "path" in t.imports

    def test_whencall_injected_when_missing(self):
        t, tree = self._transform("from bocpy import when, Cown")
        aliases = [a.name for a in tree.body[0].names]
        assert "whencall" in aliases
        assert "whencall" in t.imports

    def test_non_bocpy_import_not_modified(self):
        _, tree = self._transform("from collections import OrderedDict")
        aliases = [a.name for a in tree.body[0].names]
        assert "whencall" not in aliases

    def test_whencall_not_duplicated_when_present(self):
        _, tree = self._transform("from bocpy import when, whencall, Cown")
        aliases = [a.name for a in tree.body[0].names]
        assert aliases.count("whencall") == 1

    def test_whencall_injected_when_aliased(self):
        t, tree = self._transform("from bocpy import whencall as wc, Cown")
        aliases = [(a.name, a.asname) for a in tree.body[0].names]
        # Original aliased import kept, plus bare whencall injected
        assert ("whencall", "wc") in aliases
        assert ("whencall", None) in aliases
        assert "wc" in t.imports
        assert "whencall" in t.imports


class TestModuleTransformerDeclarations:
    """Classes and functions are recorded; @when functions excluded."""

    @staticmethod
    def _transform(source):
        tree = ast.parse(textwrap.dedent(source))
        t = BOCModuleTransformer()
        t.visit(tree)
        return t, tree

    def test_class_recorded(self):
        t, _ = self._transform("""\
            class Foo:
                pass
        """)
        assert "Foo" in t.classes

    def test_non_when_function_recorded(self):
        t, _ = self._transform("""\
            def helper():
                pass
        """)
        assert "helper" in t.functions

    def test_when_function_not_recorded(self):
        t, _ = self._transform("""\
            from bocpy import when, Cown

            @when(x)
            def behavior(x):
                pass
        """)
        assert "behavior" not in t.functions

    def test_known_vars_is_union(self):
        t, _ = self._transform("""\
            import os
            from sys import path

            class Foo:
                pass

            def bar():
                pass
        """)
        assert t.known_vars() == {"os", "path", "Foo", "bar"}


class TestModuleTransformerFiltering:
    """Only imports, classes, functions, and eligible assignments survive."""

    @staticmethod
    def _transform(source):
        tree = ast.parse(textwrap.dedent(source))
        t = BOCModuleTransformer()
        t.visit(tree)
        return t, tree

    def test_constant_assignment_preserved(self):
        _, tree = self._transform("x = 42")
        assert len(tree.body) == 1

    def test_uppercase_non_constant_preserved(self):
        _, tree = self._transform("CONFIG = some_call()")
        code = ast.unparse(tree)
        assert "CONFIG" in code

    def test_lowercase_non_constant_filtered(self):
        _, tree = self._transform("config = some_call()")
        assert len(tree.body) == 0

    def test_multi_target_non_constant_filtered(self):
        _, tree = self._transform("a = b = some_call()")
        assert len(tree.body) == 0

    def test_multi_target_constant_preserved(self):
        _, tree = self._transform("a = b = 42")
        assert len(tree.body) == 1

    def test_for_loop_filtered(self):
        _, tree = self._transform("""\
            for i in range(10):
                pass
        """)
        assert len(tree.body) == 0

    def test_bare_expression_filtered(self):
        _, tree = self._transform('print("hello")')
        assert len(tree.body) == 0


# ── export_module (full pipeline) ───────────────────────────────────────


class TestExportBehaviorNaming:
    """Behaviors are renamed to __behavior__N with sequential numbering."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_single_behavior_named_0(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def first(x):
                return x.value
        """)
        names = [info.name for info in result.behaviors.values()]
        assert names == ["__behavior__0"]
        assert "def __behavior__0(" in result.code

    def test_two_behaviors_sequential(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)
            y = Cown(2)

            @when(x)
            def first(x):
                return x.value

            @when(y)
            def second(y):
                return y.value
        """)
        names = sorted(info.name for info in result.behaviors.values())
        assert names == ["__behavior__0", "__behavior__1"]


class TestExportCaptures:
    """Captured variables are recorded and added as behavior parameters."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_capture_appended_as_arg(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)
            factor = 3

            @when(x)
            def scaled(x):
                return x.value * factor
        """)
        info = list(result.behaviors.values())[0]
        assert "factor" in info.captures
        # factor must appear as a parameter in the generated behavior def
        sig = result.code.split("def __behavior__0(")[1].split("):")[0]
        assert "factor" in sig

    def test_no_captures_when_none_needed(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def identity(x):
                return x.value
        """)
        info = list(result.behaviors.values())[0]
        assert info.captures == []


class TestExportDecoratorStripping:
    """Generated behavior functions must not carry any decorators."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_no_decorator_on_behavior(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def f(x):
                return x.value
        """)
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                assert node.decorator_list == [], (
                    f"{node.name} still has decorators"
                )


class TestExportFileRewrite:
    """__file__ references inside behaviors are rewritten to the source path."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_file_replaced_with_absolute_path(self):
        path = "/some/test/file.py"
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def f(x):
                return __file__
        """, path=path)
        # Walk the generated AST and confirm __file__ has been replaced
        # with the absolute source path as a string constant. Substring
        # matching against the unparsed source is platform-fragile because
        # backslashes in Windows paths get escaped during unparse.
        expected = os.path.abspath(path)
        gen_tree = ast.parse(result.code)
        constants = [
            n.value for n in ast.walk(gen_tree)
            if isinstance(n, ast.Constant) and n.value == expected
        ]
        assert constants, (
            f"expected absolute path {expected!r} as a string constant in "
            f"generated code:\n{result.code}"
        )

    def test_file_capture_does_not_become_parameter(self):
        """__file__ must be inlined, not added to the behavior's args list.

        Regression: the rewriter previously added every captured free
        variable (including __file__) as an extra positional parameter.
        After visit() inlined __file__ to a string Constant the result was
        an invalid signature like ``def __behavior__0(x, '/path'):``,
        which only failed at worker import time.
        """
        path = "/some/test/file.py"
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def f(x):
                return __file__
        """, path=path)
        gen_tree = ast.parse(result.code)
        behaviors = [
            n for n in ast.walk(gen_tree)
            if isinstance(n, ast.FunctionDef) and n.name.startswith("__behavior__")
        ]
        assert behaviors, "no behavior function found in generated code"
        for b in behaviors:
            arg_names = [a.arg for a in b.args.args]
            assert "__file__" not in arg_names, (
                f"{b.name} should not receive __file__ as a parameter; "
                f"got args={arg_names}"
            )
            assert arg_names == ["x"], (
                f"{b.name} expected args ['x'], got {arg_names}"
            )


class TestExportNestedWhen:
    """Nested @when inside a behavior produces multiple behavior functions."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_nested_produces_two_behaviors(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def outer(x):
                @when(x)
                def inner(x):
                    return x.value
                return inner
        """)
        assert len(result.behaviors) == 2


class TestExportMetadata:
    """ExportResult carries class, function, and behavior metadata."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_classes_and_functions_reported(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            class MyClass:
                pass

            def helper():
                pass
        """)
        assert "MyClass" in result.classes
        assert "helper" in result.functions

    def test_behavior_keyed_by_line_number(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def f(x):
                return x.value
        """)
        assert len(result.behaviors) == 1
        line = next(iter(result.behaviors.keys()))
        assert isinstance(line, int)
        assert line > 0


# ── Import alias tests ──────────────────────────────────────────────────


class TestImportAlias:
    """Aliased imports must not appear as captured variables."""

    def test_import_as_not_captured(self):
        """``import X as Y`` — Y should be known, not captured."""
        source = textwrap.dedent("""\
            import collections as col
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def use_alias(x):
                return col.OrderedDict()
        """)
        tree = ast.parse(source)
        result = export_module(tree)

        for info in result.behaviors.values():
            assert "col" not in info.captures, (
                f"'col' should not be captured; captures = {info.captures}"
            )

    def test_from_import_as_not_captured(self):
        """``from X import Y as Z`` — Z should be known, not captured."""
        source = textwrap.dedent("""\
            from collections import OrderedDict as OD
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def use_alias(x):
                return OD()
        """)
        tree = ast.parse(source)
        result = export_module(tree)

        for info in result.behaviors.values():
            assert "OD" not in info.captures, (
                f"'OD' should not be captured; captures = {info.captures}"
            )
