"""Behavior-oriented Concurrency."""

import os
import sys

from ._core import drain, receive, send, set_tags, TIMEOUT
from ._math import Matrix
from .behaviors import (Behaviors, Cown, notice_delete, notice_read,
                        notice_sync, notice_update, notice_write, noticeboard,
                        REMOVED,
                        start, wait, when, whencall, WORKER_COUNT)


def get_include() -> str:
    """Return the absolute path to the bocpy public C header root.

    Use the returned path as an additional ``include_dirs`` entry on a
    downstream :class:`setuptools.Extension` so its translation units
    can ``#include <bocpy/bocpy.h>``. The directory contains a single
    ``bocpy/`` subdirectory holding the public ABI surface; bocpy's
    private headers are not exposed.

    :return: Absolute filesystem path to the include root (the parent
        of the ``bocpy/`` subdirectory containing ``bocpy.h`` and
        ``xidata.h``).
    :rtype: str
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "include")


def get_sources() -> list[str]:
    """Return platform-specific extra C sources for downstream extensions.

    On Windows the returned list contains the absolute path to
    ``bocpy_msvc.c``, which provides MSVC out-of-line bodies for the
    atomic ops declared in ``<bocpy/bocpy.h>``. On non-Windows
    platforms the list is empty (``<stdatomic.h>`` provides
    everything).

    :return: A list of absolute paths to add to a downstream
        :class:`setuptools.Extension`'s ``sources=`` list.
    :rtype: list[str]
    """
    if sys.platform == "win32":
        return [os.path.join(get_include(), "bocpy", "bocpy_msvc.c")]
    return []


__all__ = ["Behaviors", "Cown", "Matrix", "REMOVED", "TIMEOUT",
           "WORKER_COUNT", "drain", "get_include", "get_sources",
           "notice_delete", "notice_read",
           "notice_sync", "notice_update", "notice_write", "noticeboard",
           "receive",
           "send", "set_tags", "start", "wait", "when", "whencall"]
