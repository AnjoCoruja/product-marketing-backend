"""Testes end-to-end da integração das FASES 2 + 3 + 4.

Dois pipelines completos, exercitados como o n8n os exercita:

A) Produto incompleto:
   POST /product/analyze (foto+texto) -> status awaiting_user + pergunta
   -> n8n envia a pergunta no Telegram -> usuário responde
   -> POST /product/respond -> LangGraph atualizado -> validação
   -> Drive -> Sheets -> resposta de confirmação (texto final).

B) Produto completo em uma mensagem:
   POST /product/analyze -> completo -> Drive -> Sheets -> confirmação.
"""

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

IMG_B64 = base64.b64encode(b"foto-camisa").decode()

INCOMPLETE = ProductExtraction(
    name="Camisa feminina",
    description="Camisa feminina azul",
    color="Azul",
    size=None,
    wholesale_price=None,
    retail_price=None,
)

COMPLETE = INCOMPLETE.model_copy(
    update={"size": "M", "wholesale_price": 35.0, "retail_price": 59.90}
)


def format_confirmation(data: dict) -> str:
    """Mesmo formato que o n8n monta para a mensagem final."""

    def brl(v: float) -> str:
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    return (
        f"Produto {data['product_id']} cadastrado com sucesso.\n\n"
        f"Nome: {data['name']}\n"
        f"Cor: {data['color']}\n"
        f"Tamanho: {data['size']}\n"
        f"Atacado: R${brl(data['wholesale_price'])}\n"
        f"Varejo: R${brl(data['retail_price'])}\n\n"
        "Foto salva no Google Drive."
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    drive = FakeDriveBackend()
    sheets = FakeSheetsBackend()

    def build_runner(extraction: ProductExtraction) -> ProductAgentRunner:
        return ProductAgentRunner(
            db_path=tmp_path / "cp.db",
            analyze_tool=AnalyzeProductImageTool(chat_model=FakeChatModel(extraction)),
            validate_tool=ValidateProductTool(),
            save_image_tool=SaveProductImageTool(backend=drive),
            create_record_tool=CreateProductRecordTool(backend=sheets),
            registry=ProductRegistry(db_path=":memory:"),
        )

    class Env:
        def use(self, extraction: ProductExtraction) -> TestClient:
            monkeypatch.setattr(api, "agent", build_runner(extraction))
            return TestClient(api.app)

    e = Env()
    e.drive = drive
    e.sheets = sheets
    e.build_runner = build_runner
    return e


# --- Pipeline A: pergunta + resposta --------------------------------------


def test_pipeline_incomplete_question_response_persists_and_confirms(env):
    client = env.use(INCOMPLETE)

    r1 = client.post(
        "/product/analyze",
        json={
            "text": "Camisa feminina azul",
            "image_base64": IMG_B64,
            "telegram_chat_id": 12345,
            "telegram_message_id": 999,
        },
    )
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["status"] == "awaiting_user"
    assert d1["product_id"] == "P000001"
    assert set(d1["missing_fields"]) == {"size", "wholesale_price", "retail_price"}
    assert d1["question"]
    # nada persistiu ainda
    assert len(env.drive.files) == 0
    assert len(env.sheets.rows) == 1  # só header

    r2 = client.post(
        "/product/respond",
        json={"message": "tamanho M, atacado 35, varejo 59,90", "telegram_chat_id": 12345},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["status"] == "complete"
    assert d2["is_complete"] is True

    # Drive: foto salva em /produtos novos com nome determinístico
    assert len(env.drive.files) == 1
    file = next(iter(env.drive.files.values()))
    assert file["name"] == "P000001_camisa_feminina_original.jpg"
    assert env.drive.folders[file["folder_id"]]["name"] == "produtos novos"

    # Sheets: linha completa do produto
    assert len(env.sheets.rows) == 2
    row = env.sheets.rows[1]
    assert row[0] == "P000001"
    assert row[2] == "Camisa feminina"
    assert row[4] == "Azul"
    assert row[5] == "M"
    assert row[6] == 35.0
    assert row[7] == 59.90

    # Mensagem final de confirmação
    assert format_confirmation(d2) == (
        "Produto P000001 cadastrado com sucesso.\n\n"
        "Nome: Camisa feminina\n"
        "Cor: Azul\n"
        "Tamanho: M\n"
        "Atacado: R$35,00\n"
        "Varejo: R$59,90\n\n"
        "Foto salva no Google Drive."
    )


def test_pipeline_incomplete_two_response_rounds(env):
    """Resposta parcial -> nova pergunta -> completa -> Drive + Sheets."""
    client = env.use(INCOMPLETE)

    client.post(
        "/product/analyze",
        json={
            "text": "Camisa feminina azul",
            "image_base64": IMG_B64,
            "telegram_chat_id": 12345,
            "telegram_message_id": 999,
        },
    )
    r = client.post(
        "/product/respond",
        json={"message": "M", "telegram_chat_id": 12345},
    )
    d = r.json()
    assert d["status"] == "awaiting_user"
    assert set(d["missing_fields"]) == {"wholesale_price", "retail_price"}

    r = client.post(
        "/product/respond",
        json={"message": "35, 59,90", "telegram_chat_id": 12345},
    )
    assert r.json()["status"] == "complete"
    assert len(env.drive.files) == 1
    assert len(env.sheets.rows) == 2


def test_pipeline_survives_backend_restart_between_question_and_response(env, tmp_path):
    """O base64 da foto precisa sobreviver no checkpoint SQLite."""
    runner1 = env.build_runner(INCOMPLETE)
    state = runner1.start(
        user_id="12345",
        telegram_chat_id=12345,
        message_id=999,
        intake_text="Camisa feminina azul",
        image_base64=IMG_B64,
    )
    assert state["status"] == "awaiting_user"

    # "Restart": novo runner, mesmo SQLite de checkpoints
    runner2 = env.build_runner(INCOMPLETE)
    pid = runner2.find_pending_product(12345)
    assert pid == "P000001"
    state = runner2.resume(pid, "M, 35, 59,90")
    assert state["status"] == "complete"

    assert len(env.drive.files) == 1
    assert len(env.sheets.rows) == 2


# --- Pipeline B: produto completo de primeira ------------------------------


def test_pipeline_complete_single_message(env):
    client = env.use(COMPLETE)

    r = client.post(
        "/product/analyze",
        json={
            "text": "Camisa feminina azul M, atacado 35, varejo 59,90",
            "image_base64": IMG_B64,
            "telegram_chat_id": 12345,
            "telegram_message_id": 999,
        },
    )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "complete"
    assert len(env.drive.files) == 1
    assert len(env.sheets.rows) == 2
    assert format_confirmation(d).startswith(
        "Produto P000001 cadastrado com sucesso."
    )


def test_pipeline_message_without_pending_question_returns_404(env):
    """Mensagem do usuário sem produto aguardando."""
    client = env.use(COMPLETE)

    client.post(
        "/product/analyze",
        json={
            "text": "Camisa feminina azul M, 35, 59,90",
            "image_base64": IMG_B64,
            "telegram_chat_id": 12345,
            "telegram_message_id": 999,
        },
    )
    r = client.post(
        "/product/respond",
        json={"message": "obrigado!", "telegram_chat_id": 12345},
    )
    assert r.status_code in (400, 404, 422)
