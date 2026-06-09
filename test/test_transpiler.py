"""Tests for the transpiler module."""

import ast
import os
import textwrap

from bocpy.transpiler import BOCModuleTransformer, CapturedVariableFinder, export_module


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

    def test_except_as_name_excluded(self):
        assert "ex" not in self._captures("""\
            def f():
                try:
                    pass
                except RuntimeError as ex:
                    return str(ex)
        """, known_vars={"RuntimeError", "str"})


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


class TestCapturedNestedWhen:
    """Names referenced only inside a nested @when must propagate outward.

    A nested @when is rewritten by ``WhenTransformer`` into a ``whencall(...)``
    in the outer behavior's frame, so its captures and cown arguments must be
    available there. Plain nested ``def``s keep the existing opaque
    treatment because Python's own closure handles them.
    """

    @staticmethod
    def _captures(source, known_vars=frozenset()):
        tree = ast.parse(textwrap.dedent(source))
        finder = CapturedVariableFinder(set(known_vars))
        finder.visit(tree.body[0])
        return finder.captured_vars

    def test_inner_when_capture_propagates(self):
        caps = self._captures("""\
            def outer(c):
                @when(c)
                def _(c):
                    use(marker)
        """, known_vars={"when", "use"})
        assert "marker" in caps

    def test_inner_when_decorator_arg_propagates(self):
        caps = self._captures("""\
            def outer():
                @when(other_cown)
                def _(x):
                    pass
        """, known_vars={"when"})
        assert "other_cown" in caps

    def test_inner_when_locals_not_captured(self):
        caps = self._captures("""\
            def outer():
                @when(c)
                def _(c):
                    x = 1
                    use(x, c)
        """, known_vars={"when", "use"})
        assert caps == {"c"}

    def test_plain_nested_def_unchanged(self):
        caps = self._captures("""\
            def outer():
                def helper():
                    return inner_only
        """)
        assert caps == set()

    def test_deeply_nested_when_propagates(self):
        caps = self._captures("""\
            def outer(c):
                @when(c)
                def _(c):
                    @when(c)
                    def _(c):
                        use(deep_marker)
        """, known_vars={"when", "use"})
        assert "deep_marker" in caps

    def test_when_inside_for_loop_name_not_captured(self):
        caps = self._captures("""\
            def outer(handles):
                bodies = []
                for h in handles:
                    @when(h)
                    def body(h):
                        pass
                    bodies.append(body)
                return bodies
        """, known_vars={"when"})
        assert caps == set()

    def test_when_inside_for_loop_capture_propagates(self):
        caps = self._captures("""\
            def outer(handles):
                for h in handles:
                    @when(h)
                    def body(h):
                        use(marker)
        """, known_vars={"when", "use"})
        assert "marker" in caps

    def test_when_inside_nested_blocks_capture_propagates(self):
        caps = self._captures("""\
            def outer(items):
                for x in items:
                    if x:
                        with ctx():
                            @when(other_cown)
                            def inner(c):
                                use(marker)
                return inner
        """, known_vars={"when", "use", "ctx"})
        assert "other_cown" in caps
        assert "marker" in caps
        assert "inner" not in caps

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


class TestExportDecoratorComposition:
    """Decorator handling: @when is stripped, others are preserved."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_when_stripped_from_behavior(self):
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
                for dec in node.decorator_list:
                    dec_src = ast.unparse(dec)
                    assert "when" not in dec_src, (
                        f"{node.name} still has @when decorator"
                    )

    def test_below_decorator_preserved(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown
            import functools

            x = Cown(1)

            def identity(fn):
                return fn

            @when(x)
            @identity
            def f(x):
                return x.value
        """)
        gen_tree = ast.parse(result.code)
        found = False
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                assert len(node.decorator_list) == 1, (
                    f"expected 1 decorator, got {len(node.decorator_list)}"
                )
                assert ast.unparse(node.decorator_list[0]) == "identity"
                found = True
        assert found, "no __behavior__ function found"

    def test_above_decorator_raises(self):
        import pytest
        with pytest.raises(SyntaxError, match="above @when"):
            self._export("""\
                from bocpy import when, whencall, Cown

                x = Cown(1)

                def log_calls(fn):
                    return fn

                @log_calls
                @when(x)
                def f(x):
                    return x.value
            """)

    def test_unresolvable_decorator_name_raises(self):
        import pytest
        with pytest.raises(SyntaxError, match="not_importable"):
            self._export("""\
                from bocpy import when, whencall, Cown

                x = Cown(1)

                @when(x)
                @not_importable
                def f(x):
                    return x.value
            """)

    def test_decorator_with_module_level_constant_arg(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            MAX_RETRIES = 3

            def retry(n):
                def decorator(fn):
                    return fn
                return decorator

            x = Cown(1)

            @when(x)
            @retry(MAX_RETRIES)
            def f(x):
                return x.value
        """)
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                assert len(node.decorator_list) == 1
                assert "retry" in ast.unparse(node.decorator_list[0])

    def test_async_def_with_when_raises(self):
        import pytest
        with pytest.raises(SyntaxError, match="async"):
            self._export("""\
                from bocpy import when, whencall, Cown

                x = Cown(1)

                @when(x)
                async def f(x):
                    return x.value
            """)

    def test_lambda_in_decorator_does_not_false_positive(self):
        """Names bound by a Lambda inside a decorator must not be flagged."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            def retry(fn):
                def deco(target):
                    return target
                return deco

            @when(x)
            @retry(lambda x: x * 2)
            def f(x):
                return x.value
        """)
        assert "__behavior__" in result.code

    def test_comprehension_in_decorator_does_not_false_positive(self):
        """Comprehension targets are local to the comprehension scope."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)
            REGISTRY = [1, 2, 3]

            def register(items):
                def deco(fn):
                    return fn
                return deco

            @when(x)
            @register([item for item in REGISTRY])
            def f(x):
                return x.value
        """)
        assert "__behavior__" in result.code

    def test_genexp_in_decorator_does_not_false_positive(self):
        """Generator-expression bound names are local to the genexp."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)
            REGISTRY = [1, 2, 3]

            def use(items):
                def deco(fn):
                    return fn
                return deco

            @when(x)
            @use(sum(item for item in REGISTRY))
            def f(x):
                return x.value
        """)
        assert "__behavior__" in result.code

    def test_dictcomp_in_decorator_does_not_false_positive(self):
        """DictComp key/value names are local to the DictComp scope."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)
            REGISTRY = [1, 2, 3]

            def use(d):
                def deco(fn):
                    return fn
                return deco

            @when(x)
            @use({k: k * 2 for k in REGISTRY})
            def f(x):
                return x.value
        """)
        assert "__behavior__" in result.code

    def test_staticmethod_below_when_raises(self):
        import pytest
        with pytest.raises(SyntaxError, match="staticmethod"):
            self._export("""\
                from bocpy import when, whencall, Cown

                x = Cown(1)

                @when(x)
                @staticmethod
                def f(x):
                    return x.value
            """)

    def test_classmethod_below_when_raises(self):
        import pytest
        with pytest.raises(SyntaxError, match="classmethod"):
            self._export("""\
                from bocpy import when, whencall, Cown

                x = Cown(1)

                @when(x)
                @classmethod
                def f(x):
                    return x.value
            """)

    def test_property_below_when_raises(self):
        import pytest
        with pytest.raises(SyntaxError, match="property"):
            self._export("""\
                from bocpy import when, whencall, Cown

                x = Cown(1)

                @when(x)
                @property
                def f(x):
                    return x.value
            """)

    def test_stacked_below_decorators_preserved_in_order(self):
        """Multiple below-decorators are preserved with their source order."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            def deco_a(fn):
                return fn

            def deco_b(fn):
                return fn

            @when(x)
            @deco_a
            @deco_b
            def f(x):
                return x.value
        """)
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                names = [ast.unparse(d) for d in node.decorator_list]
                assert names == ["deco_a", "deco_b"], names

    def test_annassign_constant_resolves_in_decorator(self):
        """``X: int = 3`` makes ``X`` resolvable to a decorator argument."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            MAX_RETRIES: int = 3

            def retry(n):
                def deco(fn):
                    return fn
                return deco

            x = Cown(1)

            @when(x)
            @retry(MAX_RETRIES)
            def f(x):
                return x.value
        """)
        assert "__behavior__" in result.code

    def test_tuple_constant_target_resolves_in_decorator(self):
        """Tuple-target uppercase assignment makes targets resolvable."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            A, B = 1, 2

            def use(x):
                def deco(fn):
                    return fn
                return deco

            x = Cown(1)

            @when(x)
            @use(A + B)
            def f(x):
                return x.value
        """)
        assert "__behavior__" in result.code


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

    def test_nested_when_in_for_loop(self):
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def outer(x):
                bodies = []
                for i, h in enumerate(x.value):
                    @when(h)
                    def inner(h, i=i):
                        return (i, h.value)
                    bodies.append(inner)
                return bodies
        """)
        assert len(result.behaviors) == 2
        for info in result.behaviors.values():
            assert "inner" not in info.captures
        assert "Cannot resolve" not in result.code


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


class TestDefaultsAsCaptures:
    """``def b(c, i=i)`` and ``def b(c, x=y)`` hoist defaults to captures."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_loop_snapshot_idiom(self):
        """``def b(c, i=i)`` — capture ``i`` by name, strip the default."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            def run(c, i):
                @when(c)
                def b(c, i=i):
                    return i
        """)
        info = list(result.behaviors.values())[0]
        assert info.captures == ["i"]
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                assert node.args.defaults == [], (
                    "default for capture must be stripped from behavior signature"
                )
                names = [a.arg for a in node.args.args]
                assert names == ["c", "i"]

    def test_rename_default(self):
        """``def b(c, x=y)`` — capture ``y``, bind into param ``x``."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            c = Cown(0)
            y = 42
            @when(c)
            def b(c, x=y):
                return x
        """)
        info = list(result.behaviors.values())[0]
        assert info.captures == ["y"]
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                names = [a.arg for a in node.args.args]
                assert names == ["c", "x"]
                assert node.args.defaults == []

    def test_undefaulted_extra_captured_by_name(self):
        """``def b(c, factor)`` — bare extra captured by its own name."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            c = Cown(0)
            factor = 3
            @when(c)
            def b(c, factor):
                return factor
        """)
        info = list(result.behaviors.values())[0]
        assert info.captures == ["factor"]

    def test_combined_default_and_body_capture(self):
        """Defaults precede body free-vars in the captures list."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            def run(c, i, factor):
                @when(c)
                def b(c, i=i):
                    return i * factor
        """)
        info = list(result.behaviors.values())[0]
        assert info.captures == ["i", "factor"]

    def test_non_name_default_rejected(self):
        """Non-Name defaults cannot be hoisted — must be a bare name."""
        try:
            self._export("""\
                from bocpy import when, whencall, Cown

                c = Cown(0)
                @when(c)
                def b(c, k=foo()):
                    return k
            """)
        except SyntaxError as e:
            assert "must be a plain name" in str(e)
        else:
            raise AssertionError("expected SyntaxError for non-Name default")

    def test_default_on_cown_position_rejected(self):
        """Defaults on cown positions are not allowed."""
        try:
            self._export("""\
                from bocpy import when, whencall, Cown

                c = Cown(0)
                @when(c)
                def b(c=c):
                    return 1
            """)
        except SyntaxError as e:
            assert "cown positions" in str(e)
        else:
            raise AssertionError("expected SyntaxError for default on cown position")


class TestWhenAlias:
    """Aliased ``when`` decorators are detected and rewritten."""

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_from_import_alias(self):
        """``from bocpy import when as boc_when`` works end-to-end."""
        result = self._export("""\
            from bocpy import when as boc_when, whencall, Cown

            c = Cown(0)
            @boc_when(c)
            def b(c):
                return c.value
        """)
        names = [info.name for info in result.behaviors.values()]
        assert names == ["__behavior__0"]
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                for dec in node.decorator_list:
                    assert "boc_when" not in ast.unparse(dec)

    def test_module_attr_decorator(self):
        """``import bocpy`` + ``@bocpy.when(c)`` is recognized."""
        result = self._export("""\
            import bocpy

            c = bocpy.Cown(0)
            @bocpy.when(c)
            def b(c):
                return c.value
        """)
        names = [info.name for info in result.behaviors.values()]
        assert names == ["__behavior__0"]
        assert "from bocpy import whencall" in result.code
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("__behavior__"):
                for dec in node.decorator_list:
                    assert "bocpy.when" not in ast.unparse(dec)

    def test_module_alias_decorator(self):
        """``import bocpy as boc`` + ``@boc.when(c)`` is recognized."""
        result = self._export("""\
            import bocpy as boc

            c = boc.Cown(0)
            @boc.when(c)
            def b(c):
                return c.value
        """)
        names = [info.name for info in result.behaviors.values()]
        assert names == ["__behavior__0"]
        assert "from bocpy import whencall" in result.code


# Regression: @when result assignment must not be dropped

class TestWhenResultAssignment:
    """@when-decorated functions must produce a name = whencall(...) assignment.

    WhenTransformer.visit_FunctionDef returns an ast.Assign so the behavior
    result is bound to the function's name. visit_Module filters out every
    ast.Expr node (to drop bare expression-statement whencall results), so an
    assignment wrapped in ast.Expr would be discarded and any code reading
    .value, checking .exception, or chaining behaviors on the result would
    operate on None.

    visit_Module only filters at module scope; a @when nested inside a
    function, a method, or another behavior emits its assignment into a
    function body it never inspects. The nesting-level tests below lock
    assignment preservation at every depth.
    """

    @staticmethod
    def _export(source, path="/tmp/test.py"):
        tree = ast.parse(textwrap.dedent(source))
        return export_module(tree, path)

    def test_result_assigned_in_exported_code(self):
        """The behavior name must appear as an assignment target in the export."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def my_task(x):
                return x.value
        """)
        assert "my_task = whencall(" in result.code, (
            "result assignment was dropped from exported module;\n"
            f"generated code:\n{result.code}"
        )

    def test_result_is_ast_assign_not_expr(self):
        """visit_FunctionDef must return an ast.Assign, not an ast.Expr wrapping one.

        visit_Module filters out all ast.Expr nodes; an ast.Expr return
        would silently drop the assignment.
        """
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def my_task(x):
                return x.value
        """)
        gen_tree = ast.parse(result.code)
        assigns = [
            node for node in ast.walk(gen_tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "my_task"
                for t in node.targets
            )
        ]
        assert assigns, (
            "no ast.Assign for 'my_task' found in exported AST; "
            "the assignment was likely wrapped in ast.Expr and dropped.\n"
            f"generated code:\n{result.code}"
        )

    def test_multiple_behaviors_all_assigned(self):
        """Every @when function in the module must be assigned, not just the first."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)
            y = Cown(2)

            @when(x)
            def task_a(x):
                return x.value

            @when(y)
            def task_b(y):
                return y.value
        """)
        assert "task_a = whencall(" in result.code, (
            "'task_a' assignment missing from export;\n"
            f"generated code:\n{result.code}"
        )
        assert "task_b = whencall(" in result.code, (
            "'task_b' assignment missing from export;\n"
            f"generated code:\n{result.code}"
        )

    def test_assignment_store_context(self):
        """The assignment target must use ast.Store context, not ast.Load."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            @when(x)
            def my_task(x):
                return x.value
        """)
        gen_tree = ast.parse(result.code)
        for node in ast.walk(gen_tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "my_task"
                    for t in node.targets
                )
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "my_task":
                        assert isinstance(target.ctx, ast.Store), (
                            f"assignment target 'my_task' has ctx "
                            f"{type(target.ctx).__name__!r}, expected Store"
                        )
                return
        raise AssertionError(
            "no assignment for 'my_task' found in exported AST"
        )

    def test_result_assigned_inside_function(self):
        """A @when inside a plain function keeps its assignment in the body."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            def run():
                @when(x)
                def task(x):
                    return x.value
                return task
        """)
        assert "task = whencall(" in result.code, (
            "in-function @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )

    def test_result_assigned_inside_method(self):
        """A @when inside a method keeps its assignment in the method body."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            class Driver:
                def run(self):
                    @when(x)
                    def task(x):
                        return x.value
                    return task
        """)
        assert "task = whencall(" in result.code, (
            "in-method @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )

    def test_result_assigned_inside_nested_function(self):
        """A @when two function levels deep keeps its assignment."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            def outer():
                def inner():
                    @when(x)
                    def task(x):
                        return x.value
                    return task
                return inner
        """)
        assert "task = whencall(" in result.code, (
            "deeply-nested-function @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )

    def test_result_assigned_inside_for_loop_in_function(self):
        """A @when inside a for loop inside a function keeps its assignment."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            handles = [Cown(1), Cown(2)]

            def run():
                bodies = []
                for h in handles:
                    @when(h)
                    def task(h):
                        return h.value
                    bodies.append(task)
                return bodies
        """)
        assert "task = whencall(" in result.code, (
            "for-loop-in-function @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )

    def test_nested_when_both_results_assigned(self):
        """A nested @when assigns both the outer (module) and inner (in-body) results.

        The outer @when sits at module scope; the inner @when's assignment
        lands inside the extracted ``__behavior__`` body. Both must survive.
        """
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
        assert "outer = whencall(" in result.code, (
            "outer (module-level) @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )
        assert "inner = whencall(" in result.code, (
            "inner (in-behavior) @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )

    def test_nested_when_inside_function_both_results_assigned(self):
        """A nested @when wholly inside a function assigns both results in-body."""
        result = self._export("""\
            from bocpy import when, whencall, Cown

            x = Cown(1)

            def run():
                @when(x)
                def outer(x):
                    @when(x)
                    def inner(x):
                        return x.value
                    return inner
                return outer
        """)
        assert "outer = whencall(" in result.code, (
            "in-function outer @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )
        assert "inner = whencall(" in result.code, (
            "in-behavior inner @when result assignment was dropped;\n"
            f"generated code:\n{result.code}"
        )
