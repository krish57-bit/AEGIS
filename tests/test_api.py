"""Fast tests that need no database: tenant rules, embeddings, and API gating."""
import math

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.graph_engine import Conflict, NotFound, tenant_name, text_to_vec8


def test_tenant_name_rejects_malformed_and_unlisted(monkeypatch):
    monkeypatch.setenv("AEGIS_ALLOWED_TENANTS", "demo,acme_prod")
    assert tenant_name("acme_prod") == "aegis_acme_prod"
    for bad in ("acme-prod", "Acme", "", "a" * 49, "demo; DROP"):
        with pytest.raises(ValueError):
            tenant_name(bad)
    with pytest.raises(PermissionError):
        tenant_name("other")


def test_text_to_vec8_is_unit_length_and_deterministic():
    v = text_to_vec8("payment redis retry timeout")
    assert len(v) == 8 and math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-9)
    assert v == text_to_vec8("payment redis retry timeout")


class FakeEngine:
    def health(self):
        return {"status": "ok"}

    def graph_snapshot(self, t):
        return {"nodes": [], "edges": [], "tenant": t}

    def get_investigation(self, t, trace):
        raise NotFound("Investigation not found")

    def approve(self, t, trace):
        raise Conflict("Only a proposed investigation can be approved")

    def retain(self, t, threshold, dry_run):
        return {"dry_run": dry_run, "threshold": threshold}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("AEGIS_API_KEY", raising=False)
    monkeypatch.setenv("AEGIS_ALLOWED_TENANTS", "demo")
    monkeypatch.setattr(main, "engine", lambda: FakeEngine())
    return TestClient(main.app)


def test_health_is_public_and_graph_requires_allowed_tenant(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/graph").json()["tenant"] == "demo"
    assert client.get("/api/graph", headers={"X-Tenant-ID": "other"}).status_code == 403
    assert client.get("/api/graph", headers={"X-Tenant-ID": "bad-name"}).status_code == 422


def test_error_mapping(client):
    assert client.get("/api/investigations/x").status_code == 404
    assert client.post("/api/investigations/x/approve").status_code == 409


def test_retention_input_validation_and_dry_run_default(client):
    assert client.post("/api/memories/retain", json={}).json() == {"dry_run": True, "threshold": 0.1}
    assert client.post("/api/memories/retain", json={"threshold": 2}).status_code == 422


def test_api_key_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("AEGIS_API_KEY", "secret")
    assert client.get("/api/graph").status_code == 401
    assert client.get("/api/graph", headers={"X-API-Key": "secret"}).status_code == 200
