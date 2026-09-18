"""Modelos de domínio: Campaign e SocialPost."""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from .enums import CampaignStatus, Platform, PublicationStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CampaignCopy(BaseModel):
    """Copy da campanha, com variação por plataforma."""

    instagram: str = ""
    facebook: str = ""
    tiktok: str = ""
    hashtags: list[str] = Field(default_factory=list)
    cta: str = ""


class SocialPost(BaseModel):
    """Registro de uma publicação (tentada ou realizada) em uma plataforma.

    `post_id` preenchido garante idempotência: antes de publicar,
    verifica-se se já existe SocialPost SUCCESS para (campaign, platform).
    """

    post_id: str = Field(default_factory=lambda: str(uuid4()))
    campaign_id: str
    platform: Platform
    external_post_id: str | None = None  # ID retornado pela rede social
    status: PublicationStatus = PublicationStatus.PENDING
    caption: str = ""
    media_asset_ids: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    created_at: datetime = Field(default_factory=_now)


class Campaign(BaseModel):
    model_config = {"populate_by_name": True}

    campaign_id: str = Field(default_factory=lambda: str(uuid4()))
    product_id: str
    created_by: str = "initial"  # initial | recampaign_08h
    concept: str = ""
    # alias "copy" mantém o nome do domínio no JSON sem conflitar com BaseModel.copy
    ad_copy: CampaignCopy = Field(default_factory=CampaignCopy, alias="copy")
    status: CampaignStatus = CampaignStatus.DRAFT
    publications: list[SocialPost] = Field(default_factory=list)
    approved_at: datetime | None = None
    approved_by_chat_id: int | None = None
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
