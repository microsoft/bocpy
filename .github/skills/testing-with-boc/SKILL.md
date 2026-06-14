---
name: testing-with-boc
description: "Write tests for bocpy Behavior-Oriented Concurrency code. Use when: writing pytest tests for @when behaviors, Cown scheduling, send/receive messaging, cown grouping, chained behaviors, exception propagation. Covers parameter-count rules, module-level class requirements, and the quiesce()+unwrap() result-reading pattern."
---

# Testing with Behavior-Oriented Concurrency (BOC)

This skill describes how to write tests for code that uses the `bocpy` library for
behavior-oriented concurrency. BOC schedules work as **behaviors** — decorated
functions that run once all required **cowns** (concurrently-owned data) are
available. Testing BOC programs requires specific patterns because behaviors
execute asynchronously on worker interpreters.

> **Design guidance:** if you find yourself reaching for `time.sleep`,
> `threading.Event`, polling loops, or `wait_for_*` helpers inside a
> behavior or in test code that drives one, stop and read
> `thinking-in-boc` first. The right answer is almost always to express
> the dependency through the cown graph, not through a classical
> synchronization primitive.

## Key Concepts

| Concept | Description |
|---------|-------------|
| `Cown(value)` | A concurrently-owned wrapper. Behaviors receive exclusive temporal access to the cown's `.value`. |
| `@when(*cowns)` | Decorator that schedules the function as a behavior. The decorator replaces the function with a `Cown` holding the return value. The first N parameters bind to the N cowns; every trailing parameter **must carry a default**, whose value is snapshotted as a capture at schedule time (see below). |
| `quiesce(timeout=None)` | Blocks until all in-flight behaviors complete **without** tearing down the workers. The preferred test-thread barrier before reading results. |
| `Cown.unwrap()` | After quiescence, returns the behavior's result value, or **re-raises** the exception the behavior captured (verbatim, on the test thread). Raises `RuntimeError` if called while behaviors are still in flight. |
| `send(tag, contents)` | Sends a cross-interpreter message with the given tag. |
| `receive(tags, timeout)` | Blocks until a message with a matching tag arrives (or times out). Returns `(TIMEOUT, None)` on timeout. |
| `TIMEOUT` | Sentinel string returned as the tag by `receive` when a timeout elapses. |
| `wait(timeout)` | Blocks until all scheduled behaviors have completed, then tears the runtime down. |

### Cown count, parameter count, and captures

The first `N` parameters of the decorated function bind positionally to the
`N` arguments of `@when`. **Every additional trailing parameter must carry a
default value**; that default is evaluated when the `def` runs and
snapshotted as the capture at schedule time:

* `def b(c, factor=factor)` — captures `factor` (read from the enclosing
  scope at schedule time).
* `def b(c, i=i)` — captures `i` (the canonical loop-snapshot idiom).
* `def b(c, x=y)` — captures `y` and binds it into param `x`.

The runtime enforces this at **decoration time**:

* A **bare** extra parameter (`def b(c, factor)` — no default) raises
  `TypeError`: the capture count must equal the trailing-parameter count.
* A **closure** over an enclosing-*function* local raises `SyntaxError`
  — a behavior runs in another interpreter and cannot capture by closure.
  The error names the variable and suggests the `x=x` fix. (A plain
  **module-level global** is *not* a closure: the bindings reducer keeps
  module-level assignments, so a behavior may read them directly. The
  closure rule only bites names bound in an enclosing function — the
  common case when a behavior is written inside a test method.)
* `async def` / generator behaviors raise `SyntaxError`.

Computed defaults (`def b(c, k=expensive())`) are allowed — the value is
evaluated once and snapshotted like any other capture. A default on a cown
position (`def b(c=c)` under `@when(x)`) is just a capture-count mismatch and
raises `TypeError`.

```python
# CORRECT — 1 @when arg, 1 function param
@when(x)
def good(x):
    return x.value * 2

# CORRECT — extra params beyond the cown count are captures and must
# carry a default. The default is snapshotted at schedule time.
def schedule(x):
    factor = 2
    @when(x)
    def with_extra(x, factor=factor):   # capture ``factor`` via default
        return x.value * factor

# WRONG — bare extra parameter (no default) raises TypeError
@when(x)
def missing_default(x, factor):         # TypeError at decoration
    return x.value * factor

# WRONG — closing over an enclosing-function local raises SyntaxError
def schedule_bad(x):
    factor = 2
    @when(x)
    def via_closure(x):                  # SyntaxError: closes over 'factor'
        return x.value * factor          # pass it as ``factor=factor`` instead
```

### The `def _(c, i=i)` loop-capture idiom is supported

The canonical Python idiom for snapshotting a loop variable as a default
argument works transparently:

```python
for i, c in enumerate(cowns):
    @when(c)
    def _(c, i=i):                      # ``i`` captured at schedule time
        send("done", i)
```

Parameters beyond the cown count are captures, snapshotted at schedule time.
A capture is a trailing parameter carrying a default whose value is read when
the behavior is scheduled: `def b(c, i=i)` captures `i` (the rename form
`def b(c, x=y)` captures `y`). A bare extra parameter (`def b(c, factor)`)
raises `TypeError` at decoration time, and a closure over a free variable
raises `SyntaxError` — a behavior runs in another interpreter and cannot
capture by closure.

A behavior **cannot** reference a loop variable by closure; you must capture
it explicitly:

```python
for i, c in enumerate(cowns):
    @when(c)
    def _(c, i=i):                      # capture i; bare `i` reference would fail
        send("done", i)
```

See the "Inspecting the Worker Bindings Module" section of
`.github/copilot-instructions.md` for how `@when` captures work and how to
inspect the bindings module workers import.

If you want a fresh scope per iteration (e.g. to avoid sharing mutable state
between iterations), use a helper function:

```python
def _schedule(c, i):                    # fresh scope per iteration
    @when(c)
    def _(c):
        send("done", i)

for i, c in enumerate(cowns):
    _schedule(c, i)
```

### Critical rule: classes and functions must be declared at module level

Behaviors run in separate sub-interpreters. Workers import the module's
bindings, which means **any class or function referenced inside a
`@when` behavior must be defined at module level**. A class defined inside a test
method or local function cannot be resolved by the worker and will crash.

```python
# CORRECT — class at module level
class Accumulator:
    def __init__(self):
        self.items = []
    def add(self, item):
        self.items.append(item)

def test_accumulator(self):
    acc = Cown(Accumulator())

    @when(acc)
    def _(a):
        a.value.add(42)       # Accumulator is importable ✓

# WRONG — class inside test method
def test_accumulator_bad(self):
    class Accumulator:        # local class — worker can't import it
        ...
    acc = Cown(Accumulator())

    @when(acc)
    def _(a):
        a.value.add(42)       # will crash
```

## Project Test Setup

- Tests use **pytest** and live in the `test/` directory.
- Install test dependencies: `pip install -e .[test]`
- Run: `pytest -vv`

## Pattern 1 — `quiesce()` + `unwrap()` (default)

This is the preferred pattern for **result-shipping** assertions: the behavior
computes a value (or raises), and the test thread verifies it. A behavior
returns its result, so `@when` hands back a `Cown` holding that result. The
test blocks once on `quiesce()` (which lets every in-flight behavior finish
without tearing down the workers), then reads the result with
`Cown.unwrap()`.

`unwrap()` returns the stored value on success, and **re-raises** any exception
the behavior captured — verbatim, on the test thread — so a failing assertion
inside a behavior surfaces as a real `AssertionError` in the test. It also
guards against misuse: calling it while behaviors are still in flight raises
`RuntimeError`, so always `quiesce()` first.

Always pass a **timeout** to `quiesce()`. If the barrier is not reached in time
(e.g. a behavior never fires because of a `@when` arg-count mismatch) it raises
`TimeoutError`, so the test fails fast instead of hanging forever. Use a
module-level constant such as `QUIESCE_TIMEOUT = 5`.

```python
from bocpy import Cown, when, quiesce, wait

QUIESCE_TIMEOUT = 5  # seconds; quiesce() raises TimeoutError if exceeded


class TestExample:
    @classmethod
    def teardown_class(cls):
        wait()  # tear the runtime down after all tests

    def test_double(self):
        x = Cown(3)

        @when(x)
        def result(x):
            return x.value * 2

        quiesce(QUIESCE_TIMEOUT)
        assert result.unwrap() == 6
```

Tuple results read just as naturally:

```python
    def test_pair(self):
        v = Cown(Matrix(1, 2, [3.0, 4.0]))

        @when(v)
        def result(v):
            n = v.value.normalize()
            return (n[0, 0], n[0, 1])

        quiesce(QUIESCE_TIMEOUT)
        n0, n1 = result.unwrap()
        assert n0 == pytest.approx(0.6)
        assert n1 == pytest.approx(0.8)
```

### Asserting a behavior raised

Because `unwrap()` re-raises the captured exception, use
`pytest.raises` to assert that a behavior failed:

```python
    def test_raises(self):
        x = Cown(1)

        @when(x)
        def boom(x):
            raise ValueError("bad input")

        quiesce(QUIESCE_TIMEOUT)
        with pytest.raises(ValueError, match="bad input"):
            boom.unwrap()
```

Once `unwrap()` consumes an exception it clears the cown's exception flag, so
the runtime will not later report it as unhandled, and a second `unwrap()`
returns the (now `None`) value rather than re-raising.

### Why this pattern?

`@when` returns immediately — the behavior has not executed yet. `quiesce()` is
the test-thread barrier that lets it (and any behaviors it chains) finish.
Unlike `wait()`, it leaves the workers alive, so the result cowns remain
readable and further behaviors can still be scheduled. Reading results
directly avoids the message-queue round-trip and per-assert timeout polling
that a `send`/`receive`-based assertion would require.

## Pattern 2 — Testing Nested / Chained Behaviors

Behaviors can schedule further behaviors. A behavior that chains more work can
return the inner result cown; after `quiesce()` the whole chain has run, so you
unwrap the final result on the test thread.

```python
def test_nested(self):
    x = Cown(1)

    @when(x)
    def step1(x):
        x.value *= 2          # x is now 2

        @when(x)
        def step2(x):
            x.value *= 3      # x is now 6
            return x.value

        return step2

    quiesce(QUIESCE_TIMEOUT)
    # step1 returns the inner step2 cown; unwrap it to read the final value.
    assert step1.unwrap().unwrap() == 6
```

## Pattern 3 — Multi-Cown Coordination

Pass multiple cowns to `@when` to atomically operate on several pieces of data.
The scheduler guarantees deadlock-free acquisition.

```python
def test_transfer(self):
    x = Cown(100)
    y = Cown(0)

    @when(x, y)
    def _(x, y):
        y.value += 50
        x.value -= 50

    @when(x)
    def read_x(x):
        return x.value

    @when(y)
    def read_y(y):
        return y.value

    quiesce(QUIESCE_TIMEOUT)
    assert read_x.unwrap() == 50
    assert read_y.unwrap() == 50
```

## Pattern 4 — Cown Grouping

When you have a dynamic number of cowns (e.g., a list), you can pass them to
`@when` as a **list** (or slice) rather than individual arguments. Inside the
behavior, that parameter is delivered as a `list[Cown]` — each element is an
acquired cown whose `.value` you can read or write.

You can mix single cowns and groups freely in any order. Each distinct argument
to `@when` becomes its own parameter in the decorated function:

| `@when(...)` arguments | Behavior parameters |
|------------------------|---------------------|
| `@when(list_of_cowns)` | `(group: list[Cown])` |
| `@when(cowns[:9], cowns[9])` | `(group: list[Cown], single: Cown)` |
| `@when(cowns[0], cowns[1:])` | `(single: Cown, group: list[Cown])` |
| `@when(cowns[:4], cowns[4], cowns[5:])` | `(g0: list[Cown], single: Cown, g1: list[Cown])` |
| `@when(cowns[0], cowns[1:9], cowns[9])` | `(s0: Cown, group: list[Cown], s1: Cown)` |

### Full group example

```python
from bocpy import Cown, when, quiesce

cowns = [Cown(i) for i in range(10)]  # values 0..9, sum = 45

# All cowns as a single group
@when(cowns)
def group_sum(group: list[Cown[int]]):
    return sum(c.value for c in group)

# Group + single cown
@when(cowns[:9], cowns[9])
def group_then_single(group: list[Cown[int]], single: Cown[int]):
    return sum(c.value for c in group) + single.value

# Single cown + group
@when(cowns[0], cowns[1:])
def single_then_group(single: Cown[int], group: list[Cown[int]]):
    return single.value + sum(c.value for c in group)

# Group + single + group
@when(cowns[:4], cowns[4], cowns[5:])
def group_single_group(g0: list[Cown[int]], single: Cown[int], g1: list[Cown[int]]):
    return sum(c.value for c in g0) + single.value + sum(c.value for c in g1)
```

### Testing grouped results

The results are all cowns, so collect them after `quiesce()` with `unwrap()`.
You can also pass a list of result cowns as a group to a follow-up `@when`:

```python
def test_cown_grouping(self):
    expected = 45
    results = [group_sum, group_then_single, single_then_group, group_single_group]

    quiesce(QUIESCE_TIMEOUT)
    for r in results:
        assert r.unwrap() == expected
```

### Key rules for grouping

- Pass a **list** (or slice) of cowns to `@when` — the behavior receives the
  corresponding parameter as `list[Cown]`.
- Pass a **single cown** — the parameter receives that `Cown` directly.
- You can **interleave** singles and groups in any order. The positional mapping
  between `@when(...)` arguments and the decorated function's parameters is 1:1.
- Type-annotate grouped parameters as `list[Cown[T]]` for clarity.

## Pattern 5 — Exception Propagation

If a behavior raises, the exception is captured in the returned cown's `.value`
**and** the cown's `.exception` flag is set to `True`. The simplest way to
assert a behavior raised is `unwrap()` under `pytest.raises`, which re-raises
the captured exception verbatim on the test thread:

```python
def test_exception_in_behavior(self):
    x = Cown(1)

    @when(x)
    def bad(x):
        x.value /= 0          # ZeroDivisionError

    quiesce(QUIESCE_TIMEOUT)
    with pytest.raises(ZeroDivisionError):
        bad.unwrap()
```

An `Exception` object a behavior **returns** (rather than raises) is just a
value: `.exception` stays `False` and `unwrap()` returns it normally.

```python
def test_returned_exception_is_not_flagged(self):
    """An Exception object *returned* from a behavior is just a value."""
    x = Cown(1)

    @when(x)
    def returns_exc(x):
        return ValueError("not really an error")

    quiesce(QUIESCE_TIMEOUT)
    result = returns_exc.unwrap()
    assert isinstance(result, ValueError)
```

If you need to inspect the flag from **inside** a downstream behavior (rather
than unwrap on the test thread), the same rules apply:

- `cown.exception` distinguishes a thrown exception from a returned
  `Exception` value — assert on it before `isinstance(.value, Exception)`.
- Writing `cown.value = ...` from inside a behavior **clears** `.exception`.
- `cown.exception` is also writable inside a behavior, to manually mark or
  unmark a cown as carrying an error.

## Pattern 6 — Noticeboard

The noticeboard is a global key-value store (up to 64 keys) that behaviors can
read and write **without** acquiring any cowns. Writes are non-blocking; reads
return a snapshot taken once per behavior execution.

| Function | Purpose |
|----------|---------|
| `notice_write(key, value)` | Non-blocking write. |
| `notice_update(key, fn, default=None)` | Atomic read-modify-write. `fn` and `default` must be picklable. Returning `REMOVED` deletes the entry. |
| `notice_delete(key)` | Non-blocking delete. |
| `noticeboard()` | Read-only mapping — snapshot of the noticeboard, cached for the duration of the current behavior. |
| `notice_read(key, default=None)` | Convenience: one key from the snapshot. |

### Key rule: snapshot per behavior

Within a single behavior, `noticeboard()` and `notice_read()` always return
data from the **same** snapshot — even if other behaviors write in the
meantime. To see a write made by another behavior, schedule a follow-up
behavior (typically by chaining via a cown returned from `@when`).

Because the snapshot can only be read from **inside** a behavior, store the
read into the result cown returned by `@when` and `unwrap()` it on the test
thread after `quiesce()` (Pattern 1). The result cown is used only by that one
behavior, so it does not affect scheduling.

```python
def test_noticeboard_roundtrip(self):
    x = Cown(0)

    @when(x)
    def step1(x):
        notice_write("greeting", "hello")

    # The chain on `step1` ensures step2 runs *after* the write has been
    # applied and step2's snapshot sees it.
    @when(x, step1)
    def step2(x, _):
        return notice_read("greeting")

    quiesce(QUIESCE_TIMEOUT)
    assert step2.unwrap() == "hello"
```

### Atomic update

`notice_update` runs `fn(current_value)` on the scheduler and writes the
result back atomically. Lambdas and closures are **not** picklable — use a
module-level function (optionally wrapped with `functools.partial`) or an
`operator` function.

```python
from functools import partial
from operator import add

def _bump(n, by):
    return n + by

class TestCounter:
    @classmethod
    def teardown_class(cls):
        wait()

    def test_atomic_increment(self):
        x = Cown(0)

        @when(x)
        def init(x):
            notice_write("count", 0)

        @when(x, init)
        def bump(x, _):
            notice_update("count", partial(_bump, by=5))
            notice_update("count", partial(add, 3))

        @when(x, bump)
        def check(x, _):
            return notice_read("count")

        quiesce(QUIESCE_TIMEOUT)
        assert check.unwrap() == 8
```

### Delete via `REMOVED`

Returning the `REMOVED` sentinel from a `notice_update` callback deletes the
entry. `notice_delete(key)` is the direct form.

```python
def _drop_if_zero(n):
    return REMOVED if n == 0 else n - 1

def test_remove_via_update(self):
    x = Cown(0)

    @when(x)
    def init(x):
        notice_write("lives", 1)

    @when(x, init)
    def tick(x, _):
        notice_update("lives", _drop_if_zero)   # 1 -> 0
        notice_update("lives", _drop_if_zero)   # 0 -> REMOVED

    @when(x, tick)
    def check(x, _):
        return "lives" in noticeboard()

    quiesce(QUIESCE_TIMEOUT)
    assert check.unwrap() is False
```

### Common noticeboard pitfalls

| Pitfall | Fix |
|---------|-----|
| Reading a value back inside the **same** behavior that wrote it | The snapshot was taken at the start of the behavior. Chain a follow-up `@when` to observe the write. |
| Passing a lambda or closure to `notice_update` | They are not picklable. Use a module-level function with `functools.partial`, or an `operator` function. |
| Asserting in the test body that `noticeboard()` contains a key | Read inside a behavior, return the result, then `quiesce()` and `unwrap()` it — `noticeboard()` and `notice_read()` outside any behavior return a snapshot that is never refreshed. |
| Writing more than 64 distinct keys | Excess writes are dropped with a logged warning — they do **not** raise. Keep tests within the limit (and `notice_delete` keys you no longer need). |

## Pattern 7 — Parameterized Tests

Use `@pytest.mark.parametrize` to sweep inputs. Each invocation gets its own
cowns so tests are isolated.

```python
@pytest.mark.parametrize("n", [1, 10, 15])
def test_fibonacci(self, n):
    result = fib_parallel(n)
    expected = fib_sequential(n)

    quiesce(QUIESCE_TIMEOUT)
    assert result.unwrap() == expected
```

## Pattern 8 — Testing `send`/`receive` Messaging Directly

For code that uses the lower-level messaging API without behaviors:

```python
from bocpy import send, receive, TIMEOUT

def test_basic_messaging():
    send("tag", "payload")
    tag, value = receive("tag", 1)
    assert tag != TIMEOUT
    assert value == "payload"

def test_receive_timeout():
    tag, value = receive("tag", 0.1)
    assert tag == TIMEOUT
    assert value is None

def test_timeout_with_after_callback():
    tag, value = receive("tag", 0.1, lambda: ("fallback", 42))
    assert tag == "fallback"
    assert value == 42
```

## Pattern 9 — Complex Objects in Cowns

Mutable objects (e.g., class instances) work inside cowns. Behaviors mutate them
in-place under exclusive access.

```python
class Counter:
    def __init__(self):
        self.n = 0
    def increment(self):
        self.n += 1

def test_object_in_cown(self):
    c = Cown(Counter())

    for _ in range(10):
        @when(c)
        def _(c):
            c.value.increment()

    @when(c)
    def final(c):
        return c.value.n

    quiesce(QUIESCE_TIMEOUT)
    assert final.unwrap() == 10
```

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| **Parameter count mismatch in `@when`** | The decorated function must have exactly as many cown parameters as `@when` arguments; any further parameters must be captures (trailing parameters with defaults). A mismatch raises `TypeError` at decoration. |
| **Classes/functions defined inside a test** | Behaviors run in sub-interpreters that import the module. Define all classes and functions used in behaviors at **module level** so workers can resolve them. |
| Asserting in the test body right after `@when` | The behavior hasn't run yet. Call `quiesce()` first, then read results with `unwrap()` (Pattern 1). |
| `receive` without a timeout | If a behavior crashes silently, the test hangs forever. Always pass a timeout (e.g. `RECEIVE_TIMEOUT = 10`) and assert the result is not `TIMEOUT`. |
| Forgetting `wait()` in teardown | Pending behaviors may leak into the next test class. Always call `wait()` in `teardown_class`. |
| Reading `cown.value` outside a behavior | A cown must be acquired first. After `quiesce()`, use `unwrap()` (which acquires for you); otherwise read inside `@when`. |
| Trying to capture a loop variable by closure | A behavior runs in another interpreter and cannot close over free variables (raises `SyntaxError`). Capture it as a trailing default instead: `def _(c, i=i): ...`. |
| Non-XIData-compatible objects in cowns across interpreters | Stick to built-in types or objects that support cross-interpreter data. |
| Importing `unittest.mock` in a BOC test | Worker sub-interpreters import the test module's bindings. `unittest.mock` transitively imports `asyncio`, which can deadlock during PEP 684 per-interpreter init (observed on macOS arm64 + Python 3.12/3.13). Use the in-house `mockreplacement.patch_attr` / `Recorder` helpers (see `test/mockreplacement.py`), and import them **inside the test method** — never at module scope, because workers also fail to find `mockreplacement` on their `sys.path` during bootstrap. |
| Test function names with uppercase letters (N802) | Test names must be lowercase. E.g., `test_t_equals_transpose`, **not** `test_T_equals_transpose`, even when testing a property like `.T`. |
| Assigning `Cown(m)` to an unused variable (F841) | When the return value isn't needed (e.g., releasing a resource), use bare `Cown(m)` without assignment. |
| Using single quotes (Q000) | The project enforces `inline-quotes = double`. Use `"nan"` not `'nan'`. |
| Multi-line class docstring formatting (D205/D209) | Summary line, then blank line, then body. Closing `"""` on its own line. |
| Placing `# noqa: B023` on the `def` line instead of the violation line | `# noqa: B023` must go on the line that **references** the loop variable, not the `def _(a):` line above it. |
