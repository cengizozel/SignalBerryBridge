import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    """Fresh bridge module bound to a temp DB. No workers started."""
    monkeypatch.setenv("SIGNAL_NUMBER", "+15550009999")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setenv("SIGNAL_API_URL", "http://signal-api.test:5000")
    import bridge as mod
    importlib.reload(mod)
    mod.init_db()
    return mod


@pytest.fixture
def client(bridge):
    bridge.app.config["TESTING"] = True
    return bridge.app.test_client()
