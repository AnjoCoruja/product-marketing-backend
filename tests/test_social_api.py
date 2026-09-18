"""Testes dos endpoints /social/* (FASE 7)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api
from agents.social_media_agent import SocialMediaAgent
from services.publication_registry import PublicationRegistry
from tools.social_publish import FakeSocialBackend, PublishToPlatformTool


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", "")
    monkeypatch.setenv("SOCIAL_ENABLE_INSTAGRAM", "true")
    monkeypatch.setenv("SOCIAL_ENABLE_FACEBOOK", "true")
    monkeypatch.setenv("SOCIAL_ENABLE_TIKTOK", "true")
    monkeypatch.setenv("PUBLICATION_REGISTRY_DB", str(tmp_path / "pub.db"))
    from config import get_settings
    get_settings.cache_clear()
    yield TestClient(api.app)
    get_settings.cache_clear()


PAYLOAD = {
    "product_id": "P000001",
    "campaign_id": "C000001",
    "image_url": "https://drive.example.com/img.jpg",
    "caption_instagram": "Camisa! R$ 59,90",
    "caption_facebook": "Camisa nova.",
    "caption_tiktok": "Camisa",
    "hashtags": ["moda"],
}


def test_publish_all_platforms(client):
    resp = client.post("/social/publish", json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["campaign_id"] == "C000001"
    platforms = {p["platform"]: p for p in body["publications"]}
    assert set(platforms) == {"instagram", "facebook", "tiktok"}
    for p in platforms.values():
        assert p["status"] == "published"
        assert p["post_id"]
        assert p["published_at"]
        assert p["error"] is None


def test_publish_requires_image(client):
    payload = {**PAYLOAD, "image_url": "", "image_base64": None}
    resp = client.post("/social/publish", json=payload)
    assert resp.status_code == 400


def test_status_endpoint(client):
    client.post("/social/publish", json=PAYLOAD)
    resp = client.get("/social/status/C000001")
    assert resp.status_code == 200
    recs = {r["platform"]: r for r in resp.json()["publications"]}
    assert recs["instagram"]["status"] == "published"
    assert recs["instagram"]["post_id"]


def test_status_unknown_campaign(client):
    resp = client.get("/social/status/C999999")
    assert resp.status_code == 404


def test_independent_failure_via_api(client, monkeypatch):
    original = FakeSocialBackend.publish

    def flaky(self, **kwargs):
        if self.platform == "instagram":
            from tools.social_publish import PublishError
            raise PublishError("Instagram fora do ar")
        return original(self, **kwargs)

    monkeypatch.setattr(FakeSocialBackend, "publish", flaky)
    resp = client.post("/social/publish", json=PAYLOAD)
    assert resp.status_code == 200
    platforms = {p["platform"]: p for p in resp.json()["publications"]}
    assert platforms["instagram"]["status"] == "failed"
    assert "Instagram fora do ar" in platforms["instagram"]["error"]
    assert platforms["facebook"]["status"] == "published"
    assert platforms["tiktok"]["status"] == "published"


def test_idempotent_republish_via_api(client):
    client.post("/social/publish", json=PAYLOAD)
    first = client.get("/social/status/C000001").json()["publications"]
    resp = client.post("/social/publish", json=PAYLOAD)
    second = {p["platform"]: p for p in resp.json()["publications"]}
    # mesmo post_id — não republicou
    for rec in first:
        assert second[rec["platform"]]["post_id"] == rec["post_id"]
