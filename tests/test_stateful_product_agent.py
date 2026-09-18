"""Testes do Product Agent stateful (FASE 3) — grafo LangGraph com
checkpointer persistente (SQLite em arquivo temporário) e interrupt/resume.

Cenários cobertos:
- produto completo (sem perguntas);
- produto incompleto (pergunta -> resposta completa);
- resposta parcial (loop de perguntas);
- resposta com múltiplos campos ("M, 35, 59,90");
- usuário enviando outra mensagem (resume extra / sem thread pendente);
- erro de interpretação (preço inválido);
- conversa interrompida e retomada (novo runner, mesmo banco SQLite).
"""

from pathlib import Path

import pytest

from schemas.extraction import ProductExtraction
from services.product_agent_runner import ProductAgentRunner
from services.product_registry import ProductRegistry
from tools import AnalyzeProductImageTool, ValidateProductTool

from .test_product_agent import FakeChatModel


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


def make_runner(db_path: Path, extraction: ProductExtraction = INCOMPLETE) -> ProductAgentRunner:
    analyze = AnalyzeProductImageTool(chat_model=FakeChatModel(extraction))
    return ProductAgentRunner(
        db_path=db_path,
        analyze_tool=analyze,
        validate_tool=ValidateProductTool(),
        registry=ProductRegistry(db_path=":memory:"),
    )


import base64

_FAKE_IMAGE_B64 = base64.b64encode(b"fake-image").decode()


def start_kwargs(**overrides):
    base = {
        "user_id": "user-1",
        "telegram_chat_id": 12345,
        "message_id": 999,
        "intake_text": "Camisa feminina azul",
        "image_base64": _FAKE_IMAGE_B64,
    }
    base.update(overrides)
    return base


# --- Produto completo -----------------------------------------------------

def test_complete_product_no_questions(tmp_path):
    runner = make_runner(tmp_path / "cp.db", extraction=COMPLETE)
    result = runner.start(**start_kwargs())
    assert result["status"] == "complete"
    assert result["is_complete"] is True
    assert result["missing_fields"] == []
    assert result["size"] == "M"
    assert result["wholesale_price"] == 35.0
    assert result["retail_price"] == 59.90


# --- Produto incompleto: pergunta e resposta completa ---------------------

def test_incomplete_asks_and_completes_with_multi_field_answer(tmp_path):
    runner = make_runner(tmp_path / "cp.db")
    result = runner.start(**start_kwargs())
    assert result["status"] == "awaiting_user"
    assert set(result["missing_fields"]) == {"size", "wholesale_price", "retail_price"}
    assert "tamanho" in result["question"]
    assert "atacado" in result["question"]
    assert "varejo" in result["question"]

    # Usuário responde "M, 35, 59,90" — todos os campos de uma vez
    final = runner.resume(result["product_id"], "M, 35, 59,90")
    state = runner.get_state(result["product_id"])
    assert state["status"] == "complete"
    assert state["size"] == "M"
    assert state["wholesale_price"] == 35.0
    assert state["retail_price"] == 59.90
    assert state["missing_fields"] == []
    assert final is not None


# --- Resposta parcial: loop de perguntas ----------------------------------

def test_partial_response_loops_until_complete(tmp_path):
    runner = make_runner(tmp_path / "cp.db")
    result = runner.start(**start_kwargs())
    pid = result["product_id"]

    # Responde só o tamanho
    runner.resume(pid, "M")
    state = runner.get_state(pid)
    assert state["status"] == "awaiting_user"
    assert state["size"] == "M"
    assert set(state["missing_fields"]) == {"wholesale_price", "retail_price"}

    # Responde os dois preços
    runner.resume(pid, "35, 59,90")
    state = runner.get_state(pid)
    assert state["status"] == "complete"
    assert state["wholesale_price"] == 35.0
    assert state["retail_price"] == 59.90


# --- Erro de interpretação ------------------------------------------------

def test_unparseable_price_keeps_waiting_and_reports_error(tmp_path):
    runner = make_runner(tmp_path / "cp.db")
    result = runner.start(**start_kwargs())
    pid = result["product_id"]

    # "trinta e cinco" rotulado como atacado não converte -> erro explicado
    runner.resume(pid, "M, atacado trinta e cinco, 59,90")
    state = runner.get_state(pid)
    assert state["status"] == "awaiting_user"
    assert state["size"] == "M"
    assert state["retail_price"] == 59.90
    assert state["wholesale_price"] is None
    assert "atacado" in (state.get("pending_question") or "")

    # Usuário corrige
    runner.resume(pid, "35")
    state = runner.get_state(pid)
    assert state["status"] == "complete"


def test_extra_message_after_completion_raises(tmp_path):
    runner = make_runner(tmp_path / "cp.db", extraction=COMPLETE)
    result = runner.start(**start_kwargs())
    assert result["status"] == "complete"
    with pytest.raises(ValueError, match="não está aguardando"):
        runner.resume(result["product_id"], "mensagem perdida")


def test_resume_unknown_product_raises(tmp_path):
    runner = make_runner(tmp_path / "cp.db")
    with pytest.raises(ValueError, match="não encontrado"):
        runner.resume("produto-inexistente", "M")


# --- Conversa interrompida e retomada (persistência real) -----------------

def test_conversation_survives_runner_restart(tmp_path):
    db = tmp_path / "cp.db"
    runner = make_runner(db)
    result = runner.start(**start_kwargs())
    pid = result["product_id"]
    assert result["status"] == "awaiting_user"

    # Simula restart do processo: NOVO runner sobre o MESMO banco.
    runner2 = ProductAgentRunner(
        db_path=db,
        validate_tool=ValidateProductTool(),
        registry=ProductRegistry(db_path=":memory:"),
    )
    assert runner2.is_awaiting_user(pid)
    assert runner2.find_pending_product(12345) == pid

    runner2.resume(pid, "M, 35, 59,90")
    state = runner2.get_state(pid)
    assert state["status"] == "complete"
    assert state["size"] == "M"
    assert state["user_id"] == "user-1"
    assert state["telegram_chat_id"] == 12345
    assert state["created_at"]
    assert state["updated_at"] >= state["created_at"]


# --- Labeled answers ------------------------------------------------------

def test_labeled_multi_field_answer(tmp_path):
    runner = make_runner(tmp_path / "cp.db")
    result = runner.start(**start_kwargs())
    pid = result["product_id"]

    runner.resume(pid, "tamanho M, atacado 35, varejo 59,90")
    state = runner.get_state(pid)
    assert state["status"] == "complete"
    assert state["size"] == "M"
    assert state["wholesale_price"] == 35.0
    assert state["retail_price"] == 59.90
