"""ApprovalService — aprovação humana de campanhas via Telegram (FASE 10).

Ativo quando AUTO_PUBLISH=false. Fluxo:

1. A campanha é criada e a imagem gerada (RecampaignService).
2. O backend retorna `preview` (imagem + legenda) e registra uma
   aprovação pendente para o chat; o n8n envia a prévia e a pergunta
   "Publicar esta campanha?" com as opções PUBLICAR / CANCELAR / REFAZER.
3. A resposta do usuário chega como mensagem de texto normal: se há
   aprovação pendente no chat, o texto é interpretado como decisão.

Decisões (aceita variações comuns de digitação, sem depender de LLM):

- PUBLICAR  -> Social Media Agent publica nas redes habilitadas;
               campanha fica 'published' (ou 'published_partial').
- CANCELAR  -> campanha fica 'cancelled' ("campaign cancelled"); nada
               é publicado.
- REFAZER   -> campanha atual é cancelada ('redone') e uma NOVA
               campanha é gerada pelo Marketing Agent (conteúdo
               diferente, campaign_id novo), com nova imagem e nova
               prévia — o ciclo de aprovação recomeça.

Qualquer outro texto enquanto há aprovação pendente recebe a pergunta
repetida (não cai no fluxo de produto).
"""

from __future__ import annotations

import re
from typing import Any

from config import get_logger, get_settings
from schemas.social_publication import CampaignPublishInput
from services.campaign_registry import CampaignRegistry
from services.product_registry import ProductRegistry
from services.telegram_commands import build_preview

logger = get_logger("service.approval")

DECISIONS = ("PUBLICAR", "CANCELAR", "REFAZER")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(publicar?|publi|publish|ok|sim|aprovar?)$", re.I), "PUBLICAR"),
    (re.compile(r"^(cancelar?|cancel|nao|não|no)$", re.I), "CANCELAR"),
    (re.compile(r"^(refazer?|redo|refaça|refaca)$", re.I), "REFAZER"),
]


def parse_decision(text: str) -> str | None:
    normalized = " ".join((text or "").strip().split())
    for pattern, decision in _PATTERNS:
        if pattern.fullmatch(normalized):
            return decision
    return None


class ApprovalService:
    def __init__(
        self,
        *,
        product_registry: ProductRegistry | None = None,
        campaign_registry: CampaignRegistry | None = None,
        social_agent: Any = None,
        sheet_tool: Any = None,
        recampaign_service: Any = None,
    ) -> None:
        self.product_registry = product_registry or ProductRegistry()
        self.campaign_registry = campaign_registry or CampaignRegistry()
        self._social_agent = social_agent
        self._sheet_tool = sheet_tool
        self._recampaign_service = recampaign_service

    def _get_social_agent(self):
        if self._social_agent is None:
            from agents.social_media_agent import build_social_agent

            self._social_agent = build_social_agent(sheet_tool=self._get_sheet_tool())
        return self._social_agent

    def _get_sheet_tool(self):
        if self._sheet_tool is None:
            from tools.product_sheet import UpdateProductRecordTool

            self._sheet_tool = UpdateProductRecordTool()
        return self._sheet_tool

    # ------------------------------------------------------------------

    def has_pending(self, telegram_chat_id: int) -> bool:
        return (
            self.campaign_registry.get_pending_approval(chat_id=telegram_chat_id)
            is not None
        )

    def handle_decision(self, telegram_chat_id: int, text: str) -> dict[str, Any] | None:
        """Processa a decisão do usuário. Retorna None se não há aprovação
        pendente neste chat (a mensagem segue o fluxo normal)."""
        pending = self.campaign_registry.get_pending_approval(chat_id=telegram_chat_id)
        if not pending:
            return None

        decision = parse_decision(text)
        campaign_id = pending["campaign_id"]
        if decision is None:
            logger.info(
                "texto não reconhecido como decisão — repetindo pergunta",
                extra={"campaign_id": campaign_id, "text": text},
            )
            return {
                "handled": True,
                "command": "approval",
                "campaign_id": campaign_id,
                "status": "awaiting_approval",
                "reply": (
                    "Não entendi. Publicar esta campanha?\n"
                    "Responda: PUBLICAR, CANCELAR ou REFAZER"
                ),
            }

        campaign_row = self.campaign_registry.get(campaign_id) or {}
        product_id = campaign_row.get("product_id")
        logger.info(
            "decisão de aprovação",
            extra={"campaign_id": campaign_id, "decision": decision},
        )

        if decision == "CANCELAR":
            self.campaign_registry.update_status(campaign_id, "cancelled")
            self.campaign_registry.resolve_approval(
                pending["id"], "cancelled", decision=decision
            )
            self._update_sheet(product_id, f"campanha {campaign_id} cancelada")
            return {
                "handled": True,
                "command": "approval",
                "campaign_id": campaign_id,
                "status": "campaign_cancelled",
                "reply": f"Campanha {campaign_id} cancelada. Nada foi publicado.",
            }

        if decision == "PUBLICAR":
            return self._publish(pending, campaign_id, product_id)

        # REFAZER -> nova campanha via Marketing Agent + nova prévia
        self.campaign_registry.update_status(campaign_id, "redone")
        self.campaign_registry.resolve_approval(pending["id"], "redone", decision=decision)
        service = self._recampaign_service
        if service is None:
            from services.recampaign_service import RecampaignService

            service = RecampaignService(
                product_registry=self.product_registry,
                campaign_registry=self.campaign_registry,
            )
        try:
            result = service.recampaign_product(product_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("falha ao refazer campanha", extra={"campaign_id": campaign_id})
            return {
                "handled": True,
                "command": "approval",
                "campaign_id": campaign_id,
                "status": "error",
                "reply": f"Falha ao refazer a campanha: {exc}",
            }

        new_campaign = result["campaign"]
        image = result.get("image") or {}
        response: dict[str, Any] = {
            "handled": True,
            "command": "approval",
            "campaign_id": new_campaign["campaign_id"],
            "product_id": product_id,
            "campaign": new_campaign,
            "image": image,
            "status": result["status"],
        }
        if result["status"] == "awaiting_approval":
            self.campaign_registry.create_approval(
                new_campaign["campaign_id"], telegram_chat_id
            )
            response["preview"] = build_preview(product_id, new_campaign, image)
            response["reply"] = (
                f"Campanha refeita: {new_campaign['campaign_id']} "
                f"(a anterior {campaign_id} foi descartada).\n"
                "Prévia da nova campanha a seguir."
            )
        else:
            response["reply"] = (
                f"Campanha refeita e publicada: {new_campaign['campaign_id']}."
            )
        return response

    # ------------------------------------------------------------------

    def _publish(
        self, pending: dict[str, Any], campaign_id: str, product_id: str
    ) -> dict[str, Any]:
        campaign = self.campaign_registry.get_content(campaign_id) or {}
        images = self.campaign_registry.list_images(campaign_id)
        image_url = images[-1].get("drive_url") if images else ""
        if not image_url:
            self.campaign_registry.update_status(campaign_id, "failed")
            return {
                "handled": True,
                "command": "approval",
                "campaign_id": campaign_id,
                "status": "error",
                "reply": (
                    f"Campanha {campaign_id} sem imagem registrada — "
                    "não foi possível publicar. Use REFAZER."
                ),
            }

        social = self._get_social_agent()
        platforms = social.enabled_platforms()
        if not platforms:
            self.campaign_registry.update_status(campaign_id, "assets_ready")
            self.campaign_registry.resolve_approval(
                pending["id"], "published", decision="PUBLICAR"
            )
            return {
                "handled": True,
                "command": "approval",
                "campaign_id": campaign_id,
                "status": "assets_ready",
                "reply": (
                    f"Campanha {campaign_id} aprovada, mas nenhuma rede social "
                    "está habilitada — nada foi publicado."
                ),
            }

        self.campaign_registry.update_status(campaign_id, "publishing")
        report = social.publish_campaign(
            CampaignPublishInput(
                product_id=product_id,
                campaign_id=campaign_id,
                image_url=image_url,
                caption_instagram=campaign.get("caption_instagram", ""),
                caption_facebook=campaign.get("caption_facebook", ""),
                caption_tiktok=campaign.get("caption_tiktok", ""),
                hashtags=campaign.get("hashtags", []),
            )
        )
        final = "published" if report.all_published else "published_partial"
        self.campaign_registry.update_status(campaign_id, final)
        self.campaign_registry.resolve_approval(
            pending["id"], "published", decision="PUBLICAR"
        )
        self._update_sheet(product_id, f"campanha {campaign_id} {final}")

        data = report.model_dump(mode="json")
        published = [p["platform"] for p in data["publications"] if p["status"] == "published"]
        failed = [p["platform"] for p in data["publications"] if p["status"] == "failed"]
        reply = f"Campanha {campaign_id} publicada em: {', '.join(published)}."
        if failed:
            reply += f" Falhou em: {', '.join(failed)}."
        return {
            "handled": True,
            "command": "approval",
            "campaign_id": campaign_id,
            "product_id": product_id,
            "status": final,
            "publish_report": data,
            "reply": reply,
        }

    def _update_sheet(self, product_id: str | None, status: str) -> None:
        if not product_id:
            return
        try:
            result = self._get_sheet_tool().run(
                product_id=product_id, updates={"status": status}
            )
            if not result.success:
                logger.warning(
                    "Sheets não atualizado (não bloqueia)",
                    extra={"product_id": product_id, "error": result.error},
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "falha ao atualizar Sheets (não bloqueia)",
                extra={"product_id": product_id, "error": str(exc)},
            )
