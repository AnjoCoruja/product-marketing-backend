"""Grafo stateful do Product Agent (FASE 3).

Fluxo:
    receive_product -> analyze_product -> validate_product
      -> check_missing_fields
          | completo   -> validate_complete_product -> END
          | incompleto -> ask_user -> wait_for_user (interrupt)
      -> (resume com a resposta do usuário)
          process_user_response -> update_product
          -> ainda falta / erro de interpretação -> ask_user (loop)
          -> completo -> validate_complete_product -> END

Persistência: o grafo é compilado com um checkpointer e cada produto
roda na thread `thread_id == product_id`. O nó `wait_for_user` usa
`interrupt()` (langgraph.types) — a run pausa gravando o checkpoint
e é retomada com `Command(resume=<mensagem do usuário>)`. Nenhum
agente novo é criado por mensagem: o estado inteiro vive no
checkpointer e o resume acontece sobre a mesma thread.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from schemas.extraction import PRODUCT_FIELDS, ProductExtraction
from schemas.product_state import ProductAgentState
from services.user_response_parser import build_missing_fields_question, parse_user_response
from tools import (
    AnalyzeProductImageTool,
    CreateProductRecordTool,
    SaveProductImageTool,
    ValidateProductTool,
)

STATUS_LABELS_PT = {
    "name": "nome",
    "description": "descrição",
    "color": "cor",
    "size": "tamanho",
    "wholesale_price": "preço de atacado",
    "retail_price": "preço de varejo",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extraction_from_state(state: ProductAgentState) -> ProductExtraction:
    return ProductExtraction(
        **{f: state.get(f) for f in PRODUCT_FIELDS}
    ).with_computed_missing()


def build_product_agent_graph(
    analyze_tool: AnalyzeProductImageTool | None = None,
    validate_tool: ValidateProductTool | None = None,
    save_image_tool: SaveProductImageTool | None = None,
    create_record_tool: CreateProductRecordTool | None = None,
    checkpointer: Any = None,
):
    analyze = analyze_tool or AnalyzeProductImageTool()
    validate = validate_tool or ValidateProductTool()
    save_image = save_image_tool or SaveProductImageTool()
    create_record = create_record_tool or CreateProductRecordTool()

    # --- Nós -------------------------------------------------------------

    def receive_product(state: ProductAgentState) -> dict:
        return {
            "current_node": "receive_product",
            "status": "received",
            "updated_at": _now(),
        }

    def analyze_product(state: ProductAgentState) -> dict:
        result = analyze.run(
            text=state.get("intake_text", ""),
            # No intake a foto chega em _image_base64 (transiente);
            # original_image só existe depois do save no Drive.
            image_base64=state.get("_image_base64") or state.get("original_image"),
        )
        if not result.success:
            return {
                "current_node": "analyze_product",
                "status": "failed",
                "parse_errors": state.get("parse_errors", []) + [result.error or "analyze falhou"],
                "updated_at": _now(),
            }
        extraction = ProductExtraction(**result.data).with_computed_missing()
        updates: dict[str, Any] = {
            "current_node": "analyze_product",
            "status": "analyzing",
            "missing_fields": list(extraction.missing_fields),
            "updated_at": _now(),
        }
        for f in PRODUCT_FIELDS:
            value = getattr(extraction, f)
            if value is not None:
                updates[f] = value
        return updates

    def validate_product(state: ProductAgentState) -> dict:
        validation = validate.run(extraction=_extraction_from_state(state).model_dump())
        if not validation.success:
            return {
                "current_node": "validate_product",
                "status": "failed",
                "parse_errors": state.get("parse_errors", []) + [validation.error or "validação falhou"],
                "updated_at": _now(),
            }
        return {
            "current_node": "validate_product",
            "missing_fields": validation.data["missing_fields"],
            "updated_at": _now(),
        }

    def check_missing_fields(state: ProductAgentState) -> dict:
        return {"current_node": "check_missing_fields", "updated_at": _now()}

    def ask_user(state: ProductAgentState) -> dict:
        errors = state.get("parse_errors", [])
        question = build_missing_fields_question(state.get("missing_fields", []))
        if errors:
            question = " ".join(errors) + " " + question
        return {
            "current_node": "ask_user",
            "status": "awaiting_user",
            "pending_question": question,
            "parse_errors": [],
            "result": {
                "success": True,
                "status": "awaiting_user",
                "product_id": state["product_id"],
                "missing_fields": state.get("missing_fields", []),
                "question": question,
            },
            "updated_at": _now(),
        }

    def wait_for_user(state: ProductAgentState) -> dict:
        # Pausa a run aqui. O checkpoint gravado neste ponto é o que
        # permite retomar depois. O valor retornado pelo interrupt() é
        # o `Command(resume=...)` enviado no resume.
        user_message = interrupt(
            {
                "type": "missing_fields",
                "product_id": state["product_id"],
                "telegram_chat_id": state.get("telegram_chat_id"),
                "missing_fields": state.get("missing_fields", []),
                "question": state.get("pending_question"),
            }
        )
        return {
            "current_node": "wait_for_user",
            "status": "processing",
            "intake_text": str(user_message),
            "updated_at": _now(),
        }

    def process_user_response(state: ProductAgentState) -> dict:
        parsed = parse_user_response(
            state.get("intake_text", ""),
            state.get("missing_fields", []),
        )
        errors = list(parsed.errors)
        if parsed.unparsed:
            errors.append(
                "Não entendi: " + ", ".join(f"'{t}'" for t in parsed.unparsed) + "."
            )
        return {
            "current_node": "process_user_response",
            "result": {"parsed_updates": parsed.updates, "parse_errors": errors},
            "updated_at": _now(),
        }

    def update_product(state: ProductAgentState) -> dict:
        parsed_result = state.get("result") or {}
        updates: dict[str, Any] = {
            "current_node": "update_product",
            "parse_errors": parsed_result.get("parse_errors", []),
            "result": None,
            "updated_at": _now(),
        }
        updates.update(parsed_result.get("parsed_updates", {}))
        merged = _extraction_from_state({**state, **updates})
        updates["missing_fields"] = list(merged.missing_fields)
        return updates

    def validate_complete_product(state: ProductAgentState) -> dict:
        extraction = _extraction_from_state(state)
        validation = validate.run(extraction=extraction.model_dump())
        if not validation.success or validation.data["missing_fields"]:
            return {
                "current_node": "validate_complete_product",
                "status": "awaiting_user",
                "missing_fields": validation.data.get("missing_fields", PRODUCT_FIELDS)
                if validation.success
                else PRODUCT_FIELDS,
                "parse_errors": state.get("parse_errors", [])
                + ([] if validation.success else [validation.error or "validação falhou"]),
                "updated_at": _now(),
            }
        result = {
            "success": True,
            "status": "complete",
            "product_id": state["product_id"],
            "is_complete": True,
            "missing_fields": [],
            **{f: getattr(extraction, f) for f in PRODUCT_FIELDS},
            # Preenchidos depois pelos nós de persistência quando o
            # estado retorna ao runner
            "drive_url": state.get("drive_url"),
            "drive_file_name": state.get("drive_file_name"),
            "sheet_row": state.get("sheet_row"),
        }
        return {
            "current_node": "validate_complete_product",
            "status": "complete",
            "missing_fields": [],
            "result": result,
            "updated_at": _now(),
        }

    # --- Persistência (FASE 4) --------------------------------------------

    def save_product_image(state: ProductAgentState) -> dict:
        """Salva a foto original no Drive (/produtos novos). Idempotente:
        se drive_file_id já existe no estado, não faz nada; senão a tool
        deduplica pelo nome determinístico do arquivo."""
        if state.get("drive_file_id"):
            return {"current_node": "save_product_image", "updated_at": _now()}
        if not state.get("_image_base64"):
            return {
                "current_node": "save_product_image",
                "status": "failed",
                "parse_errors": state.get("parse_errors", [])
                + ["Produto completo sem imagem original para salvar."],
                "updated_at": _now(),
            }
        result = save_image.run(
            product_id=state["product_id"],
            name=state.get("name"),
            image_base64=state["_image_base64"],
        )
        if not result.success:
            return {
                "current_node": "save_product_image",
                "status": "failed",
                "parse_errors": state.get("parse_errors", [])
                + [result.error or "save_product_image falhou"],
                "updated_at": _now(),
            }
        return {
            "current_node": "save_product_image",
            "drive_file_id": result.data["file_id"],
            "drive_url": result.data.get("web_view_link"),
            "drive_file_name": result.data["file_name"],
            "original_image": result.data.get("web_view_link"),
            # base64 não precisa mais circular pelo estado
            "_image_base64": None,
            "updated_at": _now(),
        }

    def create_product_record(state: ProductAgentState) -> dict:
        """Insere a linha do produto no Sheets (idempotente por product_id)."""
        if state.get("sheet_row"):
            return {"current_node": "create_product_record", "updated_at": _now()}
        product = {
            "product_id": state["product_id"],
            "name": state.get("name"),
            "description": state.get("description"),
            "color": state.get("color"),
            "size": state.get("size"),
            "wholesale_price": state.get("wholesale_price"),
            "retail_price": state.get("retail_price"),
            "drive_url": state.get("drive_url"),
            "status": "active",
            "created_at": state.get("created_at"),
        }
        result = create_record.run(product=product)
        if not result.success:
            return {
                "current_node": "create_product_record",
                "status": "failed",
                "parse_errors": state.get("parse_errors", [])
                + [result.error or "create_product_record falhou"],
                "updated_at": _now(),
            }
        return {
            "current_node": "create_product_record",
            "sheet_row": result.data["row_number"],
            "updated_at": _now(),
        }

    # --- Roteadores ------------------------------------------------------

    def route_after_validate(state: ProductAgentState) -> str:
        if state.get("status") == "failed":
            return END
        return "check_missing_fields"

    def route_final(state: ProductAgentState) -> str:
        if state.get("status") == "complete":
            return "save_product_image"
        if state.get("status") == "failed":
            return END
        return "ask_user"

    def route_after_save(state: ProductAgentState) -> str:
        if state.get("status") == "failed":
            return END
        return "create_product_record"

    def route_after_record(state: ProductAgentState) -> str:
        return END

    def route_missing(state: ProductAgentState) -> str:
        if state.get("missing_fields"):
            return "ask_user"
        return "validate_complete_product"

    def route_after_update(state: ProductAgentState) -> str:
        if state.get("missing_fields") or state.get("parse_errors"):
            return "ask_user"
        return "validate_complete_product"

    # --- Grafo -----------------------------------------------------------

    graph = StateGraph(ProductAgentState)
    graph.add_node("receive_product", receive_product)
    graph.add_node("analyze_product", analyze_product)
    graph.add_node("validate_product", validate_product)
    graph.add_node("check_missing_fields", check_missing_fields)
    graph.add_node("ask_user", ask_user)
    graph.add_node("wait_for_user", wait_for_user)
    graph.add_node("process_user_response", process_user_response)
    graph.add_node("update_product", update_product)
    graph.add_node("validate_complete_product", validate_complete_product)
    graph.add_node("save_product_image", save_product_image)
    graph.add_node("create_product_record", create_product_record)

    graph.set_entry_point("receive_product")
    graph.add_edge("receive_product", "analyze_product")
    graph.add_edge("analyze_product", "validate_product")
    graph.add_conditional_edges("validate_product", route_after_validate,
                                {"check_missing_fields": "check_missing_fields", END: END})
    graph.add_conditional_edges("check_missing_fields", route_missing,
                                {"ask_user": "ask_user",
                                 "validate_complete_product": "validate_complete_product"})
    graph.add_edge("ask_user", "wait_for_user")
    graph.add_edge("wait_for_user", "process_user_response")
    graph.add_edge("process_user_response", "update_product")
    graph.add_conditional_edges("update_product", route_after_update,
                                {"ask_user": "ask_user",
                                 "validate_complete_product": "validate_complete_product"})
    graph.add_conditional_edges("validate_complete_product", route_final,
                                {"ask_user": "ask_user",
                                 "save_product_image": "save_product_image", END: END})
    graph.add_conditional_edges("save_product_image", route_after_save,
                                {"create_product_record": "create_product_record", END: END})
    graph.add_conditional_edges("create_product_record", route_after_record,
                                {END: END})
    return graph.compile(checkpointer=checkpointer)
