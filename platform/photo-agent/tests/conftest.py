"""
Suite-wide safety net: no photo-agent test may make a real HTTP request.

Every external edge (Drive, Graph API, Telegram) is meant to be mocked per test, but
"meant to be" failed once: a newly added Drive function was not yet mocked in an older
fixture, and the real function ran — refreshing a real OAuth token from the developer's
credentials file and calling the real Drive API. Individual mocks still do the work;
this makes a missing one fail loudly instead of reaching a real account.

Patches requests.Session.request, which requests.get/post/put/delete all go through.
Tests that mock requests.<verb> or a module's own helper never reach it.
"""

import pytest
import requests


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    def _blocked(self, method, url, *args, **kwargs):
        raise AssertionError(
            f"real HTTP request attempted in a test: {method} {url.split('?')[0]} — "
            "mock the edge this code path reaches"
        )

    monkeypatch.setattr(requests.Session, "request", _blocked)
