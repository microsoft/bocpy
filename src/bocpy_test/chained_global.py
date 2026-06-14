"""Chained-behavior fixture proving non-constant module globals resolve.

``module_cache`` is a *lowercase, expression-valued* module-level global
-- exactly the kind the worker bindings reducer drops (it keeps imports,
classes, functions, and UPPERCASE constants only). Because this module is
installed as part of the ``bocpy_test`` package, a worker resolves these
behaviors through the imported-module branch of ``Resolver._target_dict``
and binds their globals to *this* module's real namespace, so the global
is present.

``schedule_chain`` mirrors the real-world failure shape: a first behavior
(scheduled from the caller) reads the global through a plain helper and
then schedules a *second* behavior in the same module that reads it too.
Both must see ``module_cache`` -- the regression was that a behavior in an
importable module resolved against reduced bindings (which had dropped the
global) and raised ``NameError``.
"""

from bocpy import Cown, when

# Lowercase and built from a call expression: the bindings reducer drops
# this, so a behavior that resolved against reduced bindings could not see
# it. The dict mirrors the physics ``shell_cache = geometry.ShellCache()``.
module_cache = dict(token="module-level-value")


def read_cache() -> str:
    """Read the module global from a plain (non-behavior) helper."""
    return module_cache["token"]


def schedule_direct(c: Cown) -> Cown:
    """Schedule a single behavior that reads the lowercase module global."""
    @when(c)
    def only(a):
        return read_cache()

    return only


def schedule_chain(c1: Cown, c2: Cown) -> Cown:
    """Schedule a behavior on c1 that chains a second behavior on c2.

    Both behaviors read the lowercase module global through ``read_cache``.
    Returns the first behavior's result cown, which itself holds the second
    behavior's result cown (the standard chained-result shape).
    """
    @when(c1)
    def first(a, c2=c2):
        first_token = read_cache()

        @when(c2)
        def second(b, first_token=first_token):
            return (first_token, read_cache())

        return second

    return first
