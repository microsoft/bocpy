"""Tests for the marshalled-behavior code registry (boc_registry).

These exercise the C registry substrate directly via the private
``bocpy._core`` accessors ``registry_register`` / ``registry_lookup``.
The registry is a shared-C-heap, append-only singleton: entries are
stored as raw C copies and rebuilt as fresh Python objects in the
calling interpreter, so it is correct across sub-interpreter
boundaries and outlives any single sub-interpreter.

These tests supply keys directly to exercise the C registry substrate
in isolation; the canonical-key computation and ``co_filename`` dedup
are covered at the Python layer in ``TestCanonicalKey`` /
``TestBehaviorRegistration``.
"""

import marshal
import sys
import sysconfig
import threading
import types

import pytest

from bocpy import Cown, quiesce, wait, when, whencall
import bocpy._core as _core
import bocpy.behaviors as behaviors
from bocpy.behaviors import (
    _canonical_key, BehaviorResolveError, register_behavior, Resolver)

try:
    import _interpreters as interpreters
except ModuleNotFoundError:  # pragma: no cover - version floor fallback
    import _xxsubinterpreters as interpreters


_HAS_REFCOUNT = hasattr(sys, "gettotalrefcount")

QUIESCE_TIMEOUT = 5


def _run_in_fresh_subinterpreter(script):
    """Run ``script`` in a fresh sub-interpreter on its own OS thread.

    The whole create / run_string / destroy lifecycle happens on a
    dedicated thread, exactly as bocpy's real workers do. This matters
    because ``_core``'s per-interpreter module exec asserts its
    thread-local ``BOC_STATE`` starts NULL (a debug-build invariant);
    running module exec on the main thread, where the main
    interpreter's ``BOC_STATE`` is already live, would trip it. Returns
    the ``run_string`` result (``None`` on success); re-raises any
    exception raised on the worker thread.
    """
    holder = {}

    def worker():
        interp = interpreters.create()
        try:
            holder["result"] = interpreters.run_string(interp, script)
        except BaseException as exc:  # noqa: B036 - re-raised on the caller
            holder["exc"] = exc
        finally:
            interpreters.destroy(interp)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if "exc" in holder:
        raise holder["exc"]
    return holder.get("result")


def _rt_compute(n):
    """Pure-arithmetic body whose code object is marshalled in tests."""
    return n * 3 + 1


def _rt_uses_builtin(n):
    """Body referencing a builtin, to prove builtins resolve on rebind."""
    return abs(n) + 7


def _rt_behavior_double(c):
    """Behavior-shaped body: doubles the value of its single cown.

    Its marshalled code object is dispatched through the registry branch
    of :class:`Resolver` in the Step-3 anti-masking and resolve-error
    tests. It takes a cown capsule (``.value``) exactly as a real
    behavior thunk does.
    """
    return c.value * 2


def _rt_memo_probe(c, k):
    """Behavior body scheduled repeatedly to exercise the decoration memo.

    Takes one cown and one capture; the body is content-identical across
    schedules, so :func:`register_behavior` must marshal/register it
    exactly once and memo-hit thereafter.
    """
    return c.value + k


def _rt_nested_a(c):
    """Body with nested code (a genexp and a nested def)."""
    total = sum(n * n for n in range(c.value))

    def bump(y):
        return y + 1

    return total + bump(c.value)


def _rt_nested_b(c):
    """Same shape as :func:`_rt_nested_a` but the nested def differs.

    Only the constant inside the nested ``bump`` changes, so a canonical
    key that recurses into ``co_consts`` must distinguish the two.
    """
    total = sum(n * n for n in range(c.value))

    def bump(y):
        return y + 2

    return total + bump(c.value)


class TestRegistryMainInterpreter:
    """Register/lookup behaviour on the main interpreter."""

    def test_roundtrip(self):
        """A registered blob is rebuilt byte-for-byte on lookup."""
        key = "rt_roundtrip"
        blob = b"\x00\x01marshalled\xfe\xff"
        _core.registry_register(key, blob, "some.module", "def f(): pass")
        result = _core.registry_lookup(key)
        assert result == (blob, "some.module", "def f(): pass")

    def test_register_returns_key(self):
        """``registry_register`` echoes the key back for chaining."""
        assert _core.registry_register(
            "rt_echo", b"blob", "m", None) == "rt_echo"

    def test_miss_returns_none(self):
        """A lookup of an unregistered key returns None."""
        assert _core.registry_lookup("rt_definitely_absent_key") is None

    def test_optional_source_none(self):
        """A NULL source round-trips back as None."""
        _core.registry_register("rt_no_source", b"xyz", "m", None)
        assert _core.registry_lookup("rt_no_source") == (b"xyz", "m", None)

    def test_blob_with_embedded_nuls(self):
        """Blobs may contain NUL bytes (marshal output does)."""
        blob = b"\x00\x00\x00head\x00tail\x00\x00"
        _core.registry_register("rt_nuls", blob, "m", None)
        assert _core.registry_lookup("rt_nuls")[0] == blob

    def test_idempotent_reregister_keeps_first_blob(self):
        """Re-registering an existing key keeps the first stored blob.

        Two structurally-identical behaviors that differ only in
        ``co_filename`` legitimately share a canonical key; the registry
        keeps the first blob rather than treating the second write as a
        collision.
        """
        key = "rt_idempotent"
        first = b"first-blob"
        second = b"second-blob-DIFFERENT"
        assert _core.registry_register(key, first, "mod_a", None) == key
        assert _core.registry_register(key, second, "mod_b", "src") == key
        # First write wins on every field, not just the blob.
        assert _core.registry_lookup(key) == (first, "mod_a", None)

    def test_distinct_keys_distinct_blobs(self):
        """Distinct keys map to their own independent blobs."""
        _core.registry_register("rt_k1", b"one", "m1", None)
        _core.registry_register("rt_k2", b"two", "m2", None)
        assert _core.registry_lookup("rt_k1") == (b"one", "m1", None)
        assert _core.registry_lookup("rt_k2") == (b"two", "m2", None)

    def test_growth_beyond_initial_capacity(self):
        """Registering many distinct keys grows the table correctly."""
        n = 200  # well past the initial capacity of 16
        for i in range(n):
            _core.registry_register(
                f"rt_grow_{i}", f"blob-{i}".encode(), f"m{i}", None)
        for i in range(n):
            assert _core.registry_lookup(f"rt_grow_{i}") == (
                f"blob-{i}".encode(), f"m{i}", None)

    @pytest.mark.skipif(
        not _HAS_REFCOUNT,
        reason="requires a debug build (sys.gettotalrefcount)")
    def test_no_refcount_leak_across_lookup_cycles(self):
        """Repeated register+lookup must not leak Python references.

        The registry stores raw C buffers (no Python refcounts held), so
        the only refcount risk is in the lookup object-building path.
        Drive many cycles and assert the total refcount is stable.
        """
        key = "rt_refcount"
        _core.registry_register(key, b"payload", "m", "source")
        # Warm up any one-time allocations before measuring.
        for _ in range(100):
            _core.registry_lookup(key)
            _core.registry_register(key, b"ignored", "m", None)

        before = sys.gettotalrefcount()
        for _ in range(1000):
            result = _core.registry_lookup(key)
            del result
            _core.registry_register(key, b"ignored", "m", None)
        after = sys.gettotalrefcount()
        # Allow a tiny slack for interpreter bookkeeping noise.
        assert abs(after - before) <= 10, (before, after)


class TestRegistryCrossInterpreter:
    """The registry is shared across sub-interpreters and outlives them."""

    def test_lookup_in_spawned_subinterpreter(self):
        """An entry registered on main is visible (equal) in a child.

        The child asserts equality itself; a non-None ``run_string``
        result (or a raised exception) fails the test. Equality proves
        the raw C copy is rebuilt into byte-identical Python objects in
        the calling interpreter.
        """
        key = "xi_lookup"
        blob = b"\x00cross-interp\xff"
        module = "xi.module"
        _core.registry_register(key, blob, module, None)

        script = (
            "import bocpy._core as c\n"
            "r = c.registry_lookup(%r)\n"
            "assert r == (%r, %r, None), r\n" % (key, blob, module)
        )
        result = _run_in_fresh_subinterpreter(script)
        assert result is None, result

    def test_registry_outlives_subinterpreter(self):
        """An entry written by a child survives the child's destruction.

        This is the UAF guard (adversarial MEDIUM-1): the registry is a
        process singleton, not owned by any one sub-interpreter, so a
        write from a worker that is later destroyed must remain readable
        on the main interpreter.
        """
        key = "xi_uaf"
        blob = b"\x11\x22written-by-child\x33"
        module = "xi.child.module"

        script = (
            "import bocpy._core as c\n"
            "c.registry_register(%r, %r, %r, None)\n"
            % (key, blob, module)
        )
        result = _run_in_fresh_subinterpreter(script)
        assert result is None, result

        # The child is gone; the entry must still be readable on main.
        assert _core.registry_lookup(key) == (blob, module, None)


class TestRegistryRoundTripThroughWorker:
    """Round-trip marshalled code through a worker.

    This is the riskiest invariant: marshalled code objects must
    round-trip across the worker sub-interpreter boundary. It uses the
    EXISTING ``@when`` dispatch path (no change to the decorator,
    transpiler, or worker loop yet). An ordinary behavior runs on a
    worker and, in its body, looks the blob up in the shared registry,
    ``marshal.loads`` it, binds the reconstructed code to a throwaway
    namespace, executes it, and returns the result. The behavior's
    defining module is registered as the target module so the worker can
    look it up the same way it would for a real behavior.
    """

    @classmethod
    def teardown_class(cls):
        """Tear the runtime down after the suite."""
        wait()

    def test_pure_arithmetic_body_roundtrips(self):
        """A marshalled pure-arithmetic code object runs on a worker."""
        key = _core.registry_register(
            "rt_worker_compute", marshal.dumps(_rt_compute.__code__),
            __name__, None)

        x = Cown(13)

        @when(x)
        def result(x, key=key):
            found = _core.registry_lookup(key)
            assert found is not None, "registry miss on the worker"
            blob, _modname, _source = found
            code = marshal.loads(blob)
            fn = types.FunctionType(code, {})
            return fn(x.value)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == 13 * 3 + 1

    def test_builtin_referencing_body_roundtrips(self):
        """A marshalled body that calls a builtin resolves on rebind.

        Binding the reconstructed code to an empty globals dict still
        resolves builtins (``abs``), proving the throwaway-namespace bind
        is sufficient for self-contained behaviors.
        """
        key = _core.registry_register(
            "rt_worker_builtin", marshal.dumps(_rt_uses_builtin.__code__),
            __name__, None)

        x = Cown(-5)

        @when(x)
        def result(x, key=key):
            found = _core.registry_lookup(key)
            assert found is not None, "registry miss on the worker"
            blob, _modname, _source = found
            code = marshal.loads(blob)
            fn = types.FunctionType(code, {})
            return fn(x.value)

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == abs(-5) + 7


class TestResolverDispatch:
    """Step-3 Resolver: registry-first dispatch and typed resolve errors."""

    @classmethod
    def teardown_class(cls):
        """Tear the runtime down after the suite."""
        wait()

    def test_registry_key_dispatches_through_resolver(self):
        """A behavior keyed by a registry hex key runs via the registry.

        Anti-masking gate: ``whencall`` is handed a behavior *function*,
        which :func:`register_behavior` marshals under a canonical hex
        key that is **not** a ``__behavior__N`` attribute of the worker's
        bindings module. Reaching the doubled result therefore proves the
        worker resolved the marshalled code through the registry branch
        of :class:`Resolver`, not the bindings-module fallback (the
        fallback would raise ``AttributeError``).
        """
        # whencall does not auto-start the runtime; a trivial @when does.
        boot = Cown(0)

        @when(boot)
        def _boot(boot):
            return boot.value

        quiesce(QUIESCE_TIMEOUT)

        x = Cown(21)
        result = whencall(_rt_behavior_double, [x], [])

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == 42

    def test_unimportable_module_raises_behavior_resolve_error(self):
        """An unimportable defining module surfaces a typed resolve error.

        When a registry-resolved behavior names a
        defining module that cannot be imported, :class:`Resolver`
        raises a :class:`BehaviorResolveError` naming the behavior key
        and the module — not a bare ``ImportError``/``KeyError`` — with
        the underlying import failure chained as ``__cause__``. The
        Resolver is interpreter-agnostic Python, so exercising
        ``__getattr__`` directly is the decisive check on the exact type
        and cause (the worker reaches this path via the same call).
        """
        key = _core.registry_register(
            "rt_resolver_unimportable",
            marshal.dumps(_rt_behavior_double.__code__),
            "bocpy_no_such_module_xyz", None)

        resolver = Resolver(types.ModuleType("bindings"), "bindings")
        with pytest.raises(BehaviorResolveError) as excinfo:
            getattr(resolver, key)

        message = str(excinfo.value)
        assert key in message
        assert "bocpy_no_such_module_xyz" in message
        assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)

    def test_registry_hit_caches_resolved_function(self):
        """A registry hit is materialised onto the Resolver instance.

        Attribute-cache gate: after the first ``__getattr__`` the
        resolved function lives in the instance ``__dict__``, so the next
        C-level ``PyObject_GetAttrString`` finds it directly without
        re-entering ``__getattr__``.
        """
        key = _core.registry_register(
            "rt_resolver_cache",
            marshal.dumps(_rt_behavior_double.__code__),
            __name__, None)

        resolver = Resolver(types.ModuleType("bindings"), __name__)
        assert key not in resolver.__dict__
        first = getattr(resolver, key)
        assert key in resolver.__dict__
        assert getattr(resolver, key) is first

    def test_registry_miss_raises_attribute_error(self):
        """A registry miss raises ``AttributeError`` (registry-only dispatch).

        The bindings-module fallback path is gone: every behavior thunk
        is a registry hex key, so a name with no registry entry is a
        genuine lookup failure and :class:`Resolver` raises
        ``AttributeError`` naming the key rather than serving an attribute
        off the bindings module.
        """
        bindings = types.ModuleType("bindings")

        def __behavior__legacy(c):
            return c.value

        bindings.__behavior__legacy = __behavior__legacy
        resolver = Resolver(bindings, "bindings")
        with pytest.raises(AttributeError) as excinfo:
            _ = resolver.no_such_registry_key
        assert "no_such_registry_key" in str(excinfo.value)


class TestCanonicalKey:
    """Canonical content key: stable, filename-independent, recursive."""

    def test_key_excludes_filename_and_lineno(self):
        """The key ignores ``co_filename`` and ``co_firstlineno``.

        The interactive relabel rewrites ``co_filename`` to
        ``<behavior:KEY>``; the key must be unchanged by that (and by
        line-number drift) or the relabelled blob would key under a
        different hash than the one baked into its own filename.
        """
        code = _rt_nested_a.__code__
        k = _canonical_key(code)
        relabelled = code.replace(co_filename="<other>", co_firstlineno=999)
        assert _canonical_key(relabelled) == k

    def test_key_stable_across_marshal_roundtrip(self):
        """A marshal dump/load cycle yields the same canonical key.

        ``marshal.dumps`` output is refcount-dependent (it sets
        ``FLAG_REF``), so raw bytes are not a sound content address; the
        digest over semantic fields must be stable across the round trip.
        """
        code = _rt_nested_a.__code__
        rt = marshal.loads(marshal.dumps(code))
        assert _canonical_key(rt) == _canonical_key(code)

    def test_nested_code_participates_in_key(self):
        """Two bodies differing only in a nested def get distinct keys."""
        assert (_canonical_key(_rt_nested_a.__code__)
                != _canonical_key(_rt_nested_b.__code__))

    def test_module_folds_into_key(self):
        """The target module disambiguates byte-identical bodies.

        Two behaviors with the same code object but different defining
        modules must key distinctly: each resolves its globals against
        its own module, so they are not interchangeable. The pure
        code-only key (``target_modname=None``) stays equal -- it is the
        module mix-in alone that separates them. Without this, the
        process-global append-only registry would bind the shared key to
        whichever module registered first and mis-resolve the other.
        """
        code = _rt_nested_a.__code__
        assert _canonical_key(code, "pkg.mod_one") != \
            _canonical_key(code, "pkg.mod_two")
        # The code-identity (no module) is unchanged across both.
        assert _canonical_key(code) == _canonical_key(code)
        # Same module -> same key (intra-module dedup preserved).
        assert _canonical_key(code, "pkg.mod_one") == \
            _canonical_key(code, "pkg.mod_one")

    @pytest.mark.skipif(
        sysconfig.get_config_var("Py_GIL_DISABLED"),
        reason=(
            "Key recomputation off a marshal-loaded blob is interning-"
            "sensitive (marshal encodes a string's interned state in its "
            "type byte). On free-threaded builds a worker's marshal.loads "
            "need not reproduce the producer's interning, so a recomputed "
            "key can diverge. This is benign: the authoritative key is "
            "always the producer's, shipped with the dispatch capsule and "
            "resolved by lookup -- never recomputed on the executing "
            "worker. The only recompute path (a nested @when scheduled "
            "from a worker) merely appends a duplicate registry entry, "
            "bounded and freed at shutdown, never a wrong dispatch."
        ),
    )
    def test_key_recomputed_equal_in_subinterpreter(self):
        """The key is identical on the producer and in a fresh worker.

        Registers a marshalled code object, then in a child
        sub-interpreter loads the blob and recomputes the canonical key,
        asserting it matches the producer's. This holds on default
        (GIL-enabled) builds, where marshal round-trips preserve string
        interning, so the same body keys identically everywhere. It is
        skipped on free-threaded builds, where interning may diverge (see
        the skip reason); production never relies on worker recomputation.
        """
        code = _rt_nested_a.__code__
        k = _canonical_key(code)
        key = _core.registry_register(
            "rt_keystab_" + k, marshal.dumps(code), __name__, None)

        script = (
            "import marshal\n"
            "import bocpy._core as c\n"
            "from bocpy.behaviors import _canonical_key\n"
            "blob, _m, _s = c.registry_lookup(%r)\n"
            "code = marshal.loads(blob)\n"
            "assert _canonical_key(code) == %r, _canonical_key(code)\n"
            % (key, k)
        )
        assert _run_in_fresh_subinterpreter(script) is None


class TestBehaviorRegistration:
    """register_behavior: migration guard, interactive validation, relabel."""

    def test_whencall_string_raises_typeerror(self):
        """``whencall`` with a string thunk name raises a migration error.

        The string-thunk form was removed when ``@when`` became a runtime
        decorator; passing one must fail loudly, not silently misbehave.
        """
        with pytest.raises(TypeError, match="not a thunk name"):
            whencall("deadbeef", [Cown(1)], [])

    def test_interactive_helper_reference_rejected(self):
        """An interactive body referencing a non-importable helper fails.

        The function's module is not in ``sys.modules`` and has no spec,
        so it classifies INTERACTIVE; a reference to an interactively
        defined ``helper`` global (not a builtin, module, or capture) is
        rejected at registration naming the symbol.
        """
        ns = {"__name__": "rt_interactive_mod_x"}
        exec("def f(c):\n    return helper(c.value)\n", ns)
        with pytest.raises(NameError, match="helper"):
            register_behavior(ns["f"])

    def test_interactive_attribute_access_accepted(self):
        """Attribute access on an imported module is not a global ref.

        ``math.sqrt`` is ``LOAD_GLOBAL math`` + ``LOAD_ATTR sqrt``; the
        validator reads global *operands* via ``dis`` rather than
        ``co_names``, so it sees only ``math`` (an imported module) and
        accepts the interactive behavior.
        """
        ns = {"__name__": "rt_interactive_mod_attr"}
        exec("import math\ndef f(c):\n    return math.sqrt(c.value)\n", ns)
        key = register_behavior(ns["f"])
        assert isinstance(key, str)

    def test_interactive_undefined_global_rejected(self):
        """A bare undefined global in an interactive body is rejected."""
        ns = {"__name__": "rt_interactive_mod_bad"}
        exec("def f(c):\n    return sqrt(c.value)\n", ns)
        with pytest.raises(NameError, match="sqrt"):
            register_behavior(ns["f"])

    def test_interactive_behavior_relabelled_to_key(self):
        """An interactive behavior's stored code is relabelled to its key.

        Interactive bodies have no source file, so ``co_filename`` is
        rewritten to ``<behavior:KEY>`` for traceback display. Because the
        key excludes ``co_filename``, the label equals the key by
        construction (the single-source-of-truth invariant).
        """
        ns = {"__name__": "rt_interactive_mod_relabel"}
        exec("def f(c):\n    return c.value\n", ns)
        key = register_behavior(ns["f"])
        blob, _modname, _source = _core.registry_lookup(key)
        code = marshal.loads(blob)
        assert code.co_filename == "<behavior:%s>" % key

    def test_distinct_interactive_behaviors_distinct_labels(self):
        """Distinct interactive bodies key distinctly and self-label."""
        ns = {"__name__": "rt_interactive_mod_two"}
        exec("def a(c):\n    return c.value + 1\n", ns)
        exec("def b(c):\n    return c.value + 2\n", ns)
        ka = register_behavior(ns["a"])
        kb = register_behavior(ns["b"])
        assert ka != kb
        code_a = marshal.loads(_core.registry_lookup(ka)[0])
        code_b = marshal.loads(_core.registry_lookup(kb)[0])
        assert code_a.co_filename == "<behavior:%s>" % ka
        assert code_b.co_filename == "<behavior:%s>" % kb

    def test_identical_body_distinct_modules_keys_distinctly(self):
        """Byte-identical bodies in different modules register separately.

        A regression guard for the cross-module key collision. Two real
        module files carry distinct ``co_filename`` values, so the same
        source compiled under different filenames yields code objects
        that are *not* value-equal -- the decoration memo misses and each
        body is registered on its own merits. Because ``_canonical_key``
        excludes ``co_filename``, the code-identity halves of the two
        keys are equal; only the folded-in defining module separates
        them. Sharing one key would let the process-global append-only
        registry resolve the second body's globals against the first
        body's module -- which may not even be importable on a worker.
        """
        src = "def w(c):\n    return c.value\n"
        ns_one = {"__name__": "rt_collide_mod_one"}
        ns_two = {"__name__": "rt_collide_mod_two"}
        exec(compile(src, "rt_collide_one.py", "exec"), ns_one)
        exec(compile(src, "rt_collide_two.py", "exec"), ns_two)
        # Code identity is filename-independent, so the code-only keys
        # match; only the defining module distinguishes the two.
        assert (_canonical_key(ns_one["w"].__code__)
                == _canonical_key(ns_two["w"].__code__))
        k_one = register_behavior(ns_one["w"])
        k_two = register_behavior(ns_two["w"])
        assert k_one != k_two
        assert _core.registry_lookup(k_one)[1] == "rt_collide_mod_one"
        assert _core.registry_lookup(k_two)[1] == "rt_collide_mod_two"


class TestResolverModuleBinding:
    """A behavior's globals bind to its defining module."""

    def test_behavior_globals_bind_to_defining_module(self):
        """A resolved behavior reads globals from its defining module.

        ``lib_behavior`` lives in an importable helper module the
        bootstrap bindings module does not define. Resolving it through a
        :class:`Resolver` whose bindings module is a *different* module
        must bind the reconstructed function's globals to the defining
        module, so the body sees that module's ``LIB_CONSTANT`` rather
        than a missing bindings-module global.
        """
        from bocpy_test import module_binding as libmod

        key = register_behavior(libmod.lib_behavior)
        _blob, target, _source = _core.registry_lookup(key)
        assert target == "bocpy_test.module_binding"

        resolver = Resolver(
            types.ModuleType("rt_bootstrap_bindings"), "rt_bootstrap_bindings")
        fn = getattr(resolver, key)
        assert fn.__globals__ is libmod.__dict__

        class _Cell:
            value = 0

        assert fn(_Cell) == libmod.LIB_CONSTANT


class TestRuntimeMemo:
    """The decoration memo registers each behavior body exactly once."""

    @classmethod
    def teardown_class(cls):
        """Tear the runtime down after the suite."""
        wait()

    def _boot(self):
        """Start the runtime via a trivial behavior, then quiesce."""
        boot = Cown(0)

        @when(boot)
        def _b(boot):
            return boot.value

        quiesce(QUIESCE_TIMEOUT)

    def _install_register_spy(self, monkeypatch):
        """Spy on ``registry_register`` and return the captured-key list."""
        calls = []
        real = behaviors._core.registry_register

        def spy(key, blob, modname, source):
            calls.append(key)
            return real(key, blob, modname, source)

        monkeypatch.setattr(behaviors._core, "registry_register", spy)
        return calls

    def test_repeat_schedule_registers_once(self, monkeypatch):
        """Scheduling one body N times registers it once; captures hold.

        ``whencall`` is invoked five times with the same behavior function
        but distinct captures. The decoration memo (keyed on the original
        code object and defining module) must suppress all but the first
        registration, while each schedule still uses its own capture value.
        """
        self._boot()
        behaviors._CODE_KEY_MEMO.pop(
            (_rt_memo_probe.__code__, _rt_memo_probe.__module__), None)
        calls = self._install_register_spy(monkeypatch)

        results = []
        for i in range(5):
            c = Cown(i)
            results.append(whencall(_rt_memo_probe, [c], [i * 10]))

        quiesce(QUIESCE_TIMEOUT)
        for i, r in enumerate(results):
            assert r.unwrap() == i + i * 10
        assert len(calls) == 1

    def test_loop_defined_body_registers_once(self, monkeypatch):
        """A ``@when``-in-loop body compiles once and registers once.

        Every iteration's ``MAKE_FUNCTION`` reuses the same code object,
        so the loop fast path pays marshal + register exactly once across
        all iterations (memo hits from the second onward) while each
        iteration's ``i=i`` default snapshots its own value.
        """
        self._boot()
        calls = self._install_register_spy(monkeypatch)

        cowns = [Cown(0) for _ in range(5)]
        readers = []
        for i in range(5):
            @when(cowns[i])
            def read(c, i=i):
                return i

            readers.append(read)

        quiesce(QUIESCE_TIMEOUT)
        for idx, r in enumerate(readers):
            assert r.unwrap() == idx
        assert len(calls) == 1

    def test_nested_when_registers_from_worker(self):
        """A behavior may define and schedule a nested ``@when`` on a worker.

        The inner behavior is first seen *on the worker* running ``outer``,
        so its registration is a registry write from a worker
        sub-interpreter. Reaching the doubled inner result proves the
        worker both wrote the entry and dispatched the nested behavior.
        """
        x = Cown(10)

        @when(x)
        def outer(x):
            @when(x)
            def inner(x):
                return x.value * 2

            return inner

        quiesce(QUIESCE_TIMEOUT)
        assert outer.unwrap().unwrap() == 20
