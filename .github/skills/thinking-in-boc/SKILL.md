---
name: thinking-in-boc
description: "Think in Behavior-Oriented Concurrency, not threads-and-locks. Use when: writing or reviewing any bocpy code (library, examples, tests), about to reach for time.sleep / threading.Event / atomic counters / polling loops / wait_for_* helpers, designing how a downstream behavior observes an upstream one, scheduling work to run after the next worker is free, or building loop / tail-recursion patterns. Catches the reflex to apply classical synchronization to a problem that wants a cown."
---

# Thinking in Behavior-Oriented Concurrency

This skill is a corrective. The default reflex when synchronizing concurrent
work is to reach for **threads-and-locks** primitives: shared state, a mutex,
a condition variable, a busy-wait loop, an atomic counter, an event flag, a
`Future`. In BOC, those answers are almost always wrong — not because they
break, but because they bypass the very mechanism that makes BOC safe and
fast.

The BOC question is not *"what synchronization primitive do I need here?"*

It is: ***"what cown is this work ordered against, and what behavior should
run when that cown is free?"***

Read this skill any time you catch yourself writing one of the smells below.

## The smells

If you find yourself typing any of these inside, or in code that interacts
with, a BOC program — **stop and re-derive the design.**

| Smell | What you almost certainly meant |
|-------|---------------------------------|
| `time.sleep(...)` in a polling loop | Schedule a behavior on the cown the predicate depends on. |
| `while not <flag>: ...` busy-wait | Same — make `<flag>` a cown and `@when(flag)` a behavior. |
| `threading.Event` / `Condition` / `Lock` | A cown plus a behavior chain. |
| `wait_for_<x>_version(target)` polling | `@when(downstream_cowns)` — let the cown graph order it. |
| `atomic_counter` from Python | A `Cown(int)` mutated inside `@when(counter)`. |
| `Future`, `Queue.get()`, "ferry one value out" | `return` the value from a behavior; `@when(that_behavior)` reads it. |
| `time.sleep(0)` "yield" | `@when()` — the empty-cown behavior runs when a worker is free. |
| `if work_remaining: do_work(); else: stop` in a thread loop | A **behavior loop**: the behavior re-schedules itself with `@when(state)` on the same cown until done. |

The smells are signals that you are managing concurrency *outside* the
runtime. The runtime cannot help you make that correct or fast.

## The replacements

There are only a handful of BOC patterns. Almost every problem decomposes
into one of them.

### 1. Sequencing on data — `@when(cown)`

A behavior runs when its cowns are free. That is the entire ordering
mechanism. If `step2` must observe `step1`'s effect on `x`, both behaviors
take `x`:

```python
@when(x)
def step1(x):
    x.value = "ready"

@when(x)
def step2(x):
    assert x.value == "ready"
```

You did not need a lock. You did not need an event. You did not need to
poll. The runtime acquired `x` for `step1`, released it, and only then gave
it to `step2`.

### 2. Fan-in / barrier — `@when(cowns)` vs `@when(a, b, c)`

There are two distinct shapes for "this behavior depends on multiple
cowns" and choosing the right one matters.

**Use `@when(a, b, c)`** — separate positional arguments — when you know
**at write-time exactly which cowns** the behavior needs and they have
distinct roles. The decorated function takes one named parameter per
cown:

```python
@when(account_a, account_b)
def transfer(src, dst):                     # two roles, two names
    dst.value += src.value
    src.value = 0
```

**Use `@when(cowns)`** — a single list/tuple argument — when the **set
is dynamic or homogeneous** (its size is determined at runtime, or the
cowns play the same role). The decorated function takes **one parameter**
which is the list itself:

```python
cowns = [Cown(i) for i in range(N)]
for c in cowns:
    @when(c)
    def producer(c):
        ...                                 # writes whatever it writes

@when(cowns)                                # one list arg, not *cowns
def consumer(cowns):
    total = sum(c.value for c in cowns)     # cowns IS the list
```

This is the classical N-way barrier, expressed as data dependence: the
runtime acquires every cown in the list before the behavior runs, so the
consumer cannot start until every producer behavior has returned. **Do
not** spread the list with `*` — `@when` accepts the list directly, and
spreading would force you to know `N` at write-time, defeating the point.

Mixing the two forms — `@when(anchor, cowns)` — is also valid: the
behavior takes one named parameter (`anchor`) plus one list parameter.

### 3. Happens-after — chain on the prior behavior's result cown

`@when` returns a `Cown` holding the behavior's result. Pass that cown to a
later `@when` to enforce happens-after across unrelated data:

```python
@when(x)
def writer(x):
    notice_write("k", x.value)
    notice_sync()                           # commit before returning

@when(x, writer)                            # waits for writer to finish
def reader(x, _):
    assert notice_read("k") == x.value
```

### 4. Run when *any* worker is free — `@when()`

`@when()` with no arguments schedules a behavior with no data dependencies.
It runs as soon as a worker is available. Use this when you want some work
to happen in the background and you do not need to coordinate with any
particular cown — for example, sending a report after forks have been
released:

```python
@when(left, right, hunger)
def take_bite(left, right, hunger):
    left.value.use(); right.value.use()
    hunger.value -= 1
    if hunger.value == 0:
        # forks released when this behavior returns; the report goes
        # out from a fresh behavior so it does not delay the release.
        @when()
        def _():
            send("report", ("full", index))
```

`@when()` is also the BOC equivalent of "tail-call this on the worker
pool" — it lets the current behavior return promptly while the follow-up
work waits its turn.

### 5. Behavior loops — tail-recursive self-scheduling

To process work in chunks until done, do **not** write a `while` loop
inside one behavior — that pins one worker for the duration. Instead, the
behavior does one chunk and then **schedules the next iteration** on the
same cown:

```python
def step(state: Cown[State]):
    @when(state)
    def _(state):
        if state.value.done:
            send("done", state.value.result)
            return

        state.value.do_one_chunk()
        step(state)                         # tail-schedule next iteration
```

This is the BOC analogue of tail recursion. Each iteration releases the
cown between chunks, so:

- other behaviors waiting on `state` can interleave between iterations,
- the worker is returned to the pool between chunks, and
- work is naturally bounded by data availability — no busy-wait.

`prime_factor.py` (`sieve_check` → `sieve_work` → `sieve_check`) is the
canonical example in this repository.

### 6. Flushing your own queued mutations — `notice_sync()`

The noticeboard mutator runs on its own thread. `notice_write` /
`notice_update` / `notice_delete` are fire-and-forget. If a *subsequent
behavior* must observe your noticeboard mutation, call `notice_sync()` at
the end of the writing behavior:

```python
@when(x)
def writer(x):
    notice_write("k", v)
    notice_sync()                           # block until commit

@when(x, writer)                            # now reader sees v
def reader(x, _):
    assert notice_read("k") == v
```

`notice_sync()` flushes **only the calling thread's** prior writes. For
cross-producer ordering, lean on `@when(cowns)` (pattern 2) — let the cown
graph do the synchronization, and let each writer's `notice_sync()` make
its own commit visible before it releases its cown.

### 7. Single-assignment rendezvous — the behavior's own result cown

`@when` returns a `Cown` holding whatever the behavior returns. That cown
*is* your rendezvous — there is no need to allocate a separate `Cown(None)`
and assign into it:

```python
@when(x)
def compute(x):
    return expensive(x.value)               # the result lives in `compute`

@when(compute)
def consume(result):                        # result is a Cown
    send("answer", result.value)            # unwrap with .value
```

This replaces `Future` / `Queue` for one-shot results. For streaming use
the message queue (`send` / `receive`) directly.

## The BOC checklist

Before writing **any** synchronization, ask:

1. **What cown does this work depend on?** If the answer is "none" you may
   want `@when()`. If the answer is "X" you want `@when(X)`. If you know
   at write-time exactly which cowns you need, prefer the explicit form
   `@when(X, Y, Z)` — it is faster than the list form because the runtime
   can resolve each dependency by position rather than iterating a
   sequence. Only fall back to `@when([X, Y, Z])` (one list arg) when the
   set is dynamic or homogeneous.
2. **Who reads my output?** Their `@when(...)` should include the cown I
   wrote to, or my behavior's result-cown.
3. **Am I about to loop in one behavior?** If the loop body has any
   release-friendly point, lift it into a behavior loop (pattern 5) so
   other work can interleave.
4. **Am I about to poll, sleep, or block?** Find the cown the predicate
   depends on. Make the polling code a behavior on that cown.
5. **Am I about to use a `threading.*` primitive inside a behavior?**
   Almost certainly the wrong layer. Threads-and-locks primitives belong
   only at the BOC runtime boundary (test setup, `wait()`, `receive()` for
   assertions, the runtime's own internals).

## When the classical answer *is* right

Classical synchronization is correct in three places:

1. **Outside the runtime, talking to it.** The test thread blocking on
   `receive("assert")` for assertion messages is a thread-level wait, and
   that is fine — it is the boundary between the test harness and the
   behavior graph.
2. **`wait()` itself.** The library uses condvars internally to block the
   main thread until the runtime drains. Do not reinvent this.
3. **C-level runtime internals.** `_core.c` uses mutexes and condvars
   because it *implements* BOC. User Python code should not.

If you are not in one of those three places and you are reaching for a
classical primitive, walk back through the checklist.

## Self-correction prompt

If you have already written code that uses `time.sleep`, `wait_for_*`, an
event flag, or a polling loop in a behavior or in code that schedules
behaviors, treat it as a defect. Ask:

> *Which cown carries the dependency I am polling on? Why is the
> consuming work not a behavior on that cown?*

Rewrite to remove the classical primitive. The result is almost always
shorter, faster, and provably free of races.
