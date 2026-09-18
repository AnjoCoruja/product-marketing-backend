"""Fluxo completo via API (FASE 4): analyze -> drive -> sheets -> resposta,
e idempotência quando a MESMA mensagem do Telegram chega duas vezes."""

import base64

import pytest
from fastapi.testclient import TestClient

import api
from schemas.extraction import ProductExtraction
from services.product_agent_runner import ProductAgentRunner
from services.product_registry import ProductRegistry
from tools import (
    AnalyzeProductImageTool,
    CreateProductRecordTool,
    FakeDriveBackend,
    FakeSheetsBackend,
    SaveProductImageTool,
    ValidateProductTool,
)

from .test_product_agent import FakeChatModel

IMG_B64 = base64.b64encode(b"fake-image").decode()

COMPLETE = ProductExtraction(
    name="Camisa feminina",
    description="Camisa feminina azul",
    color="Azul",
    size="M",
    wholesale_price=35.0,
    retail_price=59.90,
)


@pytest.fixture()
def client_and_backends(tmp_path, monkeypatch):
    drive = FakeDriveBackend()
    sheets = FakeSheetsBackend()
    runner = ProductAgentRunner(
        db_path=tmp_path / "cp.db",
        analyze_tool=AnalyzeProductImageTool(chat_model=FakeChatModel(COMPLETE)),
        validate_tool=ValidateProductTool(),
        save_image_tool=SaveProductImageTool(backend=drive),
        create_record_tool=CreateProductRecordTool(backend=sheets),
        registry=ProductRegistry(db_path=":memory:"),
    )
    monkeypatch.setattr(api, "agent", runner)
    return TestClient(api.app), drive, sheets, runner


def _payload(**overrides):
    base = {
        "text": "Camisa feminina azul",
        "image_base64": IMG_B64,
        "telegram_chat_id": 12345,
        "telegram_message_id": 999,
    }
    base.update(overrides)
    return base


def test_full_flow_analyze_persists_and_confirms(client_and_backends):
    client, drive, sheets, _ = client_and_backends
    resp = client.post("/product/analyze", json=_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "complete"
    assert data["product_id"] == "P000001"  # ID sequencial
    assert data["drive_url"]
    assert data["drive_file_name"] == "P000001_camisa_feminina_original.jpg"
    assert data["sheet_row"] == 2

    # Drive: 1 arquivo na pasta /produtos novos
    assert len(drive.files) == 1
    # Sheets: header + 1 linha com os dados do produto
    assert len(sheets.rows) == 2
    row = sheets.rows[1]
    assert row[0] == "P000001"
    assert row[2] == "Camisa feminina"
    assert row[8] == data["drive_url"]


def test_duplicate_telegram_message_does_not_create_second_product(client_and_backends):
    client, drive, sheets, _ = client_and_backends

    first = client.post("/product/analyze", json=_payload())
    assert first.status_code == 200
    pid = first.json()["product_id"]

    # Mesma mensagem reprocessada (retry do n8n/Telegram)
    second = client.post("/product/analyze", json=_payload())
    assert second.status_code == 200
    assert second.json()["product_id"] == pid
    assert second.json().get("deduplicated") is True

    # Nada duplicado
    assert len(drive.files) == 1
    assert len(sheets.rows) == 2


def test_different_message_creates_next_sequential_id(client_and_backends):
    client, _, _, _ = client_and_backends
    client.post("/product/analyze", json=_payload())
    resp = client.post("/product/analyze", json=_payload(telegram_message_id=1000))
    assert resp.json()["product_id"] == "P000002"
