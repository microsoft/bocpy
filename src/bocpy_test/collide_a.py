"""Cross-module identical-body collision fixture A (see ``collide_b``).

``schedule_probe`` schedules a behavior whose body is *byte-identical*
to the one in :mod:`bocpy_test.collide_b`; the two differ only in the
module-level ``OFFSET`` global each body reads. Because the canonical
behavior key excludes ``co_filename``, the two bodies share the
code-identity half of their keys and are separated only by the folded-in
defining module. Scheduling both and observing divergent results proves
a worker binds each body's globals to its *own* defining module -- the
end-to-end regression guard for the cross-module key collision that the
process-global append-only registry would otherwise hit.
"""

from bocpy import Cown, when

OFFSET = 1000


def schedule_probe(c: Cown) -> Cown:
    """Schedule a behavior that adds this module's ``OFFSET`` to the cown."""
    @when(c)
    def probe(c):
        return c.value + OFFSET

    return probe
