"""Wiring produção: conecta a camada Repository aos pontos de entrada.

O padrão é opt-in via `database_url` — quem não passa nada continua no
SQLite legado (dev/testes intactos, lógica dos agentes intocada). Em
produção, a API (ou o arranque do serviço) chama estas fábricas com a
DATABASE_URL e injeta o resultado nos services/agents via os parâmetros
de injeção que JÁ existem (registry=..., sheet_tool=..., backend=...).

Nada aqui altera agentes: apenas fornece objetos que satisfazem as
mesmas interfaces que eles já consomem.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from config import get_logger

from .factory import Repositories, build_repositories

logger = get_logger("repositories.wiring")


# ---------------------------------------------------------------------------
# Checkpointer LangGraph: PostgreSQL em prod, SQLite como antes
# ---------------------------------------------------------------------------


def build_checkpointer(
    database_url: str | None = None,
    sqlite_path: str = "data/product_agent_checkpoints.db",
) -> Any:
    """Checkpointer do ProductAgentRunner.

    Com `database_url` usa PostgresSaver (mesma interface
    BaseCheckpointSaver do SqliteSaver — o runner não muda). Sem URL,
    mantém o SqliteSaver atual.
    """
    if database_url:
        from langgraph.checkpoint.postgres import PostgresSaver

        saver = PostgresSaver.from_conn_string(database_url)
        saver.setup()
        logger.info("checkpointer LangGraph: PostgreSQL")
        return saver

    from pathlib import Path

    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    logger.info("checkpointer LangGraph: SQLite (%s)", path)
    return SqliteSaver(conn)


# ---------------------------------------------------------------------------
# Fábricas de services/agents com os repositórios injetados
# ---------------------------------------------------------------------------


def build_product_agent_runner(
    repos: Repositories,
    database_url: str | None = None,
    **kwargs: Any,
) -> Any:
    """ProductAgentRunner com o ProductRepository e checkpointer da camada.

    Aceita os mesmos kwargs do runner original (analyze_tool etc.). Quando
    o backend é PostgreSQL e nenhum create_record_tool foi passado, injeta
    as tools de Sheets apoiadas no PostgresSheetsBackend — o grafo grava
    no banco sem mudança de lógica.
    """
    from services.product_agent_runner import ProductAgentRunner

    kwargs.setdefault("registry", repos.products)
    kwargs.setdefault("checkpointer", build_checkpointer(database_url))
    if repos.backend == "postgres":
        from tools.product_sheet import CreateProductRecordTool

        from .factory import PostgresSheetsBackend

        sheets = PostgresSheetsBackend(repos.products)
        kwargs.setdefault("create_record_tool", CreateProductRecordTool(sheets))
    return ProductAgentRunner(**kwargs)


def build_sheet_tools(repos: Repositories) -> dict[str, Any]:
    """As 4 tools de Sheets apoiadas no banco (postgres) ou no backend
    padrão (sqlite dev — FakeSheetsBackend, comportamento atual)."""
    from tools.product_sheet import (
        CreateProductRecordTool,
        GetLatestProductTool,
        GetProductTool,
        UpdateProductRecordTool,
    )

    backend = None
    if repos.backend == "postgres":
        from .factory import PostgresSheetsBackend

        backend = PostgresSheetsBackend(repos.products)
    return {
        "create": CreateProductRecordTool(backend),
        "update": UpdateProductRecordTool(backend),
        "get": GetProductTool(backend),
        "latest": GetLatestProductTool(backend),
    }


def build_recampaign_service(repos: Repositories, **kwargs: Any) -> Any:
    """RecampaignService com Product/Campaign repositories injetados."""
    from services.recampaign_service import RecampaignService

    kwargs.setdefault("product_registry", repos.products)
    kwargs.setdefault("campaign_registry", repos.campaigns)
    if repos.backend == "postgres":
        kwargs.setdefault("sheet_tool", build_sheet_tools(repos)["update"])
    return RecampaignService(**kwargs)


def build_social_media_agent(repos: Repositories, **kwargs: Any) -> Any:
    from agents.social_media_agent import SocialMediaAgent

    kwargs.setdefault("registry", repos.social_posts)
    if repos.backend == "postgres":
        kwargs.setdefault("sheet_tool", build_sheet_tools(repos)["update"])
    return SocialMediaAgent(**kwargs)


def build_image_generation_agent(repos: Repositories, **kwargs: Any) -> Any:
    from agents.image_generation_agent import ImageGenerationAgent

    kwargs.setdefault("campaign_registry", repos.campaigns)
    return ImageGenerationAgent(**kwargs)
