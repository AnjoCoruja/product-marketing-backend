"""RecampaignService — rotina diária autônoma das 08:00 (FASE 8).

Fluxo (executado UMA vez por dia, protegido por `daily_runs`):

1. consultar produtos — há produto novo cadastrado desde ontem?
   - sim -> `new_product_recent`: nenhuma campanha extra é criada (o
     produto novo já terá a campanha inicial do fluxo principal);
   - não -> seleciona o último produto lançado (`reason: recampaign_08h`).
2. sem produto disponível -> encerra normalmente com `no_product`.
3. verificar campanhas recentes do produto -> o fingerprint do conteúdo
   gerado é comparado: igual a alguma campanha existente -> rejeitada
   (nunca reutilizar a mesma campanha).
4. criar nova campanha com campaign_id próprio sequencial (C000001,
   C000002, ...) — sempre novo, mesmo para o mesmo produto.
5. pipeline: Marketing Agent -> nova imagem -> Social Media Agent ->
   publicação nas redes habilitadas -> registrar campanha e o
   resultado da execução no registry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from config import get_logger, get_settings
from schemas.social_publication import CampaignPublishInput
from services.campaign_registry import CampaignRegistry
from services.product_registry import ProductRegistry

logger = get_logger("service.recampaign")

RECENT_CAMPAIGNS_LOOKBACK = 5


def campaign_fingerprint(campaign: dict[str, Any]) -> str:
    """Hash estável do conteúdo textual da campanha.

    Duas campanhas com o mesmo conceito/legendas/hashtags são "a mesma
    campanha" para efeito da regra de não-reutilização — independente
    do campaign_id. Acentos e caixa são normalizados para não tratar
    variação cosmética como campanha nova.
    """

    def _norm(value: Any) -> Any:
        if isinstance(value, str):
            text = unicodedata.normalize("NFKD", value)
            text = "".join(c for c in text if not unicodedata.combining(c))
            return " ".join(text.lower().split())
        if isinstance(value, list):
            return [_norm(v) for v in value]
        return value

    payload = {
        "concept": _norm(campaign.get("marketing_concept", "")),
        "headline": _norm(campaign.get("headline", "")),
        "instagram": _norm(campaign.get("caption_instagram", "")),
        "facebook": _norm(campaign.get("caption_facebook", "")),
        "tiktok": _norm(campaign.get("caption_tiktok", "")),
        "hashtags": sorted(_norm(campaign.get("hashtags", []))),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class RecampaignService:
    def __init__(
        self,
        *,
        product_registry: ProductRegistry | None = None,
        campaign_registry: CampaignRegistry | None = None,
        runner: Any = None,
        marketing_agent: Any = None,
        image_agent: Any = None,
        social_agent: Any = None,
        drive_backend: Any = None,
        sheet_tool: Any = None,
        now: Any = None,
    ) -> None:
        self.product_registry = product_registry or ProductRegistry()
        self.campaign_registry = campaign_registry or CampaignRegistry()
        self._runner = runner
        self._marketing_agent = marketing_agent
        self._image_agent = image_agent
        self._social_agent = social_agent
        self._drive_backend = drive_backend
        self._sheet_tool = sheet_tool
        self._now = now  # callable -> datetime (testável)

    # ------------------------------------------------------------------
    # dependências preguiçosas (produção): montadas só quando usadas,
    # lendo as credenciais do .env — nunca no código.
    # ------------------------------------------------------------------

    def _get_runner(self):
        if self._runner is None:
            from services.product_agent_runner import ProductAgentRunner

            self._runner = ProductAgentRunner()
        return self._runner

    def _get_drive_backend(self):
        if self._drive_backend is None:
            from tools.save_product_image import FakeDriveBackend, GoogleDriveBackend

            settings = get_settings()
            if settings.google_service_account_json:
                self._drive_backend = GoogleDriveBackend(
                    settings.google_service_account_json,
                    settings.google_drive_root_folder_id,
                )
            else:
                logger.warning(
                    "GOOGLE_SERVICE_ACCOUNT_JSON ausente — Drive fake em uso"
                )
                self._drive_backend = FakeDriveBackend()
        return self._drive_backend

    def _get_sheet_tool(self):
        if self._sheet_tool is None:
            from tools.product_sheet import UpdateProductRecordTool

            self._sheet_tool = UpdateProductRecordTool()
        return self._sheet_tool

    def _get_marketing_agent(self):
        if self._marketing_agent is None:
            from agents.marketing_agent import MarketingAgent

            self._marketing_agent = MarketingAgent()
        return self._marketing_agent

    def _get_image_agent(self):
        if self._image_agent is None:
            from agents.image_generation_agent import ImageGenerationAgent
            from tools.generate_marketing_image import GenerateMarketingImageTool
            from tools.save_marketing_image import SaveMarketingImageTool

            self._image_agent = ImageGenerationAgent(
                generate_tool=GenerateMarketingImageTool(),
                save_tool=SaveMarketingImageTool(backend=self._get_drive_backend()),
                update_sheet_tool=self._get_sheet_tool(),
                campaign_registry=self.campaign_registry,
            )
        return self._image_agent

    def _get_social_agent(self):
        if self._social_agent is None:
            from agents.social_media_agent import build_social_agent

            self._social_agent = build_social_agent(sheet_tool=self._get_sheet_tool())
        return self._social_agent

    # ------------------------------------------------------------------
    # fluxo principal
    # ------------------------------------------------------------------

    def run_daily(self, run_date: str | None = None) -> dict[str, Any]:
        now = self._now() if self._now else datetime.now(timezone.utc)
        run_date = run_date or now.date().isoformat()
        logger.info("rotina diária iniciada", extra={"run_date": run_date})

        # --- consultar produtos -----------------------------------------
        latest = self.product_registry.latest()
        if not latest:
            started = self.campaign_registry.start_daily_run(
                run_date, reason="no_product"
            )
            if started:
                self.campaign_registry.finish_daily_run(
                    run_date, status="completed", detail="nenhum produto cadastrado"
                )
            logger.info("rotina encerrada: sem produto disponível")
            return {
                "run_date": run_date,
                "status": "no_product",
                "reason": "no_product",
                "message": "Nenhum produto disponível — rotina encerrada normalmente.",
                "log": self._log(run_date),
            }

        product_id = latest["product_id"]
        created_at = _parse_dt(latest.get("created_at"))
        is_new = bool(created_at and created_at >= now - timedelta(hours=24))

        if is_new:
            started = self.campaign_registry.start_daily_run(
                run_date, reason="new_product_recent", product_id=product_id
            )
            if started:
                self.campaign_registry.finish_daily_run(
                    run_date,
                    status="completed",
                    detail=f"produto novo cadastrado em {latest.get('created_at')}",
                    product_id=product_id,
                )
            logger.info(
                "rotina encerrada: produto novo no período",
                extra={"product_id": product_id},
            )
            return {
                "run_date": run_date,
                "status": "skipped",
                "reason": "new_product_recent",
                "product_id": product_id,
                "product_created_at": latest.get("created_at"),
                "message": "Produto novo no período — nenhuma recampanha criada.",
                "log": self._log(run_date),
            }

        # --- proteção contra duplicação da execução diária --------------
        if not self.campaign_registry.start_daily_run(
            run_date, reason="recampaign_08h", product_id=product_id
        ):
            existing = self.campaign_registry.get_daily_run(run_date) or {}
            logger.info(
                "rotina do dia já executada — duplicada bloqueada",
                extra={"run_date": run_date},
            )
            return {
                "run_date": run_date,
                "status": "skipped",
                "reason": "duplicate_run",
                "product_id": existing.get("product_id"),
                "campaign_id": existing.get("campaign_id"),
                "message": "A rotina de hoje já foi executada — duplicação bloqueada.",
                "log": self._log(run_date),
            }

        try:
            result = self._create_and_publish(run_date, product_id)
        except Exception as exc:  # noqa: BLE001
            self.campaign_registry.finish_daily_run(
                run_date, status="failed", detail=str(exc), product_id=product_id
            )
            logger.exception(
                "rotina diária falhou", extra={"run_date": run_date, "error": str(exc)}
            )
            raise

        self.campaign_registry.finish_daily_run(
            run_date,
            status="completed",
            detail=(
                "recampanha aguardando aprovação"
                if result.get("status") == "awaiting_approval"
                else "recampanha publicada"
            ),
            product_id=product_id,
            campaign_id=result["campaign"]["campaign_id"],
        )
        result["log"] = self._log(run_date)
        return result

    # ------------------------------------------------------------------
    # republicação manual (comando /republicar P000001)
    # ------------------------------------------------------------------

    def recampaign_product(self, product_id: str) -> dict[str, Any]:
        """Cria uma NOVA campanha (campaign_id novo) para um produto já
        cadastrado e roda o pipeline completo — usada pelo comando
        /republicar. Respeita AUTO_PUBLISH como a rotina diária.
        """
        if not self.product_registry.get(product_id):
            raise ValueError(f"Produto {product_id} não encontrado.")
        result = self._create_and_publish(run_date=None, product_id=product_id)
        result.pop("run_date", None)
        return result

    # ------------------------------------------------------------------
    # criar campanha -> imagem -> publicação -> registro
    # ------------------------------------------------------------------

    def _create_and_publish(self, run_date: str | None, product_id: str) -> dict[str, Any]:
        # dados completos do produto (o estado conversacional tem preços,
        # descrição etc.; o registry local guarda só ponteiros)
        state = self._get_runner().get_state(product_id) or {}
        product: dict[str, Any] = {"product_id": product_id}
        for field in (
            "name",
            "description",
            "color",
            "size",
            "wholesale_price",
            "retail_price",
        ):
            value = state.get(field)
            if value is not None:
                product[field] = value
        if not product.get("name"):
            product["name"] = self.product_registry.get(product_id).get("name")
        state_chat_id = state.get("telegram_chat_id")

        # --- verificar campanhas recentes ---------------------------------
        recent = self.campaign_registry.recent_campaigns(
            product_id, limit=RECENT_CAMPAIGNS_LOOKBACK
        )
        previous_concepts = [
            r["fingerprint"] for r in recent if r.get("fingerprint")
        ]
        previous_ids = [r["campaign_id"] for r in recent]
        logger.info(
            "campanhas recentes verificadas",
            extra={"product_id": product_id, "recent_campaigns": previous_ids},
        )

        # --- criar nova campanha (campaign_id próprio) ---------------------
        campaign_id = self.campaign_registry.next_campaign_id()
        marketing = self._get_marketing_agent()

        campaign: dict[str, Any] | None = None
        fingerprint = ""
        last_error: str | None = None
        for attempt in (1, 2):
            hint = dict(product)
            if previous_concepts:
                hint["previous_campaign_concepts"] = (
                    "Conceitos de campanhas JÁ USADOS para este produto "
                    f"(hashes): {previous_concepts}. Crie uma campanha com "
                    "ângulo DIFERENTE — nunca reutilize a mesma campanha."
                )
            try:
                candidate = marketing.create_campaign(hint)
            except ValueError as exc:
                last_error = str(exc)
                logger.warning(
                    "geração de campanha falhou (tentativa %d): %s", attempt, exc
                )
                continue

            fingerprint = campaign_fingerprint(candidate)
            if fingerprint in previous_concepts:
                last_error = "campanha idêntica a uma recente — rejeitada"
                logger.warning(
                    "campanha duplicada rejeitada (tentativa %d)", attempt
                )
                continue
            candidate["campaign_id"] = campaign_id
            campaign = candidate
            break

        if campaign is None:
            raise ValueError(
                f"Não foi possível criar uma campanha inédita: {last_error}"
            )

        # registra a campanha (com conteúdo) ANTES dos efeitos colaterais
        self.campaign_registry.create(
            campaign_id,
            product_id,
            fingerprint=fingerprint,
            status="generating_assets",
            content=campaign,
        )

        # --- nova imagem (foto original como referência) -------------------
        drive_file_id = (self.product_registry.get(product_id) or {}).get(
            "drive_file_id"
        ) or state.get("drive_file_id")
        if not drive_file_id:
            self.campaign_registry.update_status(campaign_id, "failed")
            raise ValueError(
                f"Produto {product_id} sem foto original no Drive — "
                "imagem de marketing não pode ser gerada."
            )
        original_b64 = base64.b64encode(
            self._get_drive_backend().download_file(drive_file_id)
        ).decode("ascii")

        image = self._get_image_agent().generate_for_campaign(
            campaign_id=campaign_id,
            product=product,
            original_image_base64=original_b64,
            image_prompt=campaign["image_prompt"],
            marketing_concept=campaign["marketing_concept"],
        )
        self.campaign_registry.update_status(campaign_id, "assets_ready")

        # --- aprovação humana (AUTO_PUBLISH=false) ------------------------
        settings = get_settings()
        if not settings.auto_publish:
            self.campaign_registry.update_status(campaign_id, "awaiting_approval")
            chat_id = (
                state_chat_id
                or (self.product_registry.get(product_id) or {}).get("telegram_chat_id")
                or (settings.allowed_chat_ids[0] if settings.allowed_chat_ids else None)
            )
            if chat_id is not None:
                self.campaign_registry.create_approval(campaign_id, chat_id)
            logger.info(
                "AUTO_PUBLISH=false — campanha aguardando aprovação",
                extra={"campaign_id": campaign_id, "product_id": product_id},
            )
            return {
                "run_date": run_date,
                "status": "awaiting_approval",
                "reason": "recampaign_08h",
                "product_id": product_id,
                "product": product,
                "campaign": campaign,
                "fingerprint": fingerprint,
                "image": image,
                "platforms": [],
                "publish_report": None,
                "message": f"Nova campanha {campaign_id} criada para {product_id} "
                "— aguardando aprovação no Telegram.",
            }

        # --- publicação (Social Media Agent) -------------------------------
        social = self._get_social_agent()
        platforms = social.enabled_platforms()
        publish_report: dict[str, Any] | None = None
        if platforms:
            report = social.publish_campaign(
                CampaignPublishInput(
                    product_id=product_id,
                    campaign_id=campaign_id,
                    image_url=image["drive_marketing_url"],
                    caption_instagram=campaign["caption_instagram"],
                    caption_facebook=campaign["caption_facebook"],
                    caption_tiktok=campaign["caption_tiktok"],
                    hashtags=campaign["hashtags"],
                )
            )
            publish_report = report.model_dump(mode="json")
            final = "published" if report.all_published else "published_partial"
            self.campaign_registry.update_status(campaign_id, final)
        else:
            self.campaign_registry.update_status(campaign_id, "assets_ready")
            logger.info("nenhuma rede habilitada — campanha pronta, sem publicação")

        logger.info(
            "rotina diária concluída",
            extra={
                "run_date": run_date,
                "product_id": product_id,
                "campaign_id": campaign_id,
                "platforms": platforms,
            },
        )
        return {
            "run_date": run_date,
            "status": "published" if platforms else "assets_ready",
            "reason": "recampaign_08h",
            "product_id": product_id,
            "campaign": campaign,
            "fingerprint": fingerprint,
            "image": image,
            "platforms": platforms,
            "publish_report": publish_report,
            "message": f"Nova campanha {campaign_id} criada para {product_id} e "
            + ("publicada." if platforms else "pronta (sem redes habilitadas)."),
        }

    def _log(self, run_date: str) -> dict[str, Any] | None:
        """Resultado registrado da execução (log persistido no registry)."""
        return self.campaign_registry.get_daily_run(run_date)
