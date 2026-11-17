import pytest


@pytest.fixture(autouse=True)
def _no_first_run_setup(monkeypatch):
    """Tests run against a fresh database each time; skip the one-time name/icon setup page unless a test wants it."""
    monkeypatch.setenv("MINIBOORU_SKIP_SETUP", "1")
