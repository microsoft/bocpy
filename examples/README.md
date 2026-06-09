# Examples

This directory contains some examples of Behaviour-Oriented Concurrency (BOC)
usage that may be of use when designing your own BOC programs. Many of them
have been translated from
[this repository](https://github.com/ic-slurp/boc-examples), where they are
implemented in C++.

## [Sketches](sketches.py)
This is the full example from the README, in which versions of two Monty Python
sketches run in parallel. It shows some basic usage for cowns and behaviors.

## [Cooking with BOC](cooking_boc.py)
This example is from [the tutorial](https://microsoft.github.com/bocpy), and
it may be helpful to compare and contrast it with the functionality equilvalent
version [using threads](cooking_threads.py).

## [Atomic Bank Transfer](bank.py)
Here we show how BOC can be used to change the state of two resources
simultaneously, in this case with a simulated bank transfer.

## [Dining Philosophers](dining_philosophers.py)
No concurrent programming library would be complete without an implementation of
the classic [Dining Philosophers](https://en.wikipedia.org/wiki/Dining_philosophers_problem)
problem. This is particularly elegant with BOC, as the dilemma (breaking deadlock 
on the forks) does not arise by construction.

## [Fibonacci](fibonacci.py)
This example is certainly not an efficient solution to computing
[Fibonacci numbers](https://en.wikipedia.org/wiki/Fibonacci_sequence), but instead
is a demonstration of how behaviors can spawn subsequent behaviors. In this example,
an entire graph of computation is created and then resolved in parallel, 
while the ordering required for correct computation is automatically maintained.

## [Boids](boids.py)
The [boids](https://en.wikipedia.org/wiki/Boids) agent-based simulation of flocking
birds provides fertile ground for exploring many interesting aspects of BOC. The
threadsafe and BOC-enlightened
[`Matrix`](http://microsoft.github.io/bocpy/sphinx/api.html#bocpy.Matrix) class is used
to store boid positions and velocities and to compute the changing positions. Boid
updates are computed on grid cells, such that for each grid cell to change requires
unique access to that cell and up to 8 of its neighbors. As the demonstrator runs at
framerate for hundreds of boids, the resulting simulation creates tens of thousands
of behaviors and cowns every second.

The simulation also demonstrates the
[`PinnedCown`](http://microsoft.github.io/bocpy/sphinx/api.html#bocpy.PinnedCown)
pattern: positions and velocities are exposed to the pyglet render loop via main-thread
aliases (read directly between frames) while a single pinned `@when` per frame
performs the write-back, dispatched by `pump()` inside the pyglet update tick. Lots
of thanks to [Ben Eater's Boids repo](https://github.com/beneater/boids.git), which
proved a helpful starting point.

## [Prime Factor](prime_factor.py)
This example generates a semiprime (a product of two primes) and then factors it
in parallel using multiple search lanes. Each lane is a chain of small behaviors
that check the [noticeboard](http://microsoft.github.io/bocpy/sphinx/api.html#bocpy.noticeboard)
for a result before doing a batch of trial divisions. When any lane finds a
factor it writes to the noticeboard, and the remaining lanes see the result on
their next check and stop early. Demonstrates the "behavior loop" pattern and
cross-behavior coordination via the noticeboard.

## [Benchmark](benchmark.py)
`benchmark.py` is the workhorse used to track BOC runtime overhead across releases.
The default run measures end-to-end throughput on a matmul-fanout workload; the
key knobs are summarised below.

- `--null-payload` — skip the matmul inner loop so the reported throughput
  reflects pure scheduler / messaging overhead with the application work
  removed. Useful when chasing scheduler regressions.
- `--pinned-spinner` — during the measurement window, drive a tail-recursing
  `@when` on a `PinnedCown` via `pump(max_behaviors=1)` so the C-level
  pinned-queue 0&rarr;1 wakeup path is loaded *alongside* the worker `@when`
  stream. Used to verify worker-throughput regression under high-rate pinned
  dispatch.
- `--pinned-spinner-sleep-s` (default `0.001`, i.e. ~1 kHz) — per-iteration
  sleep inside the pinned-spinner body. Controls the dispatch rate.
- `--repeats`, `--output`, `--table` / `--no-table`, `--quiet` — repeat-count,
  results-file path, and reporting toggles for batch runs.
- `--emit-scheduler-stats` — capture per-worker `scheduler_stats()` and
  `queue_stats()` snapshots after each repeat and embed them in the result
  JSON.

See [`scripts/bench_matrix.py`](../scripts/bench_matrix.py) for the
matrix-arithmetic micro-bench used to guard `_math.c` performance.

## [Fanout Benchmark](fanout_benchmark.py)
`fanout_benchmark.py` measures the dispatch-rate ceiling for the *fanout*
workload: a producer behavior that, on every step, allocates a batch of
fresh consumer cowns it does not hold and dispatches one `@when` per
consumer before rescheduling itself. Because the producer never holds the
consumer cowns, every child dispatch exercises the producer-local arm of
the scheduler, so the benchmark surfaces per-worker queue contention
(`enqueue_cas_retries`) — complementing `benchmark.py`'s chain workload.

- `--producers`, `--fanout-width`, `--producer-steps` — shape the workload
  (number of producer cowns, consumers dispatched per step, and steps per
  producer).
- `--payload-rows` / `--payload-cols` — size of each consumer's `Matrix`.
- `--sweep-axis` / `--sweep-values` — sweep one knob across a list of values
  in a single run.
- `--repeats`, `--output`, `--quiet`, `--json-stdout` — repeat-count,
  results-file path, and reporting toggles for batch runs.

