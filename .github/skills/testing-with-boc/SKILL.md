---
name: testing-with-boc
description: "Write tests for bocpy Behavior-Oriented Concurrency code. Use when: writing pytest tests for @when behaviors, Cown scheduling, send/receive messaging, cown grouping, chained behaviors, exception propagation. Covers parameter-count rules, module-level class requirements, and the send/receive assertion pattern."
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
| `@when(*cowns)` | Decorator that schedules the function as a behavior. The decorator replaces the function with a `Cown` holding the return value. The first N parameters bind to the N cowns; any trailing parameters are auto-captured from the caller's frame (see below). |
| `send(tag, contents)` | Sends a cross-interpreter message with the given tag. |
| `receive(tags, timeout)` | Blocks until a message with a matching tag arrives (or times out). Returns `(TIMEOUT, None)` on timeout. |
| `TIMEOUT` | Sentinel string returned as the tag by `receive` when a timeout elapses. |
| `wait(timeout)` | Blocks until all scheduled behaviors have completed. |

### Cown count, parameter count, and auto-captured extras

The first `N` parameters of the decorated function bind positionally to the
`N` arguments of `@when`. Any **additional** trailing parameters are treated
as captures of names from the caller's scope:

* `def b(c, factor)` — captures `factor` by the parameter's own name.
* `def b(c, i=i)` — captures `i` (the canonical loop-snapshot idiom).
* `def b(c, x=y)` — captures `y` and binds it into param `x`.

Defaults must be plain names; computed defaults (`def b(c, k=foo())`) and
defaults on cown positions (`def b(c=c)`) raise `SyntaxError` at export
time. Free variables referenced in the body (but not in the signature) are
also auto-captured, so the simplest spelling is usually to omit the extras
entirely and just reference them in the body.

```python
# CORRECT — 1 @when arg, 1 function param
@when(x)
def good(x):
    return x.value * 2

# ALSO CORRECT — extra params beyond the cown count are auto-captured
# from the caller's frame by name. Plain extras use the param's own
# name; defaults of the form ``i=i`` or ``x=y`` use the default's name.
factor = 2
@when(x)
def with_extra(x, factor):              # ``factor`` captured by name
    return x.value * factor

# FIX — for older code: capture extra values via closure
factor = 2
@when(x)
def fixed(x):                           # 1 param matches 1 @when arg
    return x.value * factor             # factor captured from enclosing scope
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

The transpiler hoists positional parameters beyond the cown count into
captures: bare extras (`def b(c, factor)`) capture by the parameter's own
name; defaults (`def b(c, i=i)` or the rename form `def b(c, x=y)`) capture by
the default expression's name. The default expression must be a plain
`Name`; computed defaults (`def b(c, k=foo())`) and defaults on cown
positions (`def b(c=c)`) raise `SyntaxError` at export time.

Because the transpiler already snapshots loop variables into a tuple at
schedule time, you can also just reference the loop variable directly without
the `i=i` idiom — both spellings work:

```python
for i, c in enumerate(cowns):
    @when(c)
    def _(c):
        send("done", i)                 # i is captured by value at schedule time
```

See the "Inspecting Transpiler Output" section of
`.github/copilot-instructions.md` for how to use `export_module.py` to confirm
exactly which names are parameters and which are captures.

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

Behaviors run in separate sub-interpreters. The transpiler exports the module so
workers can import it, which means **any class or function referenced inside a
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

## Pattern 1 — Assert Inside a Behavior via `send`/`receive`

Because behaviors run asynchronously, you **cannot** assert directly in the test
body after scheduling a behavior. Instead, use `send` to ship the result out of
the behavior and `receive` in the test to collect and verify it.

```python
from bocpy import Cown, when, send, receive, drain, TIMEOUT, wait

RECEIVE_TIMEOUT = 10

class TestExample:
    @classmethod
    def teardown_class(cls):
        wait()  # drain the scheduler after all tests

    def receive_asserts(self, count=1):
        """Helper: collect `count` assertion messages and fail on mismatch.

        Uses a timeout so that if a behavior never fires (e.g. due to a
        parameter-count mismatch in @when) the test fails quickly instead
        of hanging forever. The "assert" queue is always drained before
        returning so leftover messages from a failing test do not leak
        into subsequent tests in CI.
        """
        failed = None
        timed_out = False
        try:
            for _ in range(count):
                result = receive("assert", RECEIVE_TIMEOUT)
                if result[0] == TIMEOUT:
                    timed_out = True
                    break
                _, (actual, expected) = result
                if failed is None and actual != expected:
                    failed = (actual, expected)
        finally:
            drain("assert")

        assert not timed_out, (
            "Timed out waiting for an 'assert' message from a behavior. "
            "Check that every @when arg count matches the decorated "
            "function's parameter count."
        )
        if failed is not None:
            actual, expected = failed
            assert actual == expected, f"expected {expected!r}, got {actual!r}"

    def test_double(self):
        x = Cown(3)

        @when(x)
        def result(x):
            return x.value * 2

        @when(result)
        def _(r):
            send("assert", (r.value, 6))

        self.receive_asserts()
```

### Why this pattern?

`@when` returns immediately — the behavior hasn't executed yet. The test thread
must block on `receive("assert")` to synchronize with the behavior's completion.
Calling `wait()` in `teardown_class` ensures any remaining work finishes before
the next test class starts.

## Pattern 2 — Testing Nested / Chained Behaviors

Behaviors can schedule further behaviors. Use multiple `send` calls and
`receive_asserts(count)` to verify each step in the chain.

```python
def test_nested(self):
    x = Cown(1)

    @when(x)
    def step1(x):
        x.value *= 2          # x is now 2

        @when(x)
        def step2(x):
            x.value *= 3      # x is now 6

        return step2

    @when(x, step1)
    def check(x, s):
        send("assert", (x.value, 2))

        @when(x, s.value)     # s.value is the inner Cown
        def deep_check(x, _):
            send("assert", (x.value, 6))

    self.receive_asserts(2)
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
    def _(x):
        send("assert", (x.value, 50))

    @when(y)
    def _(y):
        send("assert", (y.value, 50))

    self.receive_asserts(2)
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
from bocpy import Cown, when, send, receive

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

The results are all cowns, so use the same `send`/`receive` pattern. You can
itself pass a list of result cowns as a group to `@when`:

```python
def test_cown_grouping(self):
    expected = 45
    results = [group_sum, group_then_single, single_then_group, group_single_group]

    @when(results)
    def check(results: list[Cown]):
        for r in results:
            send("assert", (r.value, expected))

    self.receive_asserts(len(results))
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
**and** the cown's `.exception` flag is set to `True`. This lets downstream
behaviors distinguish a thrown exception from a value that just happens to be
an `Exception` instance returned normally.

```python
def test_exception_in_behavior(self):
    x = Cown(1)

    @when(x)
    def bad(x):
        x.value /= 0          # ZeroDivisionError

    @when(bad)
    def _(b):
        send("assert", (b.exception, True))
        send("assert", (isinstance(b.value, ZeroDivisionError), True))
        b.value = None         # writing .value clears the exception flag

    self.receive_asserts(2)


def test_returned_exception_is_not_flagged(self):
    """An Exception object *returned* from a behavior is just a value."""
    x = Cown(1)

    @when(x)
    def returns_exc(x):
        return ValueError("not really an error")

    @when(returns_exc)
    def _(r):
        send("assert", (r.exception, False))
        send("assert", (isinstance(r.value, ValueError), True))

    self.receive_asserts(2)
```

Notes:

- Writing `cown.value = ...` from inside a behavior **clears** `.exception`.
- `cown.exception` is also writable inside a behavior, in case you want to
  manually mark or unmark a cown as carrying an error.
- Always assert on `.exception` before `isinstance(.value, Exception)` —
  otherwise a behavior that legitimately returns an `Exception` will be
  indistinguishable from one that raised.

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
        send("assert", (notice_read("greeting"), "hello"))

    self.receive_asserts()
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
            send("assert", (notice_read("count"), 8))

        receive_asserts()
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
        send("assert", ("lives" in noticeboard(), False))

    self.receive_asserts()
```

### Common noticeboard pitfalls

| Pitfall | Fix |
|---------|-----|
| Reading a value back inside the **same** behavior that wrote it | The snapshot was taken at the start of the behavior. Chain a follow-up `@when` to observe the write. |
| Passing a lambda or closure to `notice_update` | They are not picklable. Use a module-level function with `functools.partial`, or an `operator` function. |
| Asserting in the test body that `noticeboard()` contains a key | Read inside a behavior and `send` the result out — `noticeboard()` and `notice_read()` outside any behavior return a snapshot that is never refreshed. |
| Writing more than 64 distinct keys | Excess writes are dropped with a logged warning — they do **not** raise. Keep tests within the limit (and `notice_delete` keys you no longer need). |

## Pattern 7 — Parameterized Tests

Use `@pytest.mark.parametrize` to sweep inputs. Each invocation gets its own
cowns so tests are isolated.

```python
@pytest.mark.parametrize("n", [1, 10, 15])
def test_fibonacci(self, n):
    result = fib_parallel(n)
    expected = fib_sequential(n)

    @when(result)
    def _(r):
        send("assert", (r.value, expected))

    self.receive_asserts()
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
    def _(c):
        send("assert", (c.value.n, 10))

    self.receive_asserts()
```

## Common Pitfalls

| Pitfall | Fix |
|---------|-----|
| **Parameter count mismatch in `@when`** | The decorated function must have **exactly** as many parameters as `@when` arguments. A mismatch crashes the worker. Use closure variables instead of default arguments to capture extra values. |
| **Classes/functions defined inside a test** | Behaviors run in sub-interpreters that import the module. Define all classes and functions used in behaviors at **module level** so workers can resolve them. |
| Asserting in the test body right after `@when` | The behavior hasn't run yet. Use `send`/`receive` to synchronize. |
| `receive` without a timeout | If a behavior crashes silently, the test hangs forever. Always pass a timeout (e.g. `RECEIVE_TIMEOUT = 10`) and assert the result is not `TIMEOUT`. |
| Forgetting `wait()` in teardown | Pending behaviors may leak into the next test class. Always call `wait()` in `teardown_class`. |
| Reading `cown.value` outside a behavior | A cown must be acquired first. Read values inside `@when` or use `send`/`receive`. |
| Using default arguments to capture loop variables | Default args add parameters, breaking the arg-count rule. Use a closure variable instead: `val = i` on a separate line before `@when`. |
| Mismatched `receive_asserts` count | The count must match the exact number of `send("assert", ...)` calls expected. |
| Non-XIData-compatible objects in cowns across interpreters | Stick to built-in types or objects that support cross-interpreter data. |
| Importing `unittest.mock` in a BOC test | The transpiler exports the whole test module for import in every worker sub-interpreter. `unittest.mock` transitively imports `asyncio`, which can deadlock during PEP 684 per-interpreter init (observed on macOS arm64 + Python 3.12/3.13). Use the in-house `mockreplacement.patch_attr` / `Recorder` helpers (see `test/mockreplacement.py`), and import them **inside the test method** — never at module scope, because workers also fail to find `mockreplacement` on their `sys.path` during bootstrap. |
| Test function names with uppercase letters (N802) | Test names must be lowercase. E.g., `test_t_equals_transpose`, **not** `test_T_equals_transpose`, even when testing a property like `.T`. |
| Assigning `Cown(m)` to an unused variable (F841) | When the return value isn't needed (e.g., releasing a resource), use bare `Cown(m)` without assignment. |
| Using single quotes (Q000) | The project enforces `inline-quotes = double`. Use `"nan"` not `'nan'`. |
| Multi-line class docstring formatting (D205/D209) | Summary line, then blank line, then body. Closing `"""` on its own line. |
| Placing `# noqa: B023` on the `def` line instead of the violation line | `# noqa: B023` must go on the line that **references** the loop variable, not the `def _(a):` line above it. |
