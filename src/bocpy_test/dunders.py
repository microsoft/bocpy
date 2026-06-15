"""Module-dunder fixture for an imported-package behavior.

``schedule_read_dunders`` schedules a behavior that reads ``__name__``
and ``__package__``. When a worker resolves it through the
imported-module branch of ``Resolver._target_dict`` (importing this
package module), the body's globals bind to *this* module's namespace,
so the dunders report ``bocpy_test.dunders`` / ``bocpy_test`` rather than
the runtime bindings module's values. This complements the
bindings-module dunder coverage, which reads the test module's own
``__name__``.
"""

from bocpy import Cown, when


def schedule_read_dunders(c: Cown) -> Cown:
    """Schedule a behavior that returns this module's ``(__name__, __package__)``."""
    @when(c)
    def read(c):
        return (__name__, __package__)

    return read
