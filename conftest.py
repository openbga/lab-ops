"""Fixtures for every test suite run from the repository root (pyproject testpaths)."""

import pytest


@pytest.fixture(autouse=True)
def _confirmed_unless_marked(request, monkeypatch):
    """Tests that are not about Confirm it is you (accounts.confirmation) save as if the person had
    just confirmed it is them, so that every form that asks need not be filled in twice. A test
    marked `confirmation` gets the real check."""
    if request.node.get_closest_marker("confirmation") is None:
        monkeypatch.setattr("accounts.confirmation.current", lambda request: True)
