"""
Suite-wide safety net for the photo-agent tests: no real credentials, no real client .env
files, no network. The mechanism, and exactly what it does and does not cover, is
documented in tests/_isolation.py; tests/test_suite_isolation.py proves each route.

Installed at IMPORT of this conftest, not in a fixture. pytest imports the conftest of a
test directory before it collects (and so imports) any test module in it, and every
production module under tools/ and scripts/ is imported by those test modules — so this runs
before any of them resolves a credential path, loads a .env, or opens a connection, and no
per-test gap exists for anything to slip through. pytest_configure re-asserts it (a no-op if
already installed) for runs that load this conftest through a plugin path first.
"""

from tests import _isolation

_isolation.install()


def pytest_configure(config):
    _isolation.install()
