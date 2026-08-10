"""The test suite, as a real package.

The ``__init__.py`` is load-bearing rather than decorative. Some third-party
distributions install a top-level ``tests`` package into ``site-packages``; a
plain directory here would only be a namespace portion, and Python resolves a
regular package anywhere on the path in preference to a namespace one no matter
what the path order is. The installed stranger would win, and
``import tests.support...`` would import someone else's code.
"""
