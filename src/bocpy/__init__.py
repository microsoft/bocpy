"""Behavior-oriented Concurrency."""

from importlib import metadata as _metadata
import logging as _logging
import os
import sys

from ._core import drain, receive, send, set_tags, TIMEOUT
from ._math import Matrix
from .behaviors import (Behaviors, Cown, notice_delete, notice_read,
                        notice_sync, notice_update, notice_write, noticeboard,
                        PinnedCown, pump, PumpResult, quiesce,
                        REMOVED,
                        set_pump_watchdog, set_wait_pump_poll,
                        start, wait, WaitResult, when, whencall, WORKER_COUNT)

try:
    __version__ = _metadata.version("bocpy")
except Exception as _version_lookup_error:  # pragma: no cover - source checkout w/o install
    # ``importlib.metadata.version`` normally raises
    # ``PackageNotFoundError`` for an uninstalled package, but the
    # broader ``Exception`` net catches namespace-package and
    # vendored-installer edge cases (Bazel ``py_binary`` zip
    # imports, Nuitka-frozen apps, some PEP 660 editable hybrids)
    # that surface as other ``importlib.metadata`` exceptions. The
    # fallback string lets ``import bocpy`` keep working in those
    # environments instead of dying at module load. Assign the
    # fallback first so the WARNING emit — which is best-effort and
    # itself wrapped — cannot leave ``__version__`` unbound if the
    # logging stack is broken (closed stderr, misconfigured handler).
    __version__ = "0.0.0+unknown"
    # Best-effort WARNING naming the exception class so a corrupt
    # installation does not silently masquerade as a clean source
    # checkout in downstream version gates / telemetry. Swallow any
    # logger failure: import-time logging is not load-bearing.
    try:
        _logging.getLogger("bocpy").warning(
            "bocpy package metadata unavailable (%s: %s); "
            "falling back to __version__ = '0.0.0+unknown'",
            type(_version_lookup_error).__name__,
            _version_lookup_error,
        )
    except Exception:
        pass


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


__all__ = ["Behaviors", "Cown", "Matrix", "PinnedCown", "PumpResult",
           "REMOVED", "TIMEOUT",
           "WORKER_COUNT", "__version__", "drain",
           "get_include", "get_sources",
           "notice_delete", "notice_read",
           "notice_sync", "notice_update", "notice_write", "noticeboard",
           "pump",
           "quiesce",
           "receive",
           "send", "set_pump_watchdog", "set_tags", "set_wait_pump_poll",
           "start", "wait", "WaitResult",
           "when", "whencall"]
