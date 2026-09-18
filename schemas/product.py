"""Modelo de domínio: Product."""

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from .enums import Platform, ProductLifecycle


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProductSource(BaseModel):
    """Origem do cadastro (nesta fase: Telegram)."""

    telegram_message_id: int | None = None
    telegram_chat_id: int | None = None
    raw_text: str = ""
    original_photo_file_ids: list[str] = Field(default_factory=list)


class MediaAsset(BaseModel):
    """Arquivo de mídia ligado ao produto (foto original ou arte gerada).

    `type` já existe para acomodar vídeo (Video Agent, fase futura)
    sem mudança de schema.
    """

    asset_id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = "image"  # image | video (futuro)
    drive_file_id: str = ""
    platform: Platform | None = None
    prompt_used: str | None = None
    created_at: datetime = Field(default_factory=_now)


class ProductAssets(BaseModel):
    drive_folder_id: str = ""
    media: list[MediaAsset] = Field(default_factory=list)


class Product(BaseModel):
    product_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    category: str = ""
    description: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    price: float | None = None
    source: ProductSource = Field(default_factory=ProductSource)
    assets: ProductAssets = Field(default_factory=ProductAssets)
    lifecycle_status: ProductLifecycle = ProductLifecycle.ACTIVE
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
