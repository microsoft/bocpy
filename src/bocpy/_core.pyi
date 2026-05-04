"""Type stubs for private :mod:`bocpy._core` accessors.

Public re-exports are stubbed in :mod:`bocpy.__init__`; this file
only covers the private accessors used by the test suite and
internal tooling.
"""

from typing import Any


def scheduler_stats() -> list[dict[str, Any]]:
    """Snapshot the per-worker scheduler counters.

    Returns ``[]`` when the scheduler runtime is down (no workers
    allocated) -- this includes the window between :func:`bocpy.wait`
    returning and the next :func:`bocpy.start` / ``@when`` call. To
    capture a snapshot for a session that has just ended, use
    :func:`bocpy.wait` with ``stats=True``.

    When the runtime is up, returns a list with one dict per worker,
    each carrying the fields ``worker_index``, ``pushed_local``,
    ``dispatched_to_pending``, ``pushed_remote``, ``popped_local``,
    ``popped_via_steal``, ``enqueue_cas_retries``,
    ``dequeue_cas_retries``, ``batch_resets``, ``steal_attempts``,
    ``steal_failures``, ``parked``, ``last_steal_attempt_ns``, and
    ``fairness_arm_fires``.

    Counter semantics:

    * ``pushed_local`` / ``dispatched_to_pending`` / ``pushed_remote``
      record this worker's *role as producer*: they are bumped when
      this worker dispatches a behaviour (locally, into the empty
      ``pending`` slot, or onto another worker). They are **not**
      bumped when nodes arrive via a thief's
      ``boc_wsq_enqueue_spread`` re-distribution -- the global
      reconciliation ``Σ pushed_* == Σ popped_*`` holds across all
      workers, but the per-worker ``pushed_* − popped_*`` is **not**
      a local queue-depth estimate.
    * ``parked`` counts cumulative entries to the ``cnd_wait`` park
      arm.
    * ``last_steal_attempt_ns`` is a monotonic timestamp (ns; zero
      if the worker has never attempted a steal) of this worker's
      most recent steal attempt.
    * ``fairness_arm_fires`` counts the times this worker actually
      honoured ``should_steal_for_fairness`` (flag set AND queue
      non-empty when ``pop_slow`` checked it).

    Reads are best-effort (``memory_order_relaxed``); the snapshot
    may observe individual counters from different points in time.

    :return: A list of per-worker stats dicts.
    :rtype: list[dict[str, Any]]
    """


def queue_stats() -> list[dict[str, Any]]:
    """Snapshot the per-tagged-queue contention counters.

    Returns one dict per assigned ``BOCQueue``. Each dict carries
    ``queue_index``, ``tag`` (str or ``None``), ``enqueue_cas_retries``,
    ``dequeue_cas_retries``, ``pushed_total``, and ``popped_total``.
    Reads are best-effort (``memory_order_relaxed``).

    :return: A list of per-queue stats dicts.
    :rtype: list[dict[str, Any]]
    """


def physical_cpu_count() -> int:
    """Return the best-effort count of physical CPU cores available to
    this process.

    Unlike ``os.cpu_count()`` and ``len(os.sched_getaffinity(0))``,
    excludes hyperthread / SMT siblings so it reflects the count of
    independent execution units. Used to size the default worker
    pool (see :data:`bocpy.WORKER_COUNT`): oversubscribing CPU-bound
    Python workloads on hyperthread siblings often *reduces*
    throughput because two siblings on the same physical core fight
    for the same execution resources.

    Per-platform sourcing:

    * **Linux**: walks ``/sys/devices/system/cpu/cpu*/topology/thread_siblings_list``
      and intersects with ``sched_getaffinity(0)`` so cgroup /
      container CPU restrictions are honoured.
    * **macOS**: ``sysctlbyname("hw.physicalcpu_max", ...)`` with
      ``"hw.physicalcpu"`` as fallback.
    * **Windows**: ``GetLogicalProcessorInformationEx`` with
      ``RelationProcessorCore``.

    Returns ``0`` on any detection failure (sysfs unreadable,
    sysctl / API failure, etc.); callers should fall back to the
    logical CPU count in that case.

    :return: Physical core count, or 0 on failure.
    :rtype: int
    """
