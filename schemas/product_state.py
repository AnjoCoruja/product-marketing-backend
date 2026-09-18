"""Estado do Product Agent stateful (LangGraph TypedDict).

Diferente do AgentState global (Pydantic, fundação), este estado é o
canal do grafo de intake do produto. É serializado pelo checkpointer
(SqliteSaver em produção/dev, MemorySaver em testes) a cada superstep,
o que permite interromper o fluxo no `wait_for_user` e retomar dias
depois exatamente de onde parou.

Chave de correlação: thread_id == product_id. Cada produto tem sua
própria thread no checkpointer — é assim que o sistema sabe qual
produto está aguardando resposta de qual chat.
"""

from typing import Any, TypedDict


class ProductAgentState(TypedDict, total=False):
    # Identidade / correlação
    user_id: str
    telegram_chat_id: int
    message_id: int | None
    product_id: str

    # Dados do produto (espelham ProductExtraction)
    name: str | None
    description: str | None
    color: str | None
    size: str | None
    wholesale_price: float | None
    retail_price: float | None

    # Referência da imagem original (ID/URL do Drive, nunca o base64).
    # O base64 trafega apenas no campo transitório `_image_base64` da
    # primeira run.
    original_image: str | None
    _image_base64: str | None

    # Persistência externa (FASE 4 — Drive + Sheets)
    drive_file_id: str | None
    drive_url: str | None
    sheet_row: int | None
    drive_file_name: str | None

    # Controle do fluxo
    missing_fields: list[str]
    current_node: str
    status: str  # received | analyzing | awaiting_user | processing | complete | failed

    # Última pergunta feita ao usuário e erros de interpretação da resposta
    pending_question: str | None
    parse_errors: list[str]

    # Entrada bruta inicial
    intake_text: str

    # Resultado final (para o n8n/Telegram)
    result: dict[str, Any] | None

    created_at: str
    updated_at: str
