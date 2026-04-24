"""Behavior-oriented Concurrency."""

from ._core import drain, receive, send, set_tags, TIMEOUT
from ._math import Matrix
from .behaviors import (Behaviors, Cown, notice_delete, notice_read,
                        notice_sync, notice_update, notice_write, noticeboard,
                        noticeboard_version, REMOVED,
                        start, wait, when, whencall, WORKER_COUNT)

__all__ = ["Behaviors", "Cown", "Matrix", "REMOVED", "TIMEOUT",
           "WORKER_COUNT", "drain", "notice_delete", "notice_read",
           "notice_sync", "notice_update", "notice_write", "noticeboard",
           "noticeboard_version", "receive",
           "send", "set_tags", "start", "wait", "when", "whencall"]
