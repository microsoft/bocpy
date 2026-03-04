# bocpy

![BOC Logo](http://microsoft.github.io/bocpy/images/logo.svg)

Behavior-Oriented Concurrency (*BOC*) is a new paradigm for parallel and concurrent
programming which is particularly well-suited to Python. In a BOC program, data is
shared such that each behavior has unique temporal ownership of the data, removing
the need for locks to coordinate access. For Python programmers, this brings a lot
of benefits. Behaviors are implemented as decorated functions, and from the programmer's
perspective, those functions work like normal. Importantly, the programmer's task
shifts from solving concurrent data access problems to organizing data flow through
functions. The resulting programs are easier to understand, easier to support, easier
to extend, and unlock multi-core performance due to the ability to schedule behaviors
to run efficiently across multiple processes.

BOC has been implemented in several languages, including as a foundational aspect
of the research language Verona, and now has been implemented in Python. 

## Getting Started

You can install `bocpy` via PyPi:

    pip install bocpy

We provide pre-compiled wheels for Python 3.10 onwards on most platforms, but if
you have problems with your particular platform/version combination, please file
an issue on [this repository](https://github.com/microsoft/bocpy/issues).

> [!NOTE]
> the `bocpy` library depends on the Cross-Interpreter Data mechanism, which was
> introduced in Python 3.7. We explicitly test and provide wheels for all versions
> of Python that have not been end-of-lifed (3.10.19 as of time of writing is the
> oldest version we support). The library may not work in older versions of Python.

A behavior can be thought of as a function which depends on zero or more
concurrently-owned data objects (which we call **cowns**). As a programmer, you
indicate that you want the function to be called once all of those resources are
available. For example, let's say that you had two complex and time-consuming
operations, and you needed to act on the basis of both of their outcomes:

```python

def buy_cheese():
    logger = logging.getLogger("cheese_shop")
    for name in all_known_cheeses():
        if is_available(logger, name):
            return name
    
    cleanup_shop(logger)
    return None


def order_meal(exclude: str):
    logger = logging.getLogger("greasy_spoon")
    for dish in menu():
        logger.info(dish)
        if exclude.lower() not in dish.lower():
            logger.info(f"That doesn't have much {exclude} in it")
            return dish

        vikings(logger)
        if random.random() < 0.3:
            logger.info("<bloody vikings>")

    return None


cheese = buy_cheese()
meal = order_meal(exclude="spam")

if meal is not None:
    eat(meal)
elif cheese is not None:
    eat(cheese)

if meal is not None:
    print("I really wanted some cheese...")
elif cheese is not None:
    print("Cheesy comestibles")

return_to_library()
```

The code above will work, but requires the purveying of cheese and the navigation
of the menu for non-spam options to happen sequentially. If we wanted to do these
tasks in parallel, we will end up with some version of nested waiting, which can
result in deadlock. With BOC, we would write the above like this:

```python
from bocpy import wait, when, Cown

# ...

def buy_cheese():
    cheese = Cown(None)

    @when(cheese)
    def _(cheese):
        logger = logging.getLogger("cheese_shop")
        for name in all_known_cheeses():
            if is_available(logger, name):
                cheese.value = name
                return

        cleanup_shop(logger)

    return cheese


def order_meal(exclude: str):
    order = Cown(None)

    @when(order)
    def _(order):
        logger = logging.getLogger("greasy_spoon")
        logger.info("We have...")
        for dish in menu():
            logger.info(dish)
            if exclude.lower() not in dish.lower():
                logger.info(f"That doesn't have much {exclude} in it")
                order.value = dish
                return

            vikings(logger)
            if random.random() < 0.3:
                logger.info("<bloody vikings>")

    return order


cheese = buy_cheese()
meal = order_meal(exclude="spam")


@when(cheese, meal)
def _(cheese, meal):
    if meal.value is not None:
        eat(meal.value)
    elif cheese.value is not None:
        eat(cheese.value)
    else:
        print("<stomach rumbles>")


@when(cheese, meal)
def _(cheese, meal):
    if meal.value is not None:
        print("I really wanted cheese...")
    elif cheese.value is not None:
        print("Cheesy comestibles!")

    return_to_library()


wait()
```

You can view the full example
[here](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/sketches.py)

The underlying BOC scheduler ensures that this operates without deadlock, by
construction.

### Examples

We provide a few examples to show different ways of using BOC in a program:

1. [`bocpy-bank`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/bank.py): Shows an example
   where two objects (in this case, bank accounts), interact in an atomic way.
2. [`bocpy-dining-philosophers`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/dining_philosophers.py):
   The classic Dining Philosphers problem implemented using BOC.
3. [`bocpy-fibonacci`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/fibonacci.py): A
   parallel implementation of Fibonacci calculation.
4. [`bocpy-cooking-boc`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/cooking_boc.py): The example from
   the [BOC tutorial](https://microsoft.github.io/bocpy/).
5. [`bocpy-boids`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/boids.py): An agent-based bird flocking
   example demonstrating the `Matrix` class to do distributed computation over cores.
   Note: you'll need to install `pyglet` first in order to run the `bocpy-boids` example.


## Why BOC for Python?
For many Python programmers, the GIL has established a programming model in which
they do not have to think about the many potential issues that are introduced by
concurrency, in particular data races. One of the best features of BOC is that, due
to the way behaviors interact with concurrently owned data (*cowns*), each behavior
can operate over its data without a need to change this familiar programming model.
Even in a free-threading context, BOC will reduce contention on locks and provide
programs which are data-race free by construction. Our initial research and experiments
with BOC have shown near linear scaling over cores, with up to 32 concurrent worker
processes.

### This library
Our implementation is built on top of the subinterpreters mechanism and the
Cross-Interpreter Data, or `XIData`, API. As of Python 3.12, sub-interpreters have
their own GIL and thus run in parallel, and thus BOC will also run fully in
parallel.

In addition the providing the `when` function decorator, the library also exposes
low-level Erlang-style `send` and selective `receive` functions which enable
lock-free communication across threads and subinterpreters. See the
[`bocpy-primes`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/primes.py) and
[`bocpy-calculator`](https://github.com/microsoft/bocpy/blob/main/src/bocpy/examples/calculator.py)
examples for the usage of these lower-level functions.

### Additional Info
BOC is built on a solid foundation of serious scholarship and engineering. For further reading, please see:
1. [When Concurrency Matters: Behaviour-Oriented Concurrency](https://dl.acm.org/doi/10.1145/3622852)
2. [Reference implementation in C#](https://github.com/microsoft/verona-rt/tree/main/docs/internal/concurrency/modelimpl)
3. [OOPSLA23 Talk](https://www.youtube.com/watch?v=iX8TJWonbGU)

> **Trademarks** This project may contain trademarks or logos for projects, products, or services.
> Authorized use of Microsoft trademarks or logos is subject to and must follow Microsoft’s
> Trademark & Brand Guidelines. Use of Microsoft trademarks or logos in modified versions of this
> project must not cause confusion or imply Microsoft sponsorship. Any use of third-party 
> trademarks or logos are subject to those third-party’s policies.
