"""Comandos de controle pelo Telegram (FASE 10).

Comandos suportados (texto começando com "/"):

  /status                -> resumo do sistema (operações + produto/campanha
                            mais recentes + AUTO_PUBLISH + redes habilitadas)
  /ultimo                -> ficha do último produto cadastrado
  /produto P000001       -> ficha do produto + campanhas recentes
  /republicar P000001    -> cria NOVA campanha (campaign_id novo) para o
                            produto e roda o pipeline Marketing -> imagem ->
                            (aprovação ou publicação direta)
  /cancelar P000001      -> cancela o produto (status cancelled) e as
                            campanhas dele que ainda não foram publicadas

Respostas são pt-BR prontas para enviar ao usuário via Telegram.
"""

from __future__ import annotations

import re
from typing import Any

from config import get_logger, get_settings
from services.campaign_registry import CampaignRegistry
from services.product_registry import ProductRegistry

logger = get_logger("service.telegram_commands")

COMMAND_RE = re.compile(r"^/(\w+)(?:\s+(\S+))?\s*$")


def format_brl(value: Any) -> str:
    """Formata número como moeda pt-BR (R$ 1.299,00)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else "-"
    text = f"{amount:,.2f}"
    text = text.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


class TelegramCommandService:
    def __init__(
        self,
        *,
        product_registry: ProductRegistry | None = None,
        campaign_registry: CampaignRegistry | None = None,
        runner: Any = None,
        operation_registry: Any = None,
        recampaign_service: Any = None,
    ) -> None:
        self.product_registry = product_registry or ProductRegistry()
        self.campaign_registry = campaign_registry or CampaignRegistry()
        self._runner = runner
        self._operation_registry = operation_registry
        self._recampaign_service = recampaign_service

    # ------------------------------------------------------------------

    def _get_runner(self):
        if self._runner is None:
            from services.product_agent_runner import ProductAgentRunner

            self._runner = ProductAgentRunner()
        return self._runner

    def _get_operations(self):
        if self._operation_registry is None:
            from services.operation_registry import OperationRegistry

            self._operation_registry = OperationRegistry()
        return self._operation_registry

    # ------------------------------------------------------------------
    # roteador
    # ------------------------------------------------------------------

    def handle(self, text: str, telegram_chat_id: int) -> dict[str, Any] | None:
        """Retorna a resposta do comando ou None se `text` não é comando."""
        match = COMMAND_RE.match((text or "").strip())
        if not match:
            return None
        command = match.group(1).lower()
        arg = match.group(2)
        logger.info(
            "comando recebido",
            extra={"command": command, "arg": arg, "chat_id": telegram_chat_id},
        )
        handler = {
            "status": self._cmd_status,
            "ultimo": self._cmd_ultimo,
            "produto": self._cmd_produto,
            "republicar": self._cmd_republicar,
            "cancelar": self._cmd_cancelar,
        }.get(command)
        if handler is None:
            return {
                "handled": True,
                "command": "unknown",
                "reply": (
                    "Comando não reconhecido. Disponíveis:\n"
                    "/status — resumo do sistema\n"
                    "/ultimo — último produto cadastrado\n"
                    "/produto P000001 — detalhes do produto\n"
                    "/republicar P000001 — nova campanha para o produto\n"
                    "/cancelar P000001 — cancelar produto"
                ),
            }
        return handler(arg, telegram_chat_id)

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    def _cmd_status(self, _arg: str | None, _chat_id: int) -> dict[str, Any]:
        settings = get_settings()
        summary = self._get_operations().summary()
        by_status = summary.get("by_status") or summary.get("statuses") or {}
        latest_product = self.product_registry.latest()
        latest_campaign = None
        if latest_product:
            latest_campaign = self.campaign_registry.latest_for_product(
                latest_product["product_id"]
            )

        lines = ["Status do sistema:"]
        if by_status:
            parts = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
            lines.append(f"- Operações: {parts}")
        else:
            lines.append("- Operações: nenhuma registrada")
        lines.append(f"- Dead-letter: {summary.get('dead_letters', 0)}")
        lines.append(
            "- Último produto: "
            + (f"{latest_product['product_id']} ({latest_product.get('name') or 'sem nome'})"
               if latest_product else "nenhum")
        )
        lines.append(
            "- Última campanha: "
            + (f"{latest_campaign['campaign_id']} [{latest_campaign['status']}]"
               if latest_campaign else "nenhuma")
        )
        lines.append(
            f"- Publicação automática (AUTO_PUBLISH): "
            f"{'ligada' if settings.auto_publish else 'desligada — aprovação no Telegram'}"
        )
        from agents.social_media_agent import build_social_agent

        platforms = build_social_agent().enabled_platforms()
        lines.append(
            "- Redes habilitadas: " + (", ".join(platforms) if platforms else "nenhuma")
        )
        return {"handled": True, "command": "status", "reply": "\n".join(lines)}

    # ------------------------------------------------------------------
    # /ultimo e /produto
    # ------------------------------------------------------------------

    def _cmd_ultimo(self, _arg: str | None, _chat_id: int) -> dict[str, Any]:
        latest = self.product_registry.latest()
        if not latest:
            return {
                "handled": True,
                "command": "ultimo",
                "reply": "Nenhum produto cadastrado ainda.",
            }
        return self._product_reply(latest["product_id"], command="ultimo")

    def _cmd_produto(self, arg: str | None, _chat_id: int) -> dict[str, Any]:
        if not arg:
            return {
                "handled": True,
                "command": "produto",
                "reply": "Informe o produto: /produto P000001",
            }
        product_id = arg.strip().upper()
        if not re.fullmatch(r"P\d{6}", product_id):
            return {
                "handled": True,
                "command": "produto",
                "reply": f"ID inválido: {arg}. Use o formato P000001.",
            }
        if not self.product_registry.get(product_id):
            return {
                "handled": True,
                "command": "produto",
                "reply": f"Produto {product_id} não encontrado.",
            }
        return self._product_reply(product_id, command="produto")

    def _product_reply(self, product_id: str, *, command: str) -> dict[str, Any]:
        registry_row = self.product_registry.get(product_id) or {}
        state = self._get_runner().get_state(product_id) or {}

        def pick(field: str, default: Any = "-") -> Any:
            value = state.get(field)
            return value if value is not None else registry_row.get(field, default) or default

        lines = [f"Produto {product_id}:"]
        lines.append(f"- Nome: {pick('name')}")
        lines.append(f"- Descrição: {pick('description')}")
        lines.append(f"- Cor: {pick('color')}")
        lines.append(f"- Tamanho: {pick('size')}")
        if state.get("wholesale_price") is not None:
            lines.append(f"- Atacado: {format_brl(state['wholesale_price'])}")
        if state.get("retail_price") is not None:
            lines.append(f"- Varejo: {format_brl(state['retail_price'])}")
        lines.append(f"- Status: {state.get('status') or registry_row.get('status')}")
        if registry_row.get("drive_url"):
            lines.append(f"- Foto original: {registry_row['drive_url']}")
        lines.append(f"- Cadastrado em: {registry_row.get('created_at', '-')}")

        campaigns = self.campaign_registry.recent_campaigns(product_id, limit=5)
        if campaigns:
            lines.append("- Campanhas recentes:")
            for c in campaigns:
                lines.append(f"  {c['campaign_id']} [{c['status']}]")
        else:
            lines.append("- Campanhas: nenhuma")

        return {
            "handled": True,
            "command": command,
            "product_id": product_id,
            "reply": "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # /republicar
    # ------------------------------------------------------------------

    def _cmd_republicar(self, arg: str | None, telegram_chat_id: int) -> dict[str, Any]:
        if not arg:
            return {
                "handled": True,
                "command": "republicar",
                "reply": "Informe o produto: /republicar P000001",
            }
        product_id = arg.strip().upper()
        product = self.product_registry.get(product_id)
        if not product:
            return {
                "handled": True,
                "command": "republicar",
                "reply": f"Produto {product_id} não encontrado.",
            }
        if product.get("status") == "cancelled":
            return {
                "handled": True,
                "command": "republicar",
                "product_id": product_id,
                "reply": f"Produto {product_id} está cancelado — não é possível republicar.",
            }

        service = self._recampaign_service
        if service is None:
            from services.recampaign_service import RecampaignService

            service = RecampaignService(
                product_registry=self.product_registry,
                campaign_registry=self.campaign_registry,
            )
        logger.info("republicação solicitada", extra={"product_id": product_id})
        try:
            result = service.recampaign_product(product_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("falha na republicação", extra={"product_id": product_id})
            return {
                "handled": True,
                "command": "republicar",
                "product_id": product_id,
                "status": "error",
                "reply": f"Falha ao republicar {product_id}: {exc}",
            }

        campaign = result["campaign"]
        image = result.get("image") or {}
        reply = (
            f"Nova campanha {campaign['campaign_id']} criada para {product_id}.\n"
            f"Título: {campaign.get('headline', '-')}"
        )
        response: dict[str, Any] = {
            "handled": True,
            "command": "republicar",
            "product_id": product_id,
            "campaign_id": campaign["campaign_id"],
            "campaign": campaign,
            "image": image,
            "status": result["status"],
        }
        if result["status"] == "awaiting_approval":
            self.campaign_registry.create_approval(
                campaign["campaign_id"], telegram_chat_id
            )
            response["preview"] = build_preview(product_id, campaign, image)
            response["reply"] = reply + "\n\nPrévia da campanha a seguir."
        else:
            report = result.get("publish_report") or {}
            published = [
                p.get("platform")
                for p in (report.get("publications") or [])
                if p.get("status") == "published"
            ]
            failed = [
                p.get("platform")
                for p in (report.get("publications") or [])
                if p.get("status") == "failed"
            ]
            if published:
                reply += f"\nPublicada em: {', '.join(published)}."
            if failed:
                reply += f"\nFalhou em: {', '.join(failed)}."
            if not published and not failed:
                reply += "\nCampanha pronta (nenhuma rede habilitada)."
            response["reply"] = reply
        return response

    # ------------------------------------------------------------------
    # /cancelar
    # ------------------------------------------------------------------

    def _cmd_cancelar(self, arg: str | None, _chat_id: int) -> dict[str, Any]:
        if not arg:
            return {
                "handled": True,
                "command": "cancelar",
                "reply": "Informe o produto: /cancelar P000001",
            }
        product_id = arg.strip().upper()
        product = self.product_registry.get(product_id)
        if not product:
            return {
                "handled": True,
                "command": "cancelar",
                "reply": f"Produto {product_id} não encontrado.",
            }
        if product.get("status") == "cancelled":
            return {
                "handled": True,
                "command": "cancelar",
                "product_id": product_id,
                "reply": f"Produto {product_id} já está cancelado.",
            }

        self.product_registry.update(product_id, status="cancelled")
        cancelled_campaigns: list[str] = []
        for c in self.campaign_registry.recent_campaigns(product_id, limit=50):
            if c["status"] in ("draft", "generating_assets", "assets_ready",
                               "awaiting_approval"):
                self.campaign_registry.update_status(c["campaign_id"], "cancelled")
                cancelled_campaigns.append(c["campaign_id"])
            pending = self.campaign_registry.get_pending_approval(
                campaign_id=c["campaign_id"]
            )
            if pending:
                self.campaign_registry.resolve_approval(
                    pending["id"], "cancelled", decision="CANCELAR_PRODUTO"
                )
        logger.info(
            "produto cancelado",
            extra={"product_id": product_id, "campaigns": cancelled_campaigns},
        )
        reply = f"Produto {product_id} cancelado."
        if cancelled_campaigns:
            reply += f" Campanhas canceladas: {', '.join(cancelled_campaigns)}."
        return {
            "handled": True,
            "command": "cancelar",
            "product_id": product_id,
            "status": "cancelled",
            "reply": reply,
        }


# ---------------------------------------------------------------------------
# prévia de campanha para aprovação (imagem + legenda + pergunta)
# ---------------------------------------------------------------------------

def build_preview(
    product_id: str, campaign: dict[str, Any], image: dict[str, Any]
) -> dict[str, Any]:
    """Payload da prévia enviada ao Telegram quando AUTO_PUBLISH=false.

    O n8n envia a imagem (image_url) + `caption_preview` e em seguida a
    `question` com as `options`.
    """
    caption_lines = [
        f"Campanha {campaign.get('campaign_id')} — {campaign.get('headline', '')}",
        "",
        campaign.get("caption_instagram", ""),
        "",
        " ".join(campaign.get("hashtags", [])),
    ]
    return {
        "product_id": product_id,
        "campaign_id": campaign.get("campaign_id"),
        "image_url": image.get("drive_marketing_url", ""),
        "caption_preview": "\n".join(caption_lines).strip(),
        "headline": campaign.get("headline", ""),
        "marketing_concept": campaign.get("marketing_concept", ""),
        "question": "Publicar esta campanha?",
        "options": ["PUBLICAR", "CANCELAR", "REFAZER"],
    }
