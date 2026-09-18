"""Schemas do Social Media Agent (FASE 7).

Entrada: Campaign JSON (product_id, campaign_id, imagem, legendas por
rede e hashtags). Saída: um PlatformPublication por rede habilitada,
com status independente (pending → publishing → published | failed).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


PlatformName = Literal["instagram", "facebook", "tiktok"]
PLATFORMS: tuple[PlatformName, ...] = ("instagram", "facebook", "tiktok")

PublicationState = Literal["pending", "publishing", "published", "failed"]


class CampaignPublishInput(BaseModel):
    """Contrato de entrada do Social Media Agent."""

    product_id: str
    campaign_id: str
    image_url: str = ""                      # URL pública da imagem (Drive)
    image_base64: str | None = None          # alternativa: bytes inline
    caption_instagram: str = ""
    caption_facebook: str = ""
    caption_tiktok: str = ""
    hashtags: list[str] = Field(default_factory=list)


class PlatformPublication(BaseModel):
    """Registro de UMA tentativa de publicação em UMA plataforma.

    Campos exigidos pelo contrato: platform, post_id, published_at,
    status, error. `post_id` é o identificador retornado pela API
    oficial da rede (None enquanto não publicado).
    """

    platform: PlatformName
    status: PublicationState = "pending"
    post_id: str | None = None
    published_at: datetime | None = None
    error: str | None = None
    caption_used: str = ""
    attempts: int = 0


class PublishReport(BaseModel):
    """Resultado agregado de uma rodada de publicação da campanha."""

    campaign_id: str
    product_id: str
    publications: list[PlatformPublication]

    @property
    def all_published(self) -> bool:
        return bool(self.publications) and all(
            p.status == "published" for p in self.publications
        )

    @property
    def any_failed(self) -> bool:
        return any(p.status == "failed" for p in self.publications)

    def by_platform(self, platform: PlatformName) -> PlatformPublication | None:
        return next((p for p in self.publications if p.platform == platform), None)
