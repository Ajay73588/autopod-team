"""Admin API key authentication tests (item 6.3)."""

import importlib


def _reload_auth(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ADMIN_API_KEY", key)
    import auth
    importlib.reload(auth)
    return auth


def test_missing_key_rejected_when_configured(monkeypatch):
    auth = _reload_auth(monkeypatch, "supersecret")

    class FakeRequest:
        headers = {}

    assert auth.check_admin_key(FakeRequest()) is False


def test_wrong_key_rejected_when_configured(monkeypatch):
    auth = _reload_auth(monkeypatch, "supersecret")

    class FakeRequest:
        headers = {"X-Admin-Key": "wrong-key"}

    assert auth.check_admin_key(FakeRequest()) is False


def test_correct_key_accepted(monkeypatch):
    auth = _reload_auth(monkeypatch, "supersecret")

    class FakeRequest:
        headers = {"X-Admin-Key": "supersecret"}

    assert auth.check_admin_key(FakeRequest()) is True


def test_auth_disabled_when_no_key_configured(monkeypatch):
    """Local-dev convenience: with no ADMIN_API_KEY set at all, every
    request is treated as authorized (loudly logged as a warning -- see
    auth.py). This must never be the default for anything deployed."""
    auth = _reload_auth(monkeypatch, None)

    class FakeRequest:
        headers = {}

    assert auth.check_admin_key(FakeRequest()) is True
    assert auth.admin_key_configured() is False


def test_query_param_key_rejected(monkeypatch):
    """Verify that ?admin_key= query parameter is rejected to prevent secret leakage in URLs/logs."""
    auth = _reload_auth(monkeypatch, "supersecret")

    class FakeRequest:
        headers = {}
        args = {"admin_key": "supersecret"}

    assert auth.check_admin_key(FakeRequest()) is False

    class BadFakeRequest:
        headers = {}
        args = {"admin_key": "wrong"}

    assert auth.check_admin_key(BadFakeRequest()) is False

