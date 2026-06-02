## 2026-06-02 - Version 0.8.0
Vector-oriented ``Matrix`` API — six new methods (``vecdot``,
``cross``, ``normalize``, ``perpendicular``, ``angle``,
``magnitude_squared``), two new read-only properties (``size``,
``length``), and a unified ``in_place=`` keyword on every unary
method round out ``Matrix`` as a first-class vector and
batch-of-vectors type — plus an internal X-macro template refactor
of every ``_math.c`` op family that restores the compiler's
auto-vectoriser. 44 of 71 benched rows improved by ≥10%, with
representative wins of −50% to −88% on aggregates, broadcast
arithmetic, and ``normalize``. The ``_math`` extension now ships
with ``-O3`` (Linux/macOS) / ``/O2`` (Windows) so end users pick
up the wins by default.

**New Features**

- **Vector-oriented ``Matrix`` methods** — six new methods designed
  for the ``Nx2`` / ``2xN`` / ``Nx3`` / ``3xN`` vector and
  batch-of-vectors shapes that show up in ``examples/boids.py`` and
  similar simulation code:

  - ``magnitude_squared(axis=None)`` — squared L2 norm without the
    ``sqrt`` step. Cheaper than ``magnitude()`` and safe for
    sub-normal thresholding.
  - ``vecdot(other, axis=None)`` — axis-aware inner product matching
    ``numpy.linalg.vecdot``. **Not** equivalent to ``numpy.dot``;
    use ``@`` for matrix multiplication. Same-shape, row-broadcast
    (``1xN`` vs ``MxN``), and column-broadcast (``Mx1`` vs ``MxN``)
    operands are all supported.
  - ``cross(other, axis=None)`` — 2D scalar z-component or 3D cross
    product. Five shape paths share one method: ``1x2`` / ``2x1``
    returns a float; ``1x3`` / ``3x1`` returns a same-orientation
    ``Matrix``; ``Nx2`` / ``2xN`` batches collect per-vector
    scalars; ``Nx3`` / ``3xN`` batches return same-shape ``Matrix``
    results. ``axis=`` disambiguates the square ``2x2`` / ``3x3``
    shapes (default per-row).
  - ``normalize(axis=None, in_place=False)`` — divide every element
    by its magnitude. Zero-magnitude rows / columns are returned as
    exact zeros (no NaN, no division by zero). ``axis=`` selects
    per-row, per-column, or total normalisation.
  - ``perpendicular(axis=None, in_place=False)`` — rotate every 2D
    vector 90° counter-clockwise: ``(x, y) -> (-y, x)``. Accepts a
    single 2D vector, an ``Nx2`` row batch, or a ``2xN`` column
    batch.
  - ``angle(axis=None)`` — polar angle ``atan2(y, x)`` of every 2D
    vector. Returns a float for a single 2D vector input,
    otherwise a ``Matrix`` of per-vector angles.
- **``Matrix.size`` property** — total element count
  (``rows * columns``). Matches ``numpy.ndarray.size``.
- **``Matrix.length`` property** — Frobenius (L2) magnitude as a
  read-only ``@property`` so vector-like code reads naturally
  (``direction.length``, ``velocity.length``) without the
  parentheses of a method call. Equivalent to ``magnitude()`` with
  no axis argument.
- **``in_place=`` keyword on every unary ``Matrix`` method** —
  ``transpose``, ``ceil``, ``floor``, ``round``, ``negate``,
  ``abs``, plus the new ``normalize`` and ``perpendicular`` all
  accept ``in_place=True`` to mutate ``self`` and return it.
  Replaces the older ``transpose_in_place()`` method (see
  **Breaking Changes** below).
- **``axis=`` keyword on aggregate methods** — ``sum``, ``mean``,
  ``min``, ``max``, ``magnitude``, and the new ``magnitude_squared``
  now share a tri-state ``axis=`` argument (``None`` / ``0`` / ``1``)
  decoded through a single classifier. Negative axes (``-1`` /
  ``-2``) accepted for NumPy parity.

**Improvements**

- **Auto-vectorised ``_math.c`` op kernels** — the binary,
  aggregate, unary, and two-operand-aggregate op families inside
  ``_math.c`` are now stamped from per-family descriptor tables,
  one kernel per (op, shape) combination. Each per-element body is
  literally substituted into its own monomorphic inner loop,
  restoring the precondition for GCC's / Clang's auto-vectoriser.
  Representative wins (lower is better):

  | Bench row                                 | 0.7.0 (ns) | 0.8.0 (ns) | Δ       |
  | ----------------------------------------- | ---------- | ---------- | ------- |
  | ``mean()`` shape=(1000, 100)              | 44179.6    | 9001.6     | −79.6%  |
  | ``mean(1)`` shape=(1000, 100)             | 51699.4    | 7058.5     | −86.3%  |
  | ``max(1)`` shape=(1000, 100)              | 97184.2    | 11322.7    | −88.3%  |
  | ``magnitude()`` shape=(1000, 3)           | 1098.2     | 306.8      | −72.1%  |
  | ``add col-bcast`` shape=(1000, 100)       | 37823.4    | 20172.5    | −46.7%  |
  | ``div same-shape`` shape=(1000, 100)      | 80134.2    | 45458.9    | −43.3%  |
  | ``normalize()`` shape=(1000, 3) axis=None | 3644.6     | 1775.5     | −51.3%  |

  Four rows in code paths untouched by the refactor regressed by
  5–15% from layout drift (``_math.so`` ``.text`` grew +125% from
  kernel specialisation); none are on a hot path. No behavioural
  change; ``test_matrix.py`` passes unchanged.
- **``-O3`` / ``/O2`` on ``bocpy._math``** — the math extension now
  sets per-platform ``extra_compile_args`` in ``setup.py``
  (``-O3 -fno-plt`` on Linux/macOS, ``/O2`` on Windows) so end-user
  wheels and editable installs both pick up the auto-vectoriser
  wins above. Other ``bocpy`` extensions are unaffected. The SBOM
  hash for ``_math.*.so`` will drift accordingly — see
  :doc:`sbom` for the auditor-facing note.

**Breaking Changes**

- **``Matrix.transpose_in_place()`` removed** — superseded by
  ``Matrix.transpose(in_place=True)``, which returns ``self`` and
  so composes the same way every other unary method does.
  Migration is mechanical: replace ``m.transpose_in_place()`` with
  ``m.transpose(in_place=True)``.

**Documentation**

- New ``Matrix`` API entries in :doc:`api` for ``size``, ``length``,
  ``magnitude_squared``, ``vecdot``, ``cross``, ``normalize``,
  ``perpendicular``, and ``angle``, plus updated ``in_place=``
  keyword signatures on the existing unary methods.

**Tests**

- **234 new test cases** for the new ``Matrix`` methods and
  properties (1571 → 1805 passed). Coverage includes a stub-guard
  test that greps ``__init__.pyi`` for every new C-level name and
  in-cown coverage exercising each new method inside ``@when``.
- **Portable overflow regex + cross 2x3/3x2 contract pinning** —
  the cross-product test for the doubly-valid ``2x3`` / ``3x2``
  shapes now pins the 2D-batch interpretation explicitly, locking
  the documented behaviour.

**Internal**

- **``scripts/bench_matrix.py``** — bench harness used to gate the
  refactor: ``--json`` append mode, ``--report-median`` per-row
  merge, 200 ms warmup, batch-size auto-tuning.
- **``scripts/validate_wheel.py`` +
  ``scripts/_vendored_warehouse_wheel.py``** — stdlib-only wheel
  ``RECORD`` validator and a vendored slice of Warehouse's wheel
  parser; used by the PR gate to catch ``RECORD`` regressions
  before PyPI does.

**CI / build**

- **``cibuildwheel`` v3.4.0 → v3.4.1** and **``clang-format-action``**
  pin normalised to the underlying commit SHA (Dependabot's
  preferred format). Both pins move in lock-step with the
  github-actions Dependabot group.
- **``idna`` 3.16 → 3.17** in ``ci/constraints-docs.txt``. Five
  other Dependabot proposals (``docutils`` 0.23, ``ruamel-yaml``
  0.19, ``sphinx-tabs`` 3.4.7+, ``sphinx-toolbox`` 4.2, and
  ``standard-imghdr`` 3.13) require Python ≥3.11 and so cannot
  enter a universal lock that still includes Python 3.10; a
  comment above ``requires-python = ">=3.10"`` in
  ``pyproject.toml`` lists them for the post-3.10-EOL bump.
- **``flake8`` ``extend-exclude``** for ``.copilot/``, ``build/``,
  ``sphinx/build/``, and the scratch ``.env*`` venvs so the walker
  no longer trips on generated or vendored Python files.

## 2026-05-28 - Version 0.7.0
Cown-lifecycle correctness fixes — three use-after-free paths in the
``CownCapsule`` pickle / acquire / noticeboard machinery now hold the
inner ``BOCCown`` alive across the writer's wrapper drop — plus
supply-chain hardening: pinned and hash-verified Python dependencies,
SHA-pinned GitHub Actions, dependabot coverage, vulnerability scanning,
and PEP 770 SBOMs embedded in every wheel.

**New Features**

- **PEP 770 SBOMs in every wheel** — every wheel built by
  ``.github/workflows/build_wheels.yml`` now embeds a
  `CycloneDX 1.6 <https://cyclonedx.org/specification/overview/>`_
  JSON SBOM under ``<dist>-<version>.dist-info/sboms/bocpy.cdx.json``.
  Generation runs inside cibuildwheel's repair step on every platform
  (Linux ``auditwheel``, macOS ``delocate``, Windows direct injection)
  via the new stdlib-only ``scripts/build_sbom.py``. The
  ``inject`` subcommand rewrites the wheel's ``RECORD`` atomically
  (temp file + rename).
- **SBOM verification in CI** — the new ``verify_sboms`` job in
  ``build_wheels.yml`` re-downloads the extracted SBOM artifact and
  runs two checks: ``scripts/validate_sbom.py`` (stdlib-only
  structural validator pinning bocpy's wire format) and
  `grype <https://github.com/anchore/grype>`_ (third-party SBOM
  scanner) with ``--fail-on high``. A separate ``sboms`` artifact is
  also uploaded by the ``merge`` job for downstream consumers.
- **``bocpy.__version__``** — a runtime version attribute derived
  from ``importlib.metadata.version("bocpy")``, with a
  ``PackageNotFoundError`` fallback. Exported from ``bocpy.__all__``
  and documented in ``__init__.pyi``. ``pyproject.toml`` remains the
  single source of truth for the version.
- **New documentation** — :doc:`sbom` walk-through covering the
  embedded SBOM format, extraction recipes, and verification commands.
- **``wait(noticeboard=True)`` final-state capture** — :func:`wait`
  now accepts a ``noticeboard`` keyword that returns the final
  noticeboard contents as a plain ``dict`` at shutdown (after the
  noticeboard thread exits, before the entries are freed). Useful
  for surfacing an early-stopping result, last error, or aggregated
  counter that a behavior deposited just before the runtime
  quiesced, replacing the older ``send`` / ``receive`` handshake
  that earlier examples used. Combined with ``stats=True`` it
  returns a new :class:`WaitResult` ``NamedTuple`` (also exported
  from ``bocpy.__all__``) carrying both snapshots. The
  ``examples/prime_factor.py`` example was migrated to the new
  pattern.

**Bug Fixes**

- **Cown-in-cown use-after-free** — a ``Cown`` embedded inside
  another cown's value, a message-queue payload, or a noticeboard
  snapshot was previously freed when the writer's local wrapper
  dropped, because pickle bytes carry no refcount on their own.
  ``CownCapsule_reduce`` now takes an inheriting ``COWN_INCREF`` that
  ``_cown_capsule_from_pointer_inheriting`` consumes on unpickle, so
  the inner ``BOCCown`` survives until the consumer drops its
  decoded wrapper. Affects every cross-cown reference shape — see
  the new ``TestCownInCown`` class for the full container-shape fuzz.
- **Acquire-failure poisoned-state** — when ``pickle.loads`` failed
  partway through ``cown_acquire``, the cown was left in a
  half-acquired state with the encoded bytes still in place. A retry
  would re-run pickle against bytes whose embedded inherited refs
  had already been partially consumed by pickle's error path,
  risking dereferences of freed ``BOCCown*`` pointers. The cown's
  ``xidata`` is now recycled on the failure path and a guard at the
  top of ``cown_acquire`` rejects any future acquire with a
  deterministic ``RuntimeError``; the worker recovery arm surfaces
  it on the failing behavior's result cown.
- **Noticeboard hidden-cown audit** — when a noticeboard value
  reached a ``Cown`` via a route the pin walker cannot see — custom
  ``__reduce__`` / ``__getstate__``, ``copyreg.dispatch_table``,
  closure capture, module-level cache — the borrowing reconstructor
  produced a token whose inner ``BOCCown`` was not held alive by
  the entry's pin set, leaving the next reader to UAF after the
  writer's wrapper dropped. A per-thread borrowing context
  (``BOC_NB_CTX``) now audits every ``CownCapsule_reduce`` against
  the caller's pin set during the noticeboard write pickle and
  fails the whole ``notice_write`` / ``notice_update`` closed if
  any cown is unaccounted for.
- **`UnicodeDecodeError` on non-UTF-8 Windows locales** —
  ``Behaviors.start`` read ``worker.py`` with ``open(path)``, which
  picks up ``locale.getpreferredencoding(False)``. On cp1252
  (English Windows) the UTF-8 em-dashes in the worker source were
  silently mojibake-d; on cp949 (Korean Windows) the read failed
  with ``UnicodeDecodeError: 'cp949' codec can't decode byte 0xe2``
  and ``bocpy`` could not start at all (reported in
  `#14 <https://github.com/microsoft/bocpy/issues/14>`_ by
  `@Forthoney <https://github.com/Forthoney>`_). Fixed by passing
  ``encoding="utf-8"`` explicitly in ``Behaviors.start``, and the
  same fix was applied to every other ``open()`` site in the repo
  that reads or writes text known to contain non-ASCII bytes
  (``sphinx/source/conf.py``, ``examples/sketches.py`` x2,
  ``export_module.py``).
- **Silent worker-startup failures** — ``Behaviors.start_workers``
  ran ``interpreters.create()`` and ``interpreters.run_string()``
  on the worker thread without a try/except, so a failure in either
  killed the thread without ever replying on ``boc_behavior``. The
  parent's bounded ``receive()`` then timed out with no diagnostic.
  Both calls are now wrapped, and every failure path sends a
  formatted traceback over ``boc_behavior`` so the parent sees a
  structured error instead of a timeout.
- **Silent worker bootstrap import failures** — the generated
  bootstrap script that loads the user module into each worker
  sub-interpreter is now wrapped in a top-level try/except. Any
  ``BaseException`` is formatted with the user module name and sent
  over ``boc_behavior`` (falls back to ``sys.stderr`` if the
  message-queue ``send`` itself raises), then re-raised so
  ``run_string`` reports it as well. Module-import failures that
  previously surfaced only as a worker-startup timeout now arrive
  as a proper traceback.
- **``boc_sched_worker_pop_slow`` skipped ``popped_local``** — the
  slow-path pending-fallback and WSQ-dequeue branches returned
  work without bumping ``popped_local`` (the fast path always
  did), so the documented producer/consumer identity in
  :c:type:`boc_sched_stats_t` was violated whenever the fairness
  arm fired or a worker entered the slow path directly. Both
  branches now increment ``popped_local`` and reset the batch
  budget, matching the fast path. The header's reconciliation
  paragraph was also tightened to a "near-identity" that explicitly
  accounts for fairness-token pops (which are re-enqueued via raw
  ``boc_wsq_enqueue`` rather than ``boc_sched_dispatch``, leaving
  consumer-side counters without a matching producer-side bump).

**Supply Chain**

- **Hashed and pinned Python dependencies** — every CI dependency is
  resolved into a ``ci/constraints-<extra>.txt`` file via
  ``uv pip compile --universal --generate-hashes`` and installed with
  ``pip install --require-hashes``. Covers the ``test``, ``linting``,
  ``docs``, and new ``audit`` extras. ``bocpy`` itself is then
  installed via ``pip install -e . --no-deps`` so an editable build
  cannot smuggle in an unpinned transitive dependency.
- **Vulnerability scanning** — new ``audit`` job in ``pr_gate.yml``
  runs ``pip-audit --strict`` against every constraints file on every
  PR. ``pip-audit`` itself is pinned via ``ci/constraints-audit.txt``
  and self-checked. A new ``.github/workflows/nightly_audit.yml``
  re-runs the audit nightly against ``main``.
- **SHA-pinned GitHub Actions** — every ``uses:`` line in
  ``.github/workflows/`` is now pinned to a full 40-char commit SHA
  with a trailing ``# vX.Y.Z`` comment.
- **Dependabot coverage** — new ``.github/dependabot.yml`` covers
  three ecosystems (``pip`` rooted at ``/ci``, ``github-actions``
  rooted at ``/``, ``pip`` rooted at
  ``/templates/c_abi_consumer``), grouped weekly per ecosystem.
- **Downstream template pinned** — ``templates/c_abi_consumer``
  pins ``bocpy~=MAJOR.MINOR`` as both a build requirement and a
  runtime dependency. The ``finalize-pr`` skill bumps it in
  lock-step with the root version.
- **New ``SUPPLY_CHAIN.md``** — top-level policy doc describing
  everything above with the exact regeneration commands.

**Documentation**

- **Cown pickle-leak note** — :class:`Cown` now documents that
  ``pickle.dumps`` on a cown produces bytes that carry one strong
  reference per embedded cown; orphan bytes (never unpickled in the
  producing process) leak one strong ref per byte string. The bocpy
  runtime never produces orphan bytes; the leak surface only
  applies to third-party code that calls ``pickle.dumps(cown)``
  directly.
- **Noticeboard cown-lifetime guarantee** — :func:`notice_write` and
  :func:`notice_update` now document that values may embed
  :class:`Cown` references and that the noticeboard keeps each
  embedded cown alive for as long as the entry remains. The new
  paragraph in :doc:`noticeboard` mirrors this guarantee for
  readers.
- **Noticeboard final-state capture guide** — :doc:`noticeboard`
  gained a "Reading the Final State at Shutdown" section covering
  the ``wait(noticeboard=True)`` contract, the combined
  ``wait(stats=True, noticeboard=True)`` form returning
  :class:`WaitResult`, the empty-dict fallbacks for the
  never-started and never-written cases, and the recommendation
  to use ``snap.get(key)`` since :func:`wait` quiesces as soon as
  every behavior completes with no guarantee any particular write
  has landed. The early-stopping worked example in the same file
  was rewritten around the new API.

**Tests**

- **``TestCownInCown``** in ``test/test_boc.py`` — pins the
  cown-in-cown UAF fix with three cases: an inner cown allocated
  inside a behavior and observed by a downstream behavior, a cown
  sent through the message queue and consumed by the receiver, and
  a 50-trial deterministic fuzz over seven container shapes
  (``list`` / ``tuple`` / ``dict`` / ``@dataclass(slots=True)`` /
  ``__dict__``-only / ``__slots__``-only / 2-level ``Cown[Cown[T]]``).
- **``TestAcquireFailureTerminal``** in ``test/test_boc.py`` — pins
  the poisoned-state contract: after a deserialisation failure the
  cown stays permanently unavailable and every subsequent waiter
  receives the deterministic ``RuntimeError`` on its result cown.
- **Noticeboard hidden-cown regressions** in
  ``test/test_noticeboard.py`` — exercises ``__reduce__`` and
  ``copyreg.dispatch_table`` reductions that hide a cown from the
  pin walker, and verifies the audit rejects the write closed
  rather than leaving an unpinned borrowing token in the entry.
  A complementary ``_VisibleCownPair`` test guards against the
  over-eager-rejection regression.
- **``test/test_version.py``** — covers ``bocpy.__version__``:
  pyproject parity, PEP 440 shape, ``__all__`` export, and the
  ``importlib.metadata`` fallback path (subprocess test that
  verifies the WARNING is emitted when the metadata lookup raises).
- **``test/test_build_sbom.py`` and ``test/test_validate_sbom.py``**
  — full coverage of the SBOM generator and validator: CycloneDX
  1.6 shape, deterministic UUIDv5 serialNumber,
  ``SOURCE_DATE_EPOCH`` timestamp, per-entry ZIP-attribute
  preservation (``external_attr`` / ``create_system`` /
  ``compress_type`` / ``date_time``) across symlink and
  ``ZIP_STORED`` entries, atomic ``RECORD`` rewrite, and the CLI
  ``generate`` / ``inject`` / ``validate`` modes.
- **``TestWaitNoticeboardCapture``** in ``test/test_noticeboard.py``
  — pins the ``wait(noticeboard=True)`` contract: returned dict is a
  plain mutable ``dict``, empty-runtime / empty-noticeboard fallbacks
  to ``{}``, single-flag back-compat (``wait()`` stays ``None``,
  ``wait(stats=True)`` stays ``list``), combined-flag
  :class:`WaitResult` shape, last-write-wins, delete propagation
  through a chained behavior, fresh-session isolation, and the
  single-shot guarantee that an explicit ``stop()`` followed by
  ``wait(noticeboard=True)`` preserves the snapshot rather than
  re-snapshotting the now-empty noticeboard. The existing
  scheduler-stats tests in ``test/test_scheduler_stats.py`` were
  simplified to use the cown-chain barrier directly rather than a
  ``send``/``receive`` handshake, now that the same change is
  exercised end-to-end by the new ``wait(noticeboard=True)`` tests.

**Internal**

- ``flake8`` now lints ``.pyi`` stubs (the default ``--filename``
  glob silently skipped them). Pre-existing defects in
  ``__init__.pyi``, ``_core.pyi``, and ``test_boc.py`` cleaned up in
  the same pass. The workflow also lints the new ``scripts/``
  directory.
- **`flake8-encodings` added to the `[linting]` extra** — pins the
  Windows-locale class of bug above as a permanent regression gate.
  Any future ``open()`` call without an explicit ``encoding=``
  (or with ``encoding=None``) now fails the PR-gate lint job. The
  plugin and its transitive dependencies (``flake8-helper``,
  ``astatine``, ``domdf-python-tools``, ``natsort``) are pinned and
  hash-verified in ``ci/constraints-linting.txt`` like every other
  CI dependency.
- **Defensive ``receive()`` timeouts on every lifecycle path** —
  ``Behaviors.start_workers``, ``stop_workers``, ``_abort_workers``,
  and the noticeboard mutator loop now pass a bounded timeout to
  every ``_core.receive()`` they own. A wedged worker therefore
  fails fast with a deterministic ``RuntimeError`` instead of
  hanging the parent forever. Defence in depth against the
  sub-interpreter wedge observed on macOS arm64 + Python 3.12/3.13.
- **No ``unittest.mock`` in test files that schedule ``@when``** —
  the transpiler exports the whole test module for import in every
  worker sub-interpreter, so a top-level ``from unittest import
  mock`` triggers an ``import asyncio`` in every worker. On macOS
  arm64 + Python 3.12/3.13 this can deadlock during PEP 684
  per-interpreter init. Replaced by a small in-house
  ``test/mockreplacement.py`` (``patch_attr`` context manager +
  ``Recorder`` / ``RecorderMethod`` stubs) imported lazily inside
  the few tests that need it. The pitfall is documented in the
  ``testing-with-boc`` skill.

## 2026-05-10 - Version 0.6.0
Public C ABI for downstream extensions, enabling C-level participation
in behavior-oriented concurrency across worker sub-interpreters.

**New Features**

- **Decorator composition with ``@when``** — decorators stacked below
  ``@when`` are now preserved on the generated behavior function and
  compose with the behavior body on the worker.  Decorators placed
  above ``@when`` raise a ``SyntaxError`` at transpile time with
  actionable guidance.  ``async def`` functions with ``@when`` are
  also explicitly rejected.
- **Public C ABI (`<bocpy/bocpy.h>`)** — downstream C extensions can
  now link against bocpy to register custom Python types as
  cross-interpreter shareable so `Cown` can carry instances of
  them across worker interpreters. The header is C-only, version-gated
  via the ``BOCPY_ABI`` macro, and bumped on any incompatible change
  to ``bocpy.h`` or ``xidata.h``. Wheels remain CPython-version-tagged
  so a runtime ABI mismatch cannot occur.
- **`bocpy.get_include()` / `bocpy.get_sources()`** — Python-level
  helpers that downstream ``setup.py`` files use to locate the bocpy
  headers and the small set of C sources that must be compiled into
  the consuming extension.
- **`templates/c_abi_consumer/`** — a ready-to-copy template for
  building a C extension against the bocpy ABI, including a
  ``setup.py``, a probe extension exercising the public surface, and
  a pytest suite (``test_public_c_abi.py``) that validates the ABI
  end-to-end.
- **C source reorganisation** — the per-subsystem translation units
  introduced in 0.5.0 have been renamed with a ``boc_`` prefix
  (``boc_compat.[ch]``, ``boc_sched.[ch]``, ``boc_tags.[ch]``,
  ``boc_terminator.[ch]``, ``boc_noticeboard.[ch]``, ``boc_cown.h``)
  to give the public ABI a stable, namespaced identity. ``xidata.h``
  has moved under ``include/bocpy/`` alongside ``bocpy.h``.

**Documentation**

- New :doc:`c_abi`, :doc:`messaging`, and :doc:`noticeboard` pages
  in the Sphinx site; the API reference has been expanded to cover
  the public ABI surface.

**Breaking Changes**

- **`noticeboard_version` removed** — the global monotonic version
  counter introduced in 0.4.0 has been removed. It exposed an
  implementation detail of the snapshot cache that did not survive
  the C ABI review and had no use case that was not better served
  by ``notice_sync`` plus an explicit ``noticeboard()`` read.

## 2026-04-29 - Version 0.5.0
Verona-RT-style work-stealing scheduler, C source split into per-subsystem
translation units, and a portable atomics / threading layer.

**New Features**

- **Work-stealing scheduler** — the single behavior queue has been
  replaced with a Verona-RT-inspired distributed scheduler. Each
  worker owns a Multi-Producer Multi-Consumer behavior queue
  (`boc_bq_*`, ported from `verona-rt/src/rt/sched/mpmcq.h`), pops
  work from its own queue first, and steals from peers when empty.
  Idle workers park on a per-worker condition variable and are
  signalled directly by the producer / victim, eliminating the
  central wakeup broadcast. Per-worker statistics (steals, parks,
  fast/slow pops, dispatches) are exposed for benchmarking.
- **Per-worker fairness tokens** — each worker advances a token node
  through its own queue so that long-running behaviors cannot
  monopolise dispatch slots. The token is also used to drive the
  cooperative shutdown handshake.
- **`compat.h` / `compat.c` portability layer** — a single header now
  exposes uniform `BOCMutex`, `BOCCond`, `boc_atomic_*_explicit`,
  monotonic-time, and sleep primitives across MSVC, pthreads, and
  C11 `<threads.h>`. The work-stealing scheduler depends on the
  typed-atomics API for ARM64-correct memory ordering on Windows.
- **`xidata.h` cross-interpreter shim** — the `#if PY_VERSION_HEX`
  ladders for the `_PyXIData_*` / `_PyCrossInterpreterData_*` APIs
  that previously lived in both `_core.c` and `_math.c` have been
  centralised in one header covering CPython 3.12 through 3.15
  (including free-threaded builds).
- **`fanout_benchmark` example** — a fan-out / fan-in benchmark
  harness exercising scheduler throughput under heavy producer
  load.

**Improvements**

- **In-memory transpiled-module loading** — workers no longer write
  the transpiled module to a temporary directory and import it
  through `importlib.util.spec_from_file_location`. Instead, the
  transpiled source is embedded as a string literal in the worker
  bootstrap and `exec`'d into a fresh `types.ModuleType` registered
  in `sys.modules`. The source is also published to `linecache` so
  tracebacks still point at the transpiled lines. This removes the
  `export_dir` argument from `start()` (and the matching tempdir
  cleanup in `wait()`/`stop()`), eliminates a filesystem round-trip
  on every worker startup, and avoids leaving `.py` files behind on
  abnormal exit. Module names are validated as dotted Python
  identifiers at the boundary, and `__main__` is re-aliased to
  `__bocmain__` inside workers so a follow-up `start()` observes a
  clean `sys.modules`.
- **Nested `@when` capture** — the transpiler now recurses into
  `@when`-decorated nested functions when computing the outer
  behavior's captures, so a behavior body can schedule child
  behaviors that close over the outer frame's free names without
  raising `NameError` at dispatch time.
- **C extension split into subsystem TUs** — `_core.c` has been
  reduced from ~5,000 lines to ~3,500 by extracting `sched.{c,h}`
  (work-stealing scheduler), `noticeboard.{c,h}`, `terminator.{c,h}`,
  `tags.{c,h}` (message-queue tag table), `cown.h` (cown refcount
  helpers), and `compat.{c,h}` / `xidata.h` into separate
  translation units. Every public function now has a header
  declaration with Doxygen-style documentation.
- **Direct dispatch on cown release** — `behavior_release_all` now
  hands a resolved successor directly to a worker via the
  work-stealing dispatch path (`boc_sched_dispatch`) instead of
  re-entering the central scheduler, removing one queue hop per
  cown handoff.
- **Cooperative worker shutdown** — `boc_sched_worker_request_stop_all`
  and `boc_sched_unpause_all` provide a clean stop/drain protocol
  that interacts correctly with parked workers and the terminator.

**Internal Test Modules**

- **`_internal_test_atomics`** — pytest-driven correctness tests for
  the `compat.h` typed-atomics API on every supported platform.
- **`_internal_test_bq`** — torture tests for the MPMC behavior
  queue (`boc_bq_*`), covering segmented dequeue, FIFO fairness,
  and concurrent producer / consumer races.
- **`_internal_test_wsq`** — tests for the work-stealing primitives
  (fast pop, slow pop, steal, park / unpark handshake).

**Test Suite**

- New scheduler test files — `test_scheduler_integration.py`,
  `test_scheduler_mpmcq.py`, `test_scheduler_pertask_queue.py`,
  `test_scheduler_stats.py`, `test_scheduler_steal.py`,
  `test_scheduler_wsq.py` — exercise the distributed scheduler end
  to end and per primitive.
- `test_compat_atomics.py` — Python-level smoke tests for the
  portable atomics layer.
- `test_stop_retry_composition.py` — covers `stop()` / `start()` /
  `wait()` retry composition across multiple runtime cycles.
- `test_scheduling_stress.py` substantially expanded with new
  fan-out, work-stealing, and shutdown stress scenarios.
- `test_boc.py` and `test_transpiler.py` extended with regression
  cases discovered during the scheduler rewrite.

## 2026-04-17 - Version 0.4.0
Noticeboard, distributed scheduler, and a relocated examples package.

**New Features**

- **Noticeboard** — a shared key-value store (up to 64 keys) that
  behaviors can read and write without acquiring cowns. Writes
  (`notice_write`, `notice_delete`) are non-blocking; reads
  (`noticeboard`, `notice_read`) return a cached snapshot taken once
  per behavior execution. Atomic read-modify-write is available via
  `notice_update`, which accepts a picklable function and an optional
  default. Returning the `REMOVED` sentinel from the update function
  deletes the entry. Mutations are serialized through a single
  dedicated noticeboard thread so the C-level read-modify-write stays
  consistent without forcing behaviors to take a mutex.
- **`notice_sync`** — new public API that blocks until the caller's
  prior `notice_write` / `notice_update` / `notice_delete` mutations
  have been committed, providing a read-your-writes barrier for code
  that hands work off to a subsequent behavior.
- **`noticeboard_version`** — new public API returning a global,
  monotonic version counter that increments on every successful
  noticeboard commit. Useful as a cheap change-detection hint without
  taking a full snapshot.
- **Distributed scheduler** — the central scheduler thread has been
  removed. Two-phase locking, request linking, and dispatch now run
  in C (`BehaviorCapsule.schedule`) directly on the caller's thread,
  and cown release runs on the worker thread that just executed the
  behavior. Waiters are tracked with an MCS-style intrusive linked
  list per cown, so resolving a behavior hands off straight to the
  next waiter without bouncing through any central queue. The
  C-level terminator is now the only pending counter.
- **`Cown.exception` property** — new boolean property on `Cown` that
  indicates whether the held value is the result of an unhandled
  exception. Workers now call `set_exception` instead of `set_result`
  when a behavior raises.
- **Prime factor example** (`examples/prime_factor.py`, entry point
  `bocpy-prime-factor`) — demonstrates parallel factorisation using
  Pollard's rho algorithm with early termination coordinated via the
  noticeboard.
- **Benchmark harness** (`examples/benchmark.py`, entry point
  `bocpy-bench`) — a new micro-benchmark suite covering scheduling
  throughput, message-queue latency, and noticeboard contention.

**Bug Fixes**

- **Transpiler aliased imports** — `visit_Import` and `visit_ImportFrom`
  now track the alias name (`import X as Y` / `from X import Y as Z`)
  instead of the original name, preventing spurious "name not found"
  errors and duplicate `whencall` injection.
- **Global variable capture** — `@when` closure capture now falls back
  to `frame.f_globals` when a name is not found in any local scope,
  fixing `NameError` for module-level variables used inside behaviors.

**Improvements**

- **C mutex abstraction** — platform-specific mutex and condition-variable
  code (`SRWLock`/`pthread`/C11 `mtx_t`) is now wrapped behind a
  unified `BOCMutex`/`BOCCond` inline API, reducing `#ifdef` clutter
  and simplifying future platform work.
- **Matrix docstrings** — all `Matrix` C methods now carry built-in
  docstrings visible to `help()` and Sphinx autodoc.
- **Worker noticeboard hygiene** — workers clear the per-thread
  noticeboard cache before each behavior and on shutdown, preventing
  stale reads across behaviors.
- **Examples package relocated** — example scripts moved from
  `src/bocpy/examples/` to a top-level `examples/` directory, mapped
  back into the `bocpy.examples` package via
  `[tool.setuptools.package-dir]`. Console-script entry points are
  unchanged.
- **Filtered PyPI README** — `setup.py` now strips
  `<!-- pypi-skip-start -->...<!-- pypi-skip-end -->` regions from
  `README.md` before publishing, so unsupported content (e.g. Mermaid
  diagrams) does not appear as raw text on PyPI. The project metadata
  switches to `dynamic = ["readme"]` to enable this.
- **Documentation refresh** — `README.md`, `sphinx/source/index.rst`,
  and `sphinx/source/api.rst` have been substantially expanded to
  cover the noticeboard, the distributed scheduler model, and the new
  public APIs.
- **New `thinking-in-boc` skill** — guidance for writing BOC code
  without reaching for classical synchronization primitives.

**Tests**

- **`test/test_noticeboard.py`** — new suite covering snapshot
  semantics, `notice_update` atomicity, `REMOVED`, `notice_sync`,
  and version-counter monotonicity.
- **`test/test_scheduling_stress.py`** — new stress suite for the
  distributed scheduler covering 2PL ordering, duplicate-cown
  handling, exception propagation, and high-fan-out workloads.
- **`test/test_transpiler.py`** — new direct tests for AST extraction,
  capture rewriting, aliased imports, and module export.

## 2026-04-02 - Version 0.3.1
CownCapsule serialization support for nested cowns.

**Bug Fixes**

- Removed the ownership check in `_cown_shared` that prevented a
  `CownCapsule` from being serialized to XIData when it was the value
  of another `Cown`. The check was unnecessary — `_cown_shared` only
  stores a pointer and ownership is enforced at acquire time.

**Improvements**

- Added `CownCapsule.__reduce__` with `COWN_INCREF` pinning so that a
  `CownCapsule` embedded in a container (dict, list, etc.) can survive
  the pickle round-trip used by `object_to_xidata`. A module-level
  reconstructor (`_cown_capsule_from_pointer`) inherits the pin without
  a redundant `COWN_INCREF`, and validates the process ID on unpickle to
  guard against cross-process misuse.

## 2026-04-01 - Version 0.3.0
Spin-then-park receive; free-threaded Python compatibility.

**Improvements**

- Added `CownCapsule.disown()` — abandons a cown's value without
  serializing it and resets ownership to `NO_OWNER`. Used during worker
  cleanup to safely discard orphan cowns before the owning interpreter
  is destroyed, preventing dangling Python object references.
- Rewrote `receive` to use a two-phase spin-then-park strategy for
  single-tag untimed receives. Phase 1 spins for `BOC_SPIN_COUNT`
  iterations; Phase 2 parks the thread on a per-queue condvar, eliminating
  busy-wait CPU burn. Timed receives and multi-tag receives use
  spin-then-backoff with exponential sleep (1 µs → 1 ms cap).
- Added platform-abstracted condvar primitives (`BOCParkMutex` /
  `BOCParkCond`) with implementations for Windows (SRWLOCK /
  CONDITION_VARIABLE), macOS (pthreads), and Linux (C11 threads).
- Each `BOCQueue` now carries a `waiters` counter, `park_mutex`, and
  `park_cond`. Producers signal parked receivers after enqueue;
  `drain` and `set_tags` broadcast to wake all parked threads.
- Replaced the fixed `thrd_sleep` in `send` with a `sched_yield` /
  `SwitchToThread`, reducing send-side latency.
- Refactored the monolithic `_core_receive` into `receive_single_tag`
  and `receive_multi_tag`, each with its own backoff/parking logic.
- Moved the `BOC_QUEUE_DISABLED` check earlier in `get_queue_for_tag`
  so callers skip disabled queues instead of returning NULL after
  tag resolution.
- Added Windows-compatible `atomic_load_explicit` /
  `atomic_fetch_add_explicit` / `atomic_fetch_sub_explicit` macros
  using `InterlockedExchangeAdd64`.
- Declared `Py_mod_gil = Py_MOD_GIL_NOT_USED` in both `_core` and
  `_math` C extensions so that importing bocpy on a free-threaded
  Python build (3.13t+) does not re-enable the GIL.
- Replaced `PyDict_GetItem` (borrowed reference) with
  `PyDict_GetItemRef` (strong reference) in `BOCRecycleQueue_recycle`
  on Python 3.13+, improving forward-compatibility with free-threaded
  builds.

**Bug Fixes**

- Fixed a deadlock when the same cown is passed multiple times to `@when`
  (e.g. `@when(c, c)`). Duplicate requests for the same cown caused the
  MCS-queue-based two-phase locking to spin-wait on itself. Requests are
  now deduplicated by target cown in `Behavior.__init__`, with
  compensating `resolve_one` calls to maintain the behavior count
  invariant.

**Tests**

- `TestLostWakeStress`: single-producer random delays, bursty producer,
  and repeated single-message wake to detect lost-wake races.
- `TestMultiTagBackoff`: multi-tag receive correctness — second-tag hit,
  delayed arrival, per-tag FIFO ordering, timeout, and interleaved
  producers.
- `TestTimeoutAccuracy`: lower-bound / upper-bound wall-clock checks and
  zero-timeout immediacy.
- Added tests for duplicate cowns in `@when`: same cown twice, thrice,
  non-adjacent duplicates, duplicates within a group, and mutation
  aliasing semantics.

**CI**

- Added a `free-threaded` CI job that tests against Python 3.13t and
  3.14t on Linux, with explicit assertions that the GIL remains disabled
  after import.

## 2026-03-17 - Version 0.2.2
Point release.

**Improvements**
- Added an ASAN/UBSAN CI job that builds CPython 3.14.2 from source with
  AddressSanitizer and UndefinedBehaviorSanitizer, then runs the full test suite
  against instrumented builds of bocpy.
- Updated GitHub Actions to latest versions (`actions/checkout@v6`,
  `actions/setup-python@v5`).
- Added a Copilot skill for version bumping.

**Bug Fixes**
- Fixed a missing `Py_DECREF` on a temporary `PyObject` in the xidata recycling
  path, plugging a reference leak.
- Fixed `PyMem_RawFree` freeing the wrong pointer (`xidata->obj` instead of
  `xidata`) in the recycling queue cleanup.

## 2026-03-11 - Version 0.2.1
Point release.

**Improvements**
- Adding a repository-level copilot-instructions file
- Properly added the skills files as copilot agent skills

**Bug Fixes**
- Fixed a false positive warning message for deallocation of xidata on the main
  interpreter after module shutdown.
- Changed the clear logic when recycling

## 2026-03-04 - Version 0.2.0
Bugfix release including some minor improvements.

**Improvements**
- Examples are now included in the package, with script entrypoints for each.
- The `drain` low-level API function is now exposed at the package level
- `wait()` will now acquire frame-local `Cown` objects before shutting down the workers

**Dev Tools**
- Added an internal cown and behavior reference tracking utility

**Bug Fixes**
- Fixed a reference counting bug with cown lists
- Fixed an issue where the boids example did not run on windows due a font
  setting.


## 2026-03-02 - Version 0.1.0
Initial Release.