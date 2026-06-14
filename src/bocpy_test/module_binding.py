"""Module-binding fixture: a bare behavior body reading a module global.

``lib_behavior`` is a plain behavior-body function (not wrapped in a
``@when`` scheduler) so it can be handed straight to
``register_behavior``. It reads a module-level global to prove that a
behavior resolved from the marshalled-code registry binds its globals to
its *defining* module rather than to the bindings module of the
interpreter that runs it. See ``TestResolverModuleBinding``
in ``test_registry.py``.
"""

LIB_CONSTANT = 4242


def lib_behavior(c):
    """Behavior body that reads ``LIB_CONSTANT`` from this module."""
    return c.value + LIB_CONSTANT
