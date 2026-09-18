"""API do serviço — recebe o webhook do n8n.

Endpoints principais:
    POST /message          -> porta de entrada única (comando/aprovação/intake)
    POST /product/analyze  -> inicia o intake de um novo produto
    POST /product/respond  -> continua o fluxo com a resposta do usuário
    POST /recampaign/daily -> rotina diária 08:00
    POST /social/publish   -> publicação nas redes habilitadas
    POST /retry            -> reprocessamento de operações falhas
    Headers: X-Webhook-Secret: <N8N_WEBHOOK_SECRET>

Subir em dev:
    uvicorn api:app --reload --port 8000
"""

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from config import configure_logging, get_logger, get_settings
from schemas.social_publication import CampaignPublishInput
from services.operation_registry import OperationRegistry, OperationStatus
from services.reliability import MaxAttemptsExceeded, ServiceName
from services.product_agent_runner import ProductAgentRunner

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
logger = get_logger("api")

app = FastAPI(title="Product Marketing System", version="0.8.0")
agent = ProductAgentRunner()
operations = OperationRegistry()


def verify_webhook_secret(x_webhook_secret: str = Header(default="")) -> None:
    """Autentica chamadas do n8n. Se N8N_WEBHOOK_SECRET estiver vazio
    (dev local), a verificação é desligada — nunca em produção."""
    expected = settings.n8n_webhook_secret
    if expected and x_webhook_secret != expected:
        raise HTTPException(status_code=401, detail="webhook secret inválido")


class ErrorHandlerRequest(BaseModel):
    """Payload do workflow de Error Handler do n8n."""
    workflow_id: str | None = None
    workflow_name: str | None = None
    execution_id: str | None = None
    error: str
    error_type: str | None = None
    node_name: str | None = None
    service: str | None = None
    product_id: str | None = None
    campaign_id: str | None = None
    correlation_id: str | None = None


@app.post("/errors/report", dependencies=[Depends(verify_webhook_secret)])
def report_error(payload: ErrorHandlerRequest) -> dict:
    """Chamado pelo Error Handler workflow do n8n — dead-letter + log."""
    op = operations.create(
        kind=f"n8n:{payload.workflow_name or payload.workflow_id or 'workflow'}",
        service=payload.service,
        stage=payload.node_name,
        product_id=payload.product_id,
        campaign_id=payload.campaign_id,
        correlation_id=payload.correlation_id,
        payload={
            "workflow_id": payload.workflow_id,
            "execution_id": payload.execution_id,
        },
    )
    op = operations.start(op["operation_id"])
    op = operations.fail(
        op["operation_id"],
        error=payload.error,
        error_type=payload.error_type,
        service=payload.service,
        stage=payload.node_name,
    )
    logger.error(
        "erro reportado pelo n8n",
        extra={
            "correlation_id": op["correlation_id"],
            "product_id": payload.product_id,
            "campaign_id": payload.campaign_id,
            "stage": payload.node_name,
        },
    )
    return {"operation_id": op["operation_id"], "status": op["status"]}


class RetryRequest(BaseModel):
    ref: str
    from_stage: str | None = None


@app.post("/retry", dependencies=[Depends(verify_webhook_secret)])
def retry_operation(payload: RetryRequest) -> dict:
    """Equivalente a `/retry P000001`: reenfileira a operação falha."""
    try:
        op = operations.retry(payload.ref, from_stage=payload.from_stage)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "operation_id": op["operation_id"],
        "status": op["status"],
        "correlation_id": op["correlation_id"],
        "message": "Operação reenfileirada para reprocessamento.",
    }


@app.get("/operations/dead-letter", dependencies=[Depends(verify_webhook_secret)])
def list_dead_letters() -> dict:
    return {"dead_letters": operations.list_dead_letters()}


@app.get("/operations/{operation_id}", dependencies=[Depends(verify_webhook_secret)])
def get_operation(operation_id: str) -> dict:
    op = operations.get(operation_id)
    if not op:
        raise HTTPException(status_code=404, detail="Operação não encontrada.")
    return op


@app.get("/executions/summary", dependencies=[Depends(verify_webhook_secret)])
def executions_summary() -> dict:
    return operations.summary()


class MessageRequest(BaseModel):
    text: str = ""
    image_base64: str | None = None
    telegram_chat_id: int
    telegram_message_id: int


class AnalyzeRequest(BaseModel):
    text: str = ""
    image_base64: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


@app.post("/product/analyze", dependencies=[Depends(verify_webhook_secret)])
def analyze_product(payload: AnalyzeRequest) -> dict:
    if not payload.text and not payload.image_base64:
        raise HTTPException(status_code=400, detail="Envie texto e/ou imagem.")

    # Idempotência: mesma mensagem do Telegram já processada.
    if payload.telegram_chat_id and payload.telegram_message_id:
        existing_id = agent.find_by_message(
            payload.telegram_chat_id, payload.telegram_message_id
        )
        if existing_id:
            logger.info(
                "mensagem duplicada, retornando produto existente",
                extra={"product_id": existing_id},
            )
            state = agent.get_state(existing_id) or {}
            state["deduplicated"] = True
            return state

    logger.info("análise solicitada", extra={"agent": "product", "stage": "intake"})
    result = agent.start(
        user_id=str(payload.telegram_chat_id or "unknown"),
        telegram_chat_id=payload.telegram_chat_id or 0,
        message_id=payload.telegram_message_id,
        intake_text=payload.text,
        image_base64=payload.image_base64,
    )
    result["telegram_chat_id"] = payload.telegram_chat_id
    result["telegram_message_id"] = payload.telegram_message_id
    return result


class RespondRequest(BaseModel):
    message: str
    product_id: str | None = None
    telegram_chat_id: int | None = None


@app.post("/message", dependencies=[Depends(verify_webhook_secret)])
def handle_message(payload: MessageRequest) -> dict:
    """Porta de entrada única para o n8n: decide comando / aprovação /
    intake vs. resposta."""
    if not payload.text and not payload.image_base64:
        raise HTTPException(status_code=400, detail="Envie texto e/ou imagem.")

    # 1. Comandos de controle (/status, /ultimo, /produto, /republicar, /cancelar)
    if payload.text and payload.text.strip().startswith("/"):
        from services.telegram_commands import TelegramCommandService

        command_service = TelegramCommandService()
        response = command_service.handle(
            payload.text, payload.telegram_chat_id
        )
        if response:
            response["telegram_chat_id"] = payload.telegram_chat_id
            return response

    # 2. Aprovação pendente de campanha (AUTO_PUBLISH=false)
    if payload.text and not payload.image_base64:
        from services.approval_service import ApprovalService

        approval_service = ApprovalService()
        response = approval_service.handle_decision(
            payload.telegram_chat_id, payload.text
        )
        if response:
            response["telegram_chat_id"] = payload.telegram_chat_id
            return response

    # Idempotência: mesma mensagem do Telegram já processada.
    existing_id = agent.find_by_message(
        payload.telegram_chat_id, payload.telegram_message_id
    )
    if existing_id:
        logger.info(
            "mensagem duplicada, retornando produto existente",
            extra={"product_id": existing_id},
        )
        state = agent.get_state(existing_id) or {}
        state["deduplicated"] = True
        state["telegram_chat_id"] = payload.telegram_chat_id
        return state

    # Sem imagem e há produto aguardando resposta neste chat -> resume.
    if not payload.image_base64:
        pending_id = agent.find_pending_product(payload.telegram_chat_id)
        if pending_id:
            logger.info(
                "resposta do usuário (rota automática)",
                extra={"product_id": pending_id},
            )
            result = agent.resume(pending_id, payload.text)
            result["telegram_chat_id"] = payload.telegram_chat_id
            return result

    # Intake de produto novo — operação rastreada.
    op = operations.create(
        kind="product_intake",
        service=ServiceName.TELEGRAM.value,
        stage="intake",
        payload={
            "telegram_chat_id": payload.telegram_chat_id,
            "telegram_message_id": payload.telegram_message_id,
            "has_image": bool(payload.image_base64),
        },
    )
    operations.start(op["operation_id"])
    logger.info(
        "análise solicitada",
        extra={
            "agent": "product",
            "stage": "intake",
            "correlation_id": op["correlation_id"],
        },
    )
    try:
        result = agent.start(
            user_id=str(payload.telegram_chat_id),
            telegram_chat_id=payload.telegram_chat_id,
            message_id=payload.telegram_message_id,
            intake_text=payload.text,
            image_base64=payload.image_base64,
        )
    except Exception as exc:
        operations.fail(
            op["operation_id"],
            error=str(exc),
            error_type=type(exc).__name__,
            service=ServiceName.LANGGRAPH.value,
            stage="product_agent",
        )
        raise HTTPException(status_code=500, detail=f"Falha no intake: {exc}") from exc

    product_id = result.get("product_id")
    if product_id:
        with operations._lock, operations._connect() as conn:
            conn.execute(
                "UPDATE operations SET product_id = ? WHERE operation_id = ?",
                (product_id, op["operation_id"]),
            )
    if result.get("status") == "awaiting_user":
        operations.mark_waiting_user(op["operation_id"])
    elif result.get("status") in ("complete", "completed"):
        operations.complete(
            op["operation_id"], result={"product_id": product_id}
        )
    result["correlation_id"] = op["correlation_id"]
    result["operation_id"] = op["operation_id"]
    result["telegram_chat_id"] = payload.telegram_chat_id
    result["telegram_message_id"] = payload.telegram_message_id
    return result


@app.post("/product/respond", dependencies=[Depends(verify_webhook_secret)])
def respond_product(payload: RespondRequest) -> dict:
    """Continua a thread do produto com a resposta do usuário."""
    product_id = payload.product_id
    if not product_id:
        if payload.telegram_chat_id is None:
            raise HTTPException(
                status_code=400, detail="Informe product_id ou telegram_chat_id."
            )
        product_id = agent.find_pending_product(payload.telegram_chat_id)
        if not product_id:
            raise HTTPException(
                status_code=404,
                detail="Nenhum produto aguardando resposta neste chat.",
            )

    if not agent.get_state(product_id):
        raise HTTPException(status_code=404, detail="Produto não encontrado.")

    logger.info("resposta do usuário", extra={"product_id": product_id})
    result = agent.resume(product_id, payload.message)
    result["telegram_chat_id"] = payload.telegram_chat_id
    return result


@app.get("/product/state/{product_id}", dependencies=[Depends(verify_webhook_secret)])
def product_state(product_id: str) -> dict:
    state = agent.get_state(product_id)
    if not state:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    return state


# --- FASE 10: comandos de controle e aprovação -------------------------------

@app.get("/commands", dependencies=[Depends(verify_webhook_secret)])
def list_commands() -> dict:
    return {
        "auto_publish": settings.auto_publish,
        "commands": {
            "/status": "Resumo do sistema (operações, último produto/campanha).",
            "/ultimo": "Ficha do último produto cadastrado.",
            "/produto P000001": "Ficha do produto + campanhas recentes.",
            "/republicar P000001": "Cria nova campanha para o produto e roda o pipeline.",
            "/cancelar P000001": "Cancela o produto e campanhas não publicadas.",
        },
        "approval": {
            "question": "Publicar esta campanha?",
            "options": ["PUBLICAR", "CANCELAR", "REFAZER"],
            "active_when": "AUTO_PUBLISH=false",
        },
    }


class CommandRequest(BaseModel):
    text: str
    telegram_chat_id: int


@app.post("/commands", dependencies=[Depends(verify_webhook_secret)])
def run_command(payload: CommandRequest) -> dict:
    from services.telegram_commands import TelegramCommandService

    response = TelegramCommandService().handle(payload.text, payload.telegram_chat_id)
    if not response:
        raise HTTPException(status_code=400, detail="Texto não é um comando válido.")
    response["telegram_chat_id"] = payload.telegram_chat_id
    return response


class ApprovalDecisionRequest(BaseModel):
    decision: str
    telegram_chat_id: int


@app.post("/campaign/approve", dependencies=[Depends(verify_webhook_secret)])
def approve_campaign(payload: ApprovalDecisionRequest) -> dict:
    from services.approval_service import ApprovalService

    response = ApprovalService().handle_decision(
        payload.telegram_chat_id, payload.decision
    )
    if not response:
        raise HTTPException(
            status_code=404,
            detail="Nenhuma campanha aguardando aprovação neste chat.",
        )
    response["telegram_chat_id"] = payload.telegram_chat_id
    return response


@app.get("/campaign/pending", dependencies=[Depends(verify_webhook_secret)])
def pending_campaigns() -> dict:
    from services.campaign_registry import CampaignRegistry

    registry = CampaignRegistry()
    registry._ensure_approvals_table()
    rows = registry._conn.execute(
        "SELECT * FROM campaign_approvals WHERE status = 'pending' "
        "ORDER BY id DESC"
    ).fetchall()
    return {"pending": [dict(r) for r in rows]}


# --- FASE 8: rotina diária 08:00 --------------------------------------------

class RecampaignRequest(BaseModel):
    run_date: str | None = None


@app.post("/recampaign/daily", dependencies=[Depends(verify_webhook_secret)])
def recampaign_daily(payload: RecampaignRequest) -> dict:
    """Rotina autônoma das 08:00."""
    from services.recampaign_service import RecampaignService

    op = operations.create(
        kind="daily_recampaign",
        stage="recampaign",
        payload={"run_date": payload.run_date},
    )
    operations.start(op["operation_id"])
    service = RecampaignService()
    logger.info(
        "rotina diária solicitada",
        extra={"run_date": payload.run_date, "correlation_id": op["correlation_id"]},
    )
    try:
        result = service.run_daily(run_date=payload.run_date)
    except ValueError as exc:
        operations.fail(
            op["operation_id"],
            error=str(exc),
            error_type=type(exc).__name__,
            dead_letter=False,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        operations.fail(
            op["operation_id"],
            error=str(exc),
            error_type=type(exc).__name__,
            service=ServiceName.LANGGRAPH.value,
            stage="recampaign",
        )
        raise HTTPException(status_code=500, detail=f"Falha na rotina: {exc}") from exc

    if result.get("product_id") or result.get("campaign_id"):
        with operations._lock, operations._connect() as conn:
            conn.execute(
                "UPDATE operations SET product_id = ?, campaign_id = ? "
                "WHERE operation_id = ?",
                (
                    result.get("product_id"),
                    result.get("campaign_id")
                    or (result.get("campaign") or {}).get("campaign_id"),
                    op["operation_id"],
                ),
            )
    operations.complete(
        op["operation_id"], result={"status": result.get("status")}
    )
    result["correlation_id"] = op["correlation_id"]
    return result


@app.get("/recampaign/status", dependencies=[Depends(verify_webhook_secret)])
def recampaign_status() -> dict:
    from services.campaign_registry import CampaignRegistry

    return {"runs": CampaignRegistry().list_daily_runs()}


# --- FASE 7: Social Media Agent ---------------------------------------------

@app.post("/social/publish", dependencies=[Depends(verify_webhook_secret)])
def social_publish(payload: CampaignPublishInput) -> dict:
    """Publica a campanha nas redes habilitadas (cada rede independente)."""
    from agents.social_media_agent import build_social_agent

    if not payload.image_url and not payload.image_base64:
        raise HTTPException(status_code=400, detail="Informe image_url ou image_base64.")

    social = build_social_agent()
    if not social.enabled_platforms():
        raise HTTPException(
            status_code=409,
            detail="Nenhuma rede social habilitada/configurada.",
        )

    op = operations.create(
        kind="social_publish",
        service="social",
        stage="publishing",
        product_id=payload.product_id,
        campaign_id=payload.campaign_id,
    )
    operations.start(op["operation_id"])
    logger.info(
        "publicação solicitada",
        extra={
            "campaign_id": payload.campaign_id,
            "product_id": payload.product_id,
            "correlation_id": op["correlation_id"],
            "platforms": social.enabled_platforms(),
        },
    )
    report = social.publish_campaign(payload)
    data = report.model_dump(mode="json")
    if report.all_published:
        operations.complete(op["operation_id"], result={"status": "published"})
    else:
        failed_platforms = [
            p.get("platform")
            for p in data.get("publications", [])
            if p.get("status") == "failed"
        ]
        operations.fail(
            op["operation_id"],
            error=f"Falha nas plataformas: {failed_platforms}",
            error_type="PartialPublishError",
            service="social",
            stage="publishing",
        )
    data["correlation_id"] = op["correlation_id"]
    data["operation_id"] = op["operation_id"]
    return data


@app.get("/social/status/{campaign_id}", dependencies=[Depends(verify_webhook_secret)])
def social_status(campaign_id: str) -> dict:
    from services.publication_registry import PublicationRegistry

    registry = PublicationRegistry()
    records = registry.list_by_campaign(campaign_id)
    if not records:
        raise HTTPException(status_code=404, detail="Campanha sem publicações registradas.")
    return {"campaign_id": campaign_id, "publications": records}
