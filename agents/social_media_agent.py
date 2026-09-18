"""Social Media Agent — FASE 7.

Entrada: Campaign JSON (product_id, campaign_id, imagem, legenda
Instagram, legenda Facebook, legenda TikTok, hashtags).

Responsabilidades (na ordem do contrato):
  1. verificar quais redes estão habilitadas (settings + credenciais);
  2. adaptar o conteúdo quando necessário (regras por rede em
     tools.social_publish.adapt_caption);
  3. publicar — cada rede de forma INDEPENDENTE: se o Instagram falhar,
     Facebook e TikTok seguem normalmente;
  4. registrar o resultado por plataforma (PublicationRegistry):
     platform, post_id, published_at, status, error;
  5. tratar erros individualmente — uma rede com erro fica 'failed' com
     a mensagem, as demais não são interrompidas.

Status por plataforma: pending → publishing → published | failed.

Idempotência: antes de publicar, se já existe registro 'published' com
post_id para (campaign_id, platform), a publicação NÃO é refeita — o
registro existente é retornado (retry seguro).

Tokens: nunca no código — lidos de Settings (.env/ambiente). A tool de
Sheets é injetável (FakeSheetsBackend em dev/testes).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config import get_logger, get_settings
from schemas.social_publication import (
    PLATFORMS,
    CampaignPublishInput,
    PlatformPublication,
    PublishReport,
)
from services.publication_registry import PublicationRegistry
from tools.product_sheet import UpdateProductRecordTool
from tools.social_publish import (
    FacebookPageBackend,
    FakeSocialBackend,
    InstagramBusinessBackend,
    PublishToPlatformTool,
    SocialBackend,
    TikTokBackend,
    adapt_caption,
)

logger = get_logger("agent.social_media")


def _platform_caption(input: CampaignPublishInput, platform: str) -> str:
    return {
        "instagram": input.caption_instagram,
        "facebook": input.caption_facebook,
        "tiktok": input.caption_tiktok,
    }[platform]


class SocialMediaAgent:
    def __init__(
        self,
        publish_tools: dict[str, PublishToPlatformTool] | None = None,
        sheet_tool: UpdateProductRecordTool | None = None,
        registry: PublicationRegistry | None = None,
        registry_db_path: str | None = None,
    ) -> None:
        # Redes habilitadas = redes com tool configurada.
        self._tools = publish_tools if publish_tools is not None else {}
        self._sheet_tool = sheet_tool
        self._registry = registry or PublicationRegistry(registry_db_path)

    # 1. verificação de redes habilitadas -----------------------------------

    def enabled_platforms(self) -> list[str]:
        """Redes efetivamente habilitadas (tool configurada)."""
        return [p for p in PLATFORMS if p in self._tools]

    # fluxo principal --------------------------------------------------------

    def publish_campaign(self, input: CampaignPublishInput) -> PublishReport:
        publications: list[PlatformPublication] = []

        for platform in self.enabled_platforms():
            # idempotência: já publicado -> não republica
            if self._registry.already_published(input.campaign_id, platform):
                rec = self._registry.get(input.campaign_id, platform)
                publications.append(
                    PlatformPublication(
                        platform=platform,  # type: ignore[arg-type]
                        status="published",
                        post_id=rec["post_id"],
                        published_at=datetime.fromisoformat(rec["published_at"])
                        if rec["published_at"]
                        else None,
                        caption_used=rec["caption"],
                        attempts=rec["attempts"],
                    )
                )
                logger.info(
                    "já publicado — pulando",
                    extra={"platform": platform, "campaign_id": input.campaign_id},
                )
                continue

            # status: publishing (registro antes da tentativa)
            self._registry.upsert(
                input.campaign_id, platform, status="publishing",
                increment_attempts=True,
            )

            # 2. adaptação do conteúdo
            caption = adapt_caption(
                platform, _platform_caption(input, platform), input.hashtags
            )

            # 3. publicar (independente por rede)
            result = self._tools[platform].run(
                image_url=input.image_url,
                caption=caption,
                hashtags=input.hashtags,
            )

            # 4 + 5. registrar resultado / tratar erro individualmente
            if result.success:
                pub = PlatformPublication(
                    platform=platform,  # type: ignore[arg-type]
                    status="published",
                    post_id=result.data["post_id"],
                    published_at=datetime.fromisoformat(result.data["published_at"]),
                    caption_used=caption,
                )
                self._registry.upsert(
                    input.campaign_id, platform,
                    status="published",
                    post_id=result.data["post_id"],
                    published_at=result.data["published_at"],
                    error=None,
                    caption=caption,
                )
            else:
                pub = PlatformPublication(
                    platform=platform,  # type: ignore[arg-type]
                    status="failed",
                    error=result.error,
                    caption_used=caption,
                )
                self._registry.upsert(
                    input.campaign_id, platform,
                    status="failed",
                    error=result.error or "erro desconhecido",
                    caption=caption,
                )
                logger.warning(
                    "rede falhou — demais seguem",
                    extra={"platform": platform, "error": result.error},
                )
            publications.append(pub)

        report = PublishReport(
            campaign_id=input.campaign_id,
            product_id=input.product_id,
            publications=publications,
        )

        # Atualiza Google Sheets: consolida status por rede na coluna `status`
        if self._sheet_tool is not None:
            summary = self._sheet_status_summary(report)
            sheet = self._sheet_tool.run(
                product_id=input.product_id, updates={"status": summary}
            )
            if not sheet.success:
                logger.warning(
                    "falha ao atualizar Sheets (não bloqueia publicação)",
                    extra={"error": sheet.error, "product_id": input.product_id},
                )

        return report

    @staticmethod
    def _sheet_status_summary(report: PublishReport) -> str:
        parts = [f"{p.platform}:{p.status}" for p in report.publications]
        return "publicado [" + ", ".join(parts) + "]" if parts else "sem redes habilitadas"


# ---------------------------------------------------------------------------
# Fábrica — monta o agente a partir das Settings (produção) ou fakes (dev)
# ---------------------------------------------------------------------------

def build_social_agent(
    *,
    use_real_backends: bool | None = None,
    sheet_tool: UpdateProductRecordTool | None = None,
    registry_db_path: str | None = None,
) -> SocialMediaAgent:
    """Monta o SocialMediaAgent conforme as redes habilitadas no .env.

    Em dev (APP_ENV != prod) e sem tokens, usa FakeSocialBackend — a
    lógica completa roda sem rede. Em prod, exige o token da rede
    habilitada; rede habilitada sem credencial completa NÃO é incluída
    (log de aviso) em vez de derrubar o agente.
    """
    settings = get_settings()
    real = use_real_backends if use_real_backends is not None else settings.is_prod

    tools: dict[str, PublishToPlatformTool] = {}

    def _add(platform: str, backend: SocialBackend) -> None:
        tools[platform] = PublishToPlatformTool(platform, backend)

    if settings.social_enable_instagram:
        if real:
            try:
                _add("instagram", InstagramBusinessBackend(
                    settings.meta_access_token, settings.instagram_business_account_id
                ))
            except ValueError as exc:
                logger.warning("instagram não configurado", extra={"error": str(exc)})
        else:
            _add("instagram", FakeSocialBackend("instagram"))

    if settings.social_enable_facebook:
        if real:
            try:
                _add("facebook", FacebookPageBackend(
                    settings.meta_access_token, settings.facebook_page_id
                ))
            except ValueError as exc:
                logger.warning("facebook não configurado", extra={"error": str(exc)})
        else:
            _add("facebook", FakeSocialBackend("facebook"))

    if settings.social_enable_tiktok:
        if real:
            try:
                _add("tiktok", TikTokBackend(settings.tiktok_access_token))
            except ValueError as exc:
                logger.warning("tiktok não configurado", extra={"error": str(exc)})
        else:
            _add("tiktok", FakeSocialBackend("tiktok"))

    return SocialMediaAgent(
        publish_tools=tools,
        sheet_tool=sheet_tool,
        registry_db_path=registry_db_path,
    )
