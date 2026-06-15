"""Dev-only behavior fixtures for the bocpy test suite.

Modules in this package define ``@when`` behaviors that must be
importable on worker sub-interpreters. The package is installed editably
(``pip install -e .``) but gated out of distributed wheels/sdists -- see
the ``_packages`` comment in ``setup.py``.
"""
