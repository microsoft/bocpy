"""Tests for the transpiler module."""

import ast
import sys
import textwrap
import types

from bocpy.transpiler import (
    bind_live_module,
    bind_main,
    bind_module,
    MainModuleBinder,
)


class TestModuleTransformerImports:
    """Import handling: names are recorded and never rewritten.

    The bindings reducer does not inject ``whencall`` (behavior
    dispatch is the runtime ``@when`` decorator backed by the
    marshalled-code registry), so ``bocpy`` imports pass through
    verbatim. It records ``when`` aliases so module-level ``@when``
    defs can be dropped from the bindings module.
    """

    @staticmethod
    def _transform(source):
        tree = ast.parse(textwrap.dedent(source))
        t = MainModuleBinder()
        t.visit(tree)
        return t, tree

    def test_import_recorded(self):
        t, _ = self._transform("import os")
        assert "os" in t.imports

    def test_from_import_recorded(self):
        t, _ = self._transform("from sys import path")
        assert "path" in t.imports

    def test_bocpy_import_not_rewritten(self):
        t, tree = self._transform("from bocpy import when, Cown")
        aliases = [a.name for a in tree.body[0].names]
        assert "whencall" not in aliases
        assert "whencall" not in t.imports

    def test_non_bocpy_import_not_modified(self):
        _, tree = self._transform("from collections import OrderedDict")
        aliases = [a.name for a in tree.body[0].names]
        assert "whencall" not in aliases

    def test_when_alias_recorded(self):
        t, _ = self._transform("from bocpy import when, Cown")
        assert "when" in t.when_aliases

    def test_aliased_when_recorded(self):
        t, _ = self._transform("from bocpy import when as boc_when, Cown")
        assert "boc_when" in t.when_aliases


class TestModuleTransformerDeclarations:
    """Classes and functions are recorded; @when functions excluded."""

    @staticmethod
    def _transform(source):
        tree = ast.parse(textwrap.dedent(source))
        t = MainModuleBinder()
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

    def test_records_classes_functions_imports(self):
        t, _ = self._transform("""\
            import os
            from sys import path

            class Foo:
                pass

            def bar():
                pass
        """)
        assert t.imports == {"os", "path"}
        assert t.classes == {"Foo"}
        assert t.functions == {"bar"}


class TestModuleTransformerFiltering:
    """Only imports, classes, functions, and eligible assignments survive."""

    @staticmethod
    def _transform(source):
        tree = ast.parse(textwrap.dedent(source))
        t = MainModuleBinder()
        t.visit(tree)
        return t, tree

    def test_constant_assignment_preserved(self):
        _, tree = self._transform("x = 42")
        assert len(tree.body) == 1

    def test_uppercase_non_constant_preserved(self):
        _, tree = self._transform("CONFIG = some_call()")
        code = ast.unparse(tree)
        assert "CONFIG" in code

    def test_lowercase_non_constant_preserved(self):
        # Sub-interpreters cannot share an object, so a module-level
        # singleton is re-evaluated per worker; lowercase bindings are
        # kept alongside UPPERCASE ones so behaviors can resolve them.
        _, tree = self._transform("config = some_call()")
        code = ast.unparse(tree)
        assert "config" in code

    def test_kept_singleton_is_re_evaluated_per_import(self):
        # A kept mutable singleton is re-evaluated every time the reduced
        # bindings are imported, i.e. once per worker sub-interpreter, so
        # each interpreter holds a *distinct* object -- never shared
        # cross-behavior state. Exec'ing the reduced code twice mirrors two
        # workers importing the bindings and must yield non-identical
        # instances; this pins the documented re-instantiation contract.
        _, tree = self._transform("cache = dict(token='v')")
        code = ast.unparse(tree)
        ns_a, ns_b = {}, {}
        exec(code, ns_a)
        exec(code, ns_b)
        assert ns_a["cache"] == ns_b["cache"]
        assert ns_a["cache"] is not ns_b["cache"]

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

    def test_kept_assignment_referencing_dropped_name_raises_on_import(self):
        # A kept module-level assignment whose RHS references a name bound
        # only inside a dropped block (here an ``if``) survives the
        # reduction, but the name it depends on does not. Importing the
        # reduced bindings on a worker then raises a clear ``NameError``
        # rather than hanging -- this pins that documented boundary.
        _, tree = self._transform("""\
            import os
            if os.environ.get("X"):
                base = "/a"
            else:
                base = "/b"
            path = base + "/data"
        """)
        code = ast.unparse(tree)
        assert "path = base" in code
        assert "base = " not in code
        namespace = {}
        try:
            exec(code, namespace)
        except NameError as exc:
            assert "base" in str(exc)
        else:
            raise AssertionError("expected NameError for dropped 'base'")


class TestBindingsOnlyExport:
    """``bind_module`` reduces ``__main__`` to bindings; it extracts no behaviors.

    Since ``@when`` became a runtime decorator (the marshalled-code
    registry path), the worker bindings module must contain only the
    imports, classes, module-level functions, and constants a behavior's
    globals bind against -- never the behaviors themselves. Module-level
    ``@when`` defs are dropped (the decorator schedules them at runtime);
    a ``@when`` nested inside a retained function survives verbatim so it
    reschedules when that function runs on a worker.
    """

    @staticmethod
    def _export(source):
        tree = ast.parse(textwrap.dedent(source))
        return bind_module(tree)

    def test_module_level_when_def_dropped(self):
        result = self._export("""\
            from bocpy import when, Cown

            x = Cown(1)

            @when(x)
            def behaviour(x):
                return x.value
        """)
        assert "def behaviour(" not in result.code
        assert "__behavior__" not in result.code

    def test_plain_module_function_retained(self):
        result = self._export("""\
            from bocpy import when, Cown

            def helper(c):
                return c.value
        """)
        assert "def helper(" in result.code

    def test_nested_when_inside_function_survives(self):
        """A @when nested in a retained function keeps its decorator verbatim."""
        result = self._export("""\
            from bocpy import when, Cown

            GLOBAL = 7

            def schedule(c):
                @when(c)
                def body(c, g=GLOBAL):
                    return c.value + g
                return body
        """)
        # The helper and its nested @when must both survive, unrewritten.
        assert "def schedule(" in result.code
        assert "@when(c)" in result.code
        assert "def body(c, g=GLOBAL)" in result.code
        # No static extraction / call-site rewrite happened.
        assert "whencall(" not in result.code
        assert "__behavior__" not in result.code

    def test_imports_classes_functions_constants_kept(self):
        result = self._export("""\
            import os
            from sys import path

            MAX = 5

            class Widget:
                pass

            def helper():
                return MAX
        """)
        assert "import os" in result.code
        assert "from sys import path" in result.code
        assert "MAX = 5" in result.code
        assert "class Widget:" in result.code
        assert "def helper(" in result.code
        assert "Widget" in result.classes
        assert "helper" in result.functions

    def test_aliased_module_level_when_dropped(self):
        """``@boc_when`` / ``@bocpy.when`` behaviors are dropped like ``@when``."""
        result = self._export("""\
            from bocpy import when as boc_when, Cown

            x = Cown(1)

            @boc_when(x)
            def behaviour(x):
                return x.value
        """)
        assert "def behaviour(" not in result.code

    def test_module_attr_when_dropped(self):
        result = self._export("""\
            import bocpy

            x = bocpy.Cown(1)

            @bocpy.when(x)
            def behaviour(x):
                return x.value
        """)
        assert "def behaviour(" not in result.code

    def test_bindings_recompiles(self):
        """The emitted bindings module must be valid, compilable Python."""
        result = self._export("""\
            from bocpy import when, Cown

            CONST = 3

            def helper(c):
                @when(c)
                def body(c, k=CONST):
                    return c.value * k
                return body

            @when(Cown(1))
            def top(x):
                return x.value
        """)
        compile(result.code, "<bindings>", "exec")
        assert "def top(" not in result.code
        assert "def helper(" in result.code


class TestBindLiveModule:
    """Sourceless reduction for the REPL / ``python -c`` / ``exec``.

    A module with no source file on disk cannot be parsed, so the
    bindings come from the live namespace: an ``import`` statement per
    imported module (exactly the names an interactive behavior's globals
    may bind against), and nothing else.
    """

    @staticmethod
    def _make(ns):
        mod = types.ModuleType("live")
        mod.__dict__.update(ns)
        return bind_live_module(mod)

    def test_imported_module_becomes_import(self):
        import math
        result = self._make({"math": math})
        assert "import math" in result.code
        compile(result.code, "<bindings>", "exec")

    def test_import_is_guarded_against_importerror(self):
        """Each synthesized import is wrapped so a hostile module is skipped.

        An interactive namespace carries modules the user never imported
        (the REPL injects ``readline``) and some refuse to load in a
        sub-interpreter. The bindings must tolerate that: a failing
        import is skipped, not fatal. Execute the synthesized code in a
        namespace where the target import raises and assert it survives.
        """
        import math
        result = self._make({"math": math, "_boom": math})
        # Rewrite so importing the second name raises, mimicking a module
        # that refuses to load in a worker (e.g. readline).
        code = result.code.replace("import math as _boom",
                                   "import _bocpy_no_such_mod_xyz as _boom")
        ns = {}
        exec(compile(code, "<bindings>", "exec"), ns)
        assert "math" in ns
        assert "_boom" not in ns

    def test_denied_modules_are_not_emitted(self):
        """REPL-injected noise modules are never re-imported on a worker.

        The interactive REPL injects ``readline``/``rlcompleter`` into
        ``__main__``. A worker behavior never references them, and on
        pre-3.12 shared-GIL sub-interpreters importing ``readline``
        succeeds and blocks on the controlling terminal. They are denied
        from the bindings outright; a sibling real module is unaffected.
        """
        import math
        readline = types.ModuleType("readline")
        rlcompleter = types.ModuleType("rlcompleter")
        result = self._make({
            "readline": readline,
            "rlcompleter": rlcompleter,
            "math": math,
        })
        assert "readline" not in result.code
        assert "rlcompleter" not in result.code
        assert "import math" in result.code
        compile(result.code, "<bindings>", "exec")

    def test_aliased_module_keeps_alias(self):
        import math
        result = self._make({"m": math})
        assert "import math as m" in result.code

    def test_non_module_values_are_dropped(self):
        result = self._make({"x": 7, "helper": lambda c: c})
        assert "x" not in result.code
        assert "helper" not in result.code

    def test_dunders_are_skipped(self):
        import math
        result = self._make({"__name__": "live", "__cached__": math})
        assert "__cached__" not in result.code

    def test_empty_namespace_yields_empty_bindings(self):
        result = self._make({})
        assert result.code == ""
        assert result.classes == set()
        assert result.functions == set()

    def test_non_identifier_key_is_skipped(self):
        """A module bound under a non-identifier key is not emitted.

        ``vars(module)`` keys are usually identifiers, but a crafted
        namespace (``ns["x; import os"] = mod``) could inject malformed
        source into the worker-compiled bindings. Such an entry is
        dropped, and what remains still compiles.
        """
        import math
        result = self._make({"bad key": math, "math": math})
        assert "bad key" not in result.code
        assert "import math" in result.code
        compile(result.code, "<bindings>", "exec")

    def test_non_identifier_module_name_is_skipped(self):
        """A module whose ``__name__`` is not import-safe is dropped.

        ``types.ModuleType("not-an-identifier")`` (or one with a
        ``None``/non-``str`` ``__name__``) would emit ``import
        not-an-identifier`` and fail the worker ``compile``. It is
        skipped so the remaining bindings stay valid.
        """
        import math
        hyphen = types.ModuleType("not-an-identifier")
        nameless = types.ModuleType("placeholder")
        nameless.__name__ = None
        result = self._make({"a": hyphen, "b": nameless, "math": math})
        assert "not-an-identifier" not in result.code
        assert "import math" in result.code
        compile(result.code, "<bindings>", "exec")

    def test_dotted_module_name_is_emitted_and_compiles(self):
        """A submodule (dotted ``__name__``) is emitted and stays valid."""
        import urllib.parse
        result = self._make({"p": urllib.parse})
        assert "import urllib.parse as p" in result.code
        compile(result.code, "<bindings>", "exec")


class TestBindMain:
    """``bind_main`` reduces the *named* loaded module, not always ``__main__``.

    A sourceless first ``@when`` scheduled from a non-``__main__`` module
    must bind that module's namespace; otherwise the worker would import
    ``__main__``'s globals and the behavior's free names would not
    resolve.
    """

    def test_reduces_named_sourceless_module(self):
        import math
        mod = types.ModuleType("live_pkg.live_mod")
        mod.__name__ = "live_pkg.live_mod"
        mod.math = math
        sys.modules["live_pkg.live_mod"] = mod
        try:
            result = bind_main("live_pkg.live_mod")
        finally:
            del sys.modules["live_pkg.live_mod"]
        assert "import math" in result.code

    def test_unknown_name_falls_back_to_main(self):
        # A name absent from sys.modules must not raise; it reduces
        # __main__ instead so scheduling still proceeds.
        result = bind_main("bocpy_no_such_module_xyz")
        compile(result.code, "<bindings>", "exec")

    def test_defaults_to_main(self):
        result = bind_main()
        compile(result.code, "<bindings>", "exec")
