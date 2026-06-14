"""Global-capture behavior fixtures for the bocpy test suite.

``Multiplier`` exercises two ways a behavior reaches a module-level
value: capturing it as an explicit default (``multiply``) and reading it
as a free global (``multiply_direct``). Because this module is installed
as part of the ``bocpy_test`` package, a worker resolves these behaviors
through the imported-module branch of ``Resolver._target_dict`` and
binds their globals to *this* module -- so ``multiply_direct`` proves a
behavior's free globals resolve against its defining module across an
interpreter boundary, even when the runtime's bindings module is a
different module entirely.
"""

from bocpy import Cown, when

GLOBAL_FACTOR = 7


class Multiplier:
    """Multiplies a cown's value by a module-level global inside a method."""

    def multiply(self, x: Cown) -> Cown:
        """Schedule a behavior that captures GLOBAL_FACTOR from module scope."""
        factor = GLOBAL_FACTOR

        @when(x)
        def do_multiply(x, factor=factor):
            return x.value * factor

        return do_multiply

    def multiply_direct(self, x: Cown) -> Cown:
        """Schedule a behavior that captures GLOBAL_FACTOR directly."""
        @when(x)
        def do_multiply(x):
            return x.value * GLOBAL_FACTOR

        return do_multiply
