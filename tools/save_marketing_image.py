"""Tool: save_marketing_image — Google Drive (pasta '/fotos geradas').

Salva a imagem de marketing gerada com nome determinístico:

    {product_id}_{campaign_id}_marketing_{seq:03d}.{ext}
    Ex.: P000001_C000001_marketing_001.jpg

Idempotência: mesmo (campaign_id, seq) → mesmo nome → retorna o
arquivo existente em vez de duplicar.
"""

from __future__ import annotations

import base64
from typing import Any

from .base import BaseTool, ToolResult
from .save_product_image import _EXT_BY_MIME, _DEFAULT_MIME, DriveBackend, FakeDriveBackend

FOTOS_GERADAS_FOLDER_NAME = "fotos geradas"


def build_marketing_filename(
    product_id: str,
    campaign_id: str,
    image_seq: int,
    mime_type: str = _DEFAULT_MIME,
) -> str:
    ext = _EXT_BY_MIME.get(mime_type.lower(), "jpg")
    return f"{product_id}_{campaign_id}_marketing_{image_seq:03d}.{ext}"


class SaveMarketingImageTool(BaseTool):
    name = "save_marketing_image"
    description = (
        "Salva a imagem gerada na pasta '/fotos geradas' do Drive com "
        "nome determinístico (product_id + campaign_id + seq). "
        "Idempotente por nome na pasta."
    )

    def __init__(self, backend: DriveBackend | None = None) -> None:
        self._backend = backend or FakeDriveBackend()

    def run(
        self,
        *,
        product_id: str,
        campaign_id: str,
        image_seq: int = 1,
        image_base64: str | None = None,
        mime_type: str = _DEFAULT_MIME,
        folder_id: str | None = None,
        **_: Any,
    ) -> ToolResult:
        if not image_base64:
            return ToolResult(
                success=False,
                error="save_marketing_image chamado sem imagem (image_base64 vazio).",
                retryable=False,
            )
        try:
            content = base64.b64decode(image_base64)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"image_base64 inválido: {exc}", retryable=False
            )

        target_folder = folder_id or self._backend.create_folder(
            FOTOS_GERADAS_FOLDER_NAME
        )
        filename = build_marketing_filename(product_id, campaign_id, image_seq, mime_type)

        existing = self._backend.find_file(filename, target_folder)
        record = existing or self._backend.upload_file(
            filename, content, mime_type, target_folder
        )
        return ToolResult(
            success=True,
            data={
                "file_id": record["id"],
                "file_name": filename,
                "web_view_link": record.get("web_view_link"),
                "folder_id": target_folder,
                "image_seq": image_seq,
                "deduplicated": existing is not None,
            },
        )
