"""Cross-module identical-body collision fixture B (see ``collide_a``).

The ``schedule_probe`` body here is byte-identical to the one in
:mod:`bocpy_test.collide_a`; only ``OFFSET`` differs. See that module's
docstring for the full rationale.
"""

from bocpy import Cown, when

OFFSET = 2000


def schedule_probe(c: Cown) -> Cown:
    """Schedule a behavior that adds this module's ``OFFSET`` to the cown."""
    @when(c)
    def probe(c):
        return c.value + OFFSET

    return probe
