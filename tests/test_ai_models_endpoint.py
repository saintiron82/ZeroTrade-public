"""Tests for /api/ai/models — provider→models catalog used by the UI selector."""

from fastapi.testclient import TestClient


def _client():
    from src.web.app import app
    return TestClient(app)


def test_models_endpoint_returns_per_provider_catalog():
    """Returns {providers: [{id, models: [{id, name}, ...]}]}."""
    resp = _client().get("/api/ai/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "providers" in data
    by_id = {p["id"]: p for p in data["providers"]}
    # All three known providers present
    assert {"codex", "claude", "copilot"} <= set(by_id.keys())
    # Each provider has a non-empty models list
    for prov in data["providers"]:
        assert "models" in prov
        assert isinstance(prov["models"], list)
        assert len(prov["models"]) >= 1
        # Each model has id and name
        for m in prov["models"]:
            assert "id" in m
            assert "name" in m


def test_models_filtered_by_provider_query():
    """?provider=codex returns only codex models."""
    resp = _client().get("/api/ai/models?provider=codex")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "codex"
    assert isinstance(data["models"], list)
    assert len(data["models"]) >= 1


def test_set_ai_config_accepts_model():
    """POST /bots/{id}/ai-config persists ai_model alongside ai_provider."""
    client = _client()
    r = client.post(
        "/api/bots/mirinae-2/ai-config",
        json={"ai_provider": "claude", "ai_model": "claude-sonnet-4-6"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["ai_provider"] == "claude"
    assert data["ai_model"] == "claude-sonnet-4-6"

    # Round-trip
    g = client.get("/api/bots/mirinae-2/ai-config").json()
    assert g["ai_provider"] == "claude"
    assert g["ai_model"] == "claude-sonnet-4-6"
