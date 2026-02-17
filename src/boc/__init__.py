"""Behavior-oriented Concurrency."""

from typing import Any, Callable, List, Optional, Tuple, Union

import _boc


TIMEOUT: str = _boc.TIMEOUT


def send(tag: str, contents: Any):
    """Sends a message.

    :param tag: The tag is an arbitrary label that can be used to receive this message.
    :type tag: str
    :param contents: The contents of the message.
    :type contents: Any
    """
    _boc.send(tag, contents)


def receive(tags: Union[List[str], Tuple[str, ...], str],
            timeout: float = -1,
            after: Optional[Callable[[], Any]] = None,
            guard: Optional[Callable[[str, Any], bool]] = None) -> Optional[Any]:
    """Receives a message.

    :param tags: One or more tags. The received message will be tagged with one of these.
    :type tags: Union[List[str], Tuple[str, ...], str]
    :param timeout: A non-negative value indicates how many seconds receive should wait before returning.
                    A negative value indicates to wait until a message is received.
    :type timeout: float
    :param after: Optional callback which should be called to produce a value if receive times out.
    :type after: Optional[Callable[[], Any]]
    :param guard: Optional callback which examines the contents of a message. If it returns True, the contents are
                  returned. If False, the message is requeued and receive will obtain the next message.
    :type guard: Optional[Callable[[str, Any], bool]]
    :return: The contents of the received message
    :rtype: Any | None
    """
    return _boc.receive(tags, timeout, after, guard)


__all__ = ["send", "receive", "TIMEOUT"]


try:
    from .behaviors import start, wait, when, whencall, Behaviors, Cown, WORKER_COUNT

    __all__ += ["start", "wait", "when", "whencall", "Behaviors", "Cown", "WORKER_COUNT"]
except AttributeError:
    print("Behaviors not supported")
