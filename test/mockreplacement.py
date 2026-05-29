"""Mock-free monkey-patch helpers for the bocpy test suite.

:mod:`unittest.mock` cannot be used in tests that schedule ``@when``
behaviors: the transpiler re-exports each test module into every
worker sub-interpreter, and ``unittest.mock`` transitively imports
``asyncio``, which can deadlock during PEP 684 per-interpreter init
on macOS arm64 (Python 3.12 / 3.13).

Provides :func:`patch_attr` (a thin ``mock.patch.object``) and
:class:`Recorder` (an auto-vivifying ``MagicMock`` stand-in covering
the ``assert_called_once`` / ``call_args[0][0]`` patterns used here).
"""

from contextlib import contextmanager


@contextmanager
def patch_attr(target, name, new):
    """Temporarily set ``target.name = new``; restore on exit.

    Raises ``AttributeError`` if *name* is not already an attribute
    of *target* so a typo cannot silently create a new attribute.
    """
    if not hasattr(target, name):
        raise AttributeError(
            f"{target!r} has no attribute {name!r}; "
            "patch_attr refuses to silently create new attributes"
        )
    original = getattr(target, name)
    setattr(target, name, new)
    try:
        yield new
    finally:
        setattr(target, name, original)


class RecorderMethod:
    """Callable that records every invocation.

    Covers the ``MagicMock`` subset used by the suite: ``call_count``,
    ``call_args_list``, ``call_args``, ``assert_called_once``,
    ``return_value`` and ``side_effect`` (callable or exception).
    """

    def __init__(self, name="<RecorderMethod>"):
        """Initialise the recorder; *name* labels it in error messages."""
        self._name = name
        self.call_count = 0
        self.call_args_list = []
        self.return_value = None
        self.side_effect = None

    @property
    def call_args(self):
        """Most recent ``(args, kwargs)`` tuple, or ``None``."""
        return self.call_args_list[-1] if self.call_args_list else None

    def __call__(self, *args, **kwargs):
        """Record the call, then honour ``side_effect`` / ``return_value``."""
        self.call_count += 1
        self.call_args_list.append((args, kwargs))
        side = self.side_effect
        if side is not None:
            if isinstance(side, BaseException) or (
                isinstance(side, type) and issubclass(side, BaseException)
            ):
                raise side
            if callable(side):
                return side(*args, **kwargs)
        return self.return_value

    def assert_called_once(self):
        """Raise ``AssertionError`` unless invoked exactly once."""
        if self.call_count != 1:
            raise AssertionError(
                f"{self._name}: expected exactly 1 call, got {self.call_count}"
            )


class Recorder:
    """Attribute-auto-vivifying recorder, stand-in for ``MagicMock``.

    Reading an undefined attribute auto-creates and caches a fresh
    :class:`RecorderMethod`. Setting an attribute stores the value
    verbatim (no recording), matching ``MagicMock`` ergonomics for
    pre-configured side effects.
    """

    def __init__(self, name="<Recorder>"):
        """Initialise the recorder; *name* labels child methods in errors."""
        object.__setattr__(self, "_recorder_name", name)
        object.__setattr__(self, "_recorder_attrs", {})

    def __getattr__(self, name):
        """Auto-create a :class:`RecorderMethod` for *name* on first access."""
        if name.startswith("_recorder_"):
            raise AttributeError(name)
        attrs = object.__getattribute__(self, "_recorder_attrs")
        if name not in attrs:
            attrs[name] = RecorderMethod(f"{self._recorder_name}.{name}")
        return attrs[name]
