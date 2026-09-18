"""Tools de publicação nas redes sociais — FASE 7 (Social Media Agent).

Uma tool por plataforma, todas atrás do mesmo contrato `SocialBackend`:

    publish(image_url, caption, hashtags) -> {"post_id": ..., "published_at": ...}

Backends:
- Reais (APIs OFICIAIS):
  * InstagramBusinessBackend — Instagram Graph API (container + publish,
    conta Business/Creator, imagem por URL pública);
  * FacebookPageBackend — Facebook Graph API (POST /{page-id}/photos);
  * TikTokBackend — TikTok Content Posting API (photo post via PULL_FROM_URL).
- FakeSocialBackend — dev/testes, sem rede.

Segurança: nenhum token no código. Os backends reais recebem o token por
construtor, lido de Settings (que lê .env/ambiente).

Adaptação de conteúdo por rede (`adapt_caption`):
- Instagram: caption + hashtags (até 30), limite 2200 chars;
- Facebook: caption + hashtags, limite ~63k chars;
- TikTok: caption curta (limite 2200, alvo 150) + hashtags compactadas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from config import get_logger
from tools.base import BaseTool, ToolResult

logger = get_logger("tools.social_publish")

GRAPH_BASE = "https://graph.facebook.com/v21.0"
TIKTOK_BASE = "https://open.tiktokapis.com/v2"


class PublishError(Exception):
    """Erro de publicação em UMA plataforma (não propaga para as outras)."""


class SocialBackend(Protocol):
    def publish(
        self, *, image_url: str, caption: str, hashtags: list[str]
    ) -> dict[str, Any]:
        """Publica e retorna {"post_id": str, "published_at": iso str}."""
        ...


# ---------------------------------------------------------------------------
# Adaptação de conteúdo por plataforma
# ---------------------------------------------------------------------------

_IG_CAPTION_MAX = 2200
_IG_HASHTAG_MAX = 30
_FB_CAPTION_MAX = 63206
_FB_HASHTAG_MAX = 10
_TT_CAPTION_TARGET = 150
_TT_CAPTION_MAX = 2200


def _normalize_hashtag(tag: str) -> str:
    t = tag.strip().lstrip("#").replace(" ", "")
    return f"#{t}" if t else ""


def adapt_caption(platform: str, caption: str, hashtags: list[str]) -> str:
    """Adapta legenda + hashtags às regras práticas de cada rede."""
    tags = [_normalize_hashtag(h) for h in hashtags]
    tags = [t for t in tags if t]

    if platform == "instagram":
        block = " ".join(tags[:_IG_HASHTAG_MAX])
        text = caption.strip()
        suffix = f"\n\n{block}" if block else ""
        return (text + suffix)[:_IG_CAPTION_MAX]

    if platform == "facebook":
        block = " ".join(tags[:_FB_HASHTAG_MAX])
        text = caption.strip()
        suffix = f"\n\n{block}" if block else ""
        return (text + suffix)[:_FB_CAPTION_MAX]

    if platform == "tiktok":
        block = " ".join(tags[:_IG_HASHTAG_MAX])
        text = caption.strip()
        if len(text) > _TT_CAPTION_TARGET:
            cut = text[:_TT_CAPTION_TARGET].rsplit(" ", 1)[0]
            text = cut.rstrip(".,;:!") + "…"
        combined = f"{text} {block}".strip()
        return combined[:_TT_CAPTION_MAX]

    return caption.strip()


# ---------------------------------------------------------------------------
# Backends reais — APIs oficiais
# ---------------------------------------------------------------------------

class InstagramBusinessBackend:
    """Instagram Graph API — conta Business/Creator.

    Fluxo oficial de foto única:
      1. POST /{ig-user-id}/media      (image_url + caption) -> container_id
      2. POST /{ig-user-id}/media_publish (creation_id)    -> post_id
    """

    def __init__(self, access_token: str, ig_user_id: str) -> None:
        if not access_token or not ig_user_id:
            raise ValueError(
                "Instagram habilitado exige META_ACCESS_TOKEN e "
                "INSTAGRAM_BUSINESS_ACCOUNT_ID."
            )
        self._token = access_token
        self._ig_user_id = ig_user_id

    def publish(self, *, image_url: str, caption: str, hashtags: list[str]) -> dict[str, Any]:
        if not image_url:
            raise PublishError(
                "Instagram exige image_url pública (a Graph API baixa a imagem)."
            )
        with httpx.Client(timeout=30.0) as client:
            try:
                container = client.post(
                    f"{GRAPH_BASE}/{self._ig_user_id}/media",
                    data={
                        "image_url": image_url,
                        "caption": caption,
                        "access_token": self._token,
                    },
                )
                container.raise_for_status()
                container_id = container.json()["id"]

                published = client.post(
                    f"{GRAPH_BASE}/{self._ig_user_id}/media_publish",
                    data={"creation_id": container_id, "access_token": self._token},
                )
                published.raise_for_status()
                post_id = published.json()["id"]
            except httpx.HTTPError as exc:
                raise PublishError(f"Instagram Graph API: {exc}") from exc

        return {"post_id": post_id, "published_at": datetime.now(timezone.utc).isoformat()}


class FacebookPageBackend:
    """Facebook Graph API — publicação de foto na Página.

    POST /{page-id}/photos (url + caption).
    """

    def __init__(self, access_token: str, page_id: str) -> None:
        if not access_token or not page_id:
            raise ValueError(
                "Facebook habilitado exige META_ACCESS_TOKEN e FACEBOOK_PAGE_ID."
            )
        self._token = access_token
        self._page_id = page_id

    def publish(self, *, image_url: str, caption: str, hashtags: list[str]) -> dict[str, Any]:
        if not image_url:
            raise PublishError("Facebook exige image_url pública.")
        with httpx.Client(timeout=30.0) as client:
            try:
                resp = client.post(
                    f"{GRAPH_BASE}/{self._page_id}/photos",
                    data={
                        "url": image_url,
                        "caption": caption,
                        "access_token": self._token,
                    },
                )
                resp.raise_for_status()
                post_id = resp.json()["post_id"]
            except httpx.HTTPError as exc:
                raise PublishError(f"Facebook Graph API: {exc}") from exc

        return {"post_id": post_id, "published_at": datetime.now(timezone.utc).isoformat()}


class TikTokBackend:
    """TikTok Content Posting API — foto postada via URL pública.

    POST /v2/post/publish/content/init/ com PULL_FROM_URL (o TikTok baixa
    a imagem da URL).
    """

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("TikTok habilitado exige TIKTOK_ACCESS_TOKEN.")
        self._token = access_token

    def publish(self, *, image_url: str, caption: str, hashtags: list[str]) -> dict[str, Any]:
        if not image_url:
            raise PublishError(
                "TikTok (foto) exige image_url pública (PULL_FROM_URL)."
            )
        body = {
            "post_info": {
                "title": caption[:90] or "Novo produto",
                "description": caption,
                "privacy_level": "PUBLIC_TO_EVERYONE",
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_cover_index": 0,
                "photo_images": [image_url],
            },
            "post_mode": "DIRECT_POST",
            "media_type": "PHOTO",
        }
        with httpx.Client(timeout=30.0) as client:
            try:
                resp = client.post(
                    f"{TIKTOK_BASE}/post/publish/content/init/",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json; charset=UTF-8",
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                post_id = data.get("publish_id", "")
            except httpx.HTTPError as exc:
                raise PublishError(f"TikTok Content Posting API: {exc}") from exc

        return {"post_id": post_id, "published_at": datetime.now(timezone.utc).isoformat()}


class FakeSocialBackend:
    """Backend em memória — dev/testes, nunca toca na rede."""

    def __init__(self, platform: str, fail: bool = False) -> None:
        self.platform = platform
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def publish(self, *, image_url: str, caption: str, hashtags: list[str]) -> dict[str, Any]:
        self.calls.append({"image_url": image_url, "caption": caption, "hashtags": hashtags})
        if self.fail:
            raise PublishError(f"Falha simulada em {self.platform}")
        n = len(self.calls)
        return {
            "post_id": f"fake_{self.platform}_{n:06d}",
            "published_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Tool por plataforma
# ---------------------------------------------------------------------------

class PublishToPlatformTool(BaseTool):
    """Publica em UMA plataforma. Falha aqui não afeta outras tools."""

    def __init__(self, platform: str, backend: SocialBackend) -> None:
        self.platform = platform
        self.name = f"publish_to_{platform}"
        self.description = f"Publica imagem + legenda no {platform} (API oficial)."
        self._backend = backend

    def run(
        self,
        *,
        image_url: str,
        caption: str,
        hashtags: list[str] | None = None,
        **_: Any,
    ) -> ToolResult:
        try:
            result = self._backend.publish(
                image_url=image_url, caption=caption, hashtags=hashtags or []
            )
            return ToolResult(success=True, data=result)
        except PublishError as exc:
            logger.warning(
                "publicação falhou", extra={"platform": self.platform, "error": str(exc)}
            )
            return ToolResult(success=False, error=str(exc), retryable=True)
        except Exception as exc:  # noqa: BLE001 — nunca derrubar as outras redes
            logger.exception(
                "erro inesperado na publicação", extra={"platform": self.platform}
            )
            return ToolResult(success=False, error=str(exc), retryable=False)
