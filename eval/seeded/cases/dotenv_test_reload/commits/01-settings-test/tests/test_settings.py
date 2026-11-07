import importlib

import app.settings


def test_api_url_unset(monkeypatch):
    monkeypatch.delenv("API_URL", raising=False)
    importlib.reload(app.settings)
    assert app.settings.API_URL is None
