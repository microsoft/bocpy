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
birds provides fertile ground for exploring many interesting aspects of BOC. First, 
the threadsafe and BOC-enlightened
[`Matrix`](http://microsoft.github.io/bocpy/sphinx/api.html#bocpy.Matrix) class is used
quite extensively to store boid positions and velocities and to compute the changing 
positions. Boid updates are computed on grid cells, such that for each grid cell to
change it requires unique access to that cell and up to 8 of its neighbors. As the
demonstrator runs at framerate for hundreds of boids, the resulting simulation creates
tens of thousands of behaviors and cowns every second. Lots of thanks to
[Ben Eater's Boids repo](https://github.com/beneater/boids.git), which proved
a helpful starting point.

## Send/Receive
In addition to exposing the higher-level behavior primitives (*i.e.*,
`when`, `Cown`, `wait`), the library also exposes the lower-level functions
[`send`](http://microsoft.github.io/bocpy/sphinx/api.html#bocpy.send) and
[`receive`](http://microsoft.github.io/bocpy/sphinx/api.html#bocpy.receive), which provide
lock-free Erlang-style send and selective receive. As this paradigm may be
unfamiliar, we provide a few examples for this lower-level API as well.

### Calculator
In this example, several clients send arithmetic commands concurrently in
parallel to a calculator server, which performs the operations and prints the
result. Shows basic `send`/`receive` functionality and how to provide timeout
information.

### Primes
In this example, you have a coordination thread producing work (in this case,
batches of integers) and worker threads doing work (here, counting primes).
Shows how to use `send`/`receive` to share work across multiple worker threads.
