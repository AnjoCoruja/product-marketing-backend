"""Tool: save_product_image — Google Drive.

Salva a foto original do produto na pasta `/produtos novos` do Drive.

Nome do arquivo: `{product_id}_{nome_do_produto}_original.{ext}` —
slug ASCII, minúsculo, sem espaços nem acentos:
    P000001_camisa_feminina_original.jpg

Idempotência: antes de criar, busca um arquivo com o mesmo nome na
mesma pasta. Se já existe (reprocessamento, retry), retorna o arquivo
existente em vez de criar um duplicado.

Backend:
- real: Google Drive API v3 via google-api-python-client
  (credenciais de service account em settings);
- fake (dev/testes): backend em memória injetado via
  `FakeDriveBackend` — nenhuma credencial necessária.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from typing import Any, Protocol

from .base import BaseTool, ToolResult

PRODUTOS_NOVOS_FOLDER_NAME = "produtos novos"
_DEFAULT_MIME = "image/jpeg"
_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def slugify_filename(value: str, max_len: int = 60) -> str:
    """'Camisa Feminina M&C!' -> 'camisa_feminina_mc'."""
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return (text or "produto")[:max_len].strip("_") or "produto"


def build_filename(product_id: str, name: str | None, mime_type: str = _DEFAULT_MIME) -> str:
    ext = _EXT_BY_MIME.get(mime_type.lower(), "jpg")
    return f"{product_id}_{slugify_filename(name or 'produto')}_original.{ext}"


class DriveBackend(Protocol):
    """Contrato mínimo do Drive (real ou fake)."""

    def find_folder(self, name: str, parent_id: str | None = None) -> str | None: ...

    def create_folder(self, name: str, parent_id: str | None = None) -> str: ...

    def find_file(self, name: str, folder_id: str) -> dict | None: ...

    def upload_file(
        self, name: str, content: bytes, mime_type: str, folder_id: str
    ) -> dict: ...

    def download_file(self, file_id: str) -> bytes: ...


class FakeDriveBackend:
    """Drive em memória para dev/testes — mesma interface do real."""

    def __init__(self) -> None:
        self.folders: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        for fid, folder in self.folders.items():
            if folder["name"] == name and folder.get("parent") == parent_id:
                return fid
        return None

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        existing = self.find_folder(name, parent_id)
        if existing:
            return existing
        fid = self._next_id("folder")
        self.folders[fid] = {"id": fid, "name": name, "parent": parent_id}
        return fid

    def find_file(self, name: str, folder_id: str) -> dict | None:
        for f in self.files.values():
            if f["name"] == name and f["folder_id"] == folder_id:
                return f
        return None

    def upload_file(
        self, name: str, content: bytes, mime_type: str, folder_id: str
    ) -> dict:
        file_id = self._next_id("file")
        record = {
            "id": file_id,
            "name": name,
            "folder_id": folder_id,
            "mime_type": mime_type,
            "size": len(content),
            "content": content,
            "web_view_link": f"https://drive.example.test/file/{file_id}/view",
        }
        self.files[file_id] = record
        return record

    def download_file(self, file_id: str) -> bytes:
        record = self.files.get(file_id)
        if not record:
            raise FileNotFoundError(f"Arquivo não encontrado: {file_id}")
        return record["content"]


class GoogleDriveBackend:
    """Backend real — Google Drive API v3 com service account.

    Só importa google-api-python-client quando instanciado, para o
    serviço subir em dev sem a dependência instalada.
    """

    def __init__(self, service_account_json: str, root_folder_id: str = "") -> None:
        import json

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = json.loads(service_account_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._root_folder_id = root_folder_id or None

    def find_folder(self, name: str, parent_id: str | None = None) -> str | None:
        parent = parent_id or self._root_folder_id
        query = (
            "mimeType = 'application/vnd.google-apps.folder' "
            f"and name = '{name}' and trashed = false"
        )
        if parent:
            query += f" and '{parent}' in parents"
        resp = self._svc.files().list(q=query, fields="files(id, name)").execute()
        files = resp.get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        existing = self.find_folder(name, parent_id)
        if existing:
            return existing
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        parent = parent_id or self._root_folder_id
        if parent:
            metadata["parents"] = [parent]
        folder = self._svc.files().create(body=metadata, fields="id").execute()
        return folder["id"]

    def find_file(self, name: str, folder_id: str) -> dict | None:
        query = (
            f"name = '{name}' and '{folder_id}' in parents and trashed = false"
        )
        resp = (
            self._svc.files()
            .list(q=query, fields="files(id, name, webViewLink)")
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            return None
        f = files[0]
        return {"id": f["id"], "name": f["name"], "web_view_link": f.get("webViewLink")}

    def upload_file(
        self, name: str, content: bytes, mime_type: str, folder_id: str
    ) -> dict:
        from googleapiclient.http import MediaInMemoryUpload

        metadata = {"name": name, "parents": [folder_id]}
        media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=True)
        created = (
            self._svc.files()
            .create(body=metadata, media_body=media, fields="id, name, webViewLink")
            .execute()
        )
        # Compartilhamento de leitura por link — o URL vai para o Sheets
        self._svc.permissions().create(
            fileId=created["id"], body={"type": "anyone", "role": "reader"}
        ).execute()
        return {
            "id": created["id"],
            "name": created["name"],
            "web_view_link": created.get("webViewLink"),
        }

    def download_file(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload
        import io

        request = self._svc.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()


class SaveProductImageTool(BaseTool):
    name = "save_product_image"
    description = (
        "Salva a foto original do produto na pasta '/produtos novos' do "
        "Drive com nome determinístico (product_id + nome). Idempotente: "
        "reexecutar com o mesmo nome retorna o arquivo existente."
    )

    def __init__(self, backend: DriveBackend | None = None) -> None:
        self._backend = backend or FakeDriveBackend()

    def run(
        self,
        *,
        product_id: str,
        name: str | None = None,
        image_base64: str | None = None,
        mime_type: str = _DEFAULT_MIME,
        folder_id: str | None = None,
        **_: Any,
    ) -> ToolResult:
        if not image_base64:
            return ToolResult(
                success=False,
                error="save_product_image chamado sem imagem (image_base64 vazio).",
                retryable=False,
            )
        try:
            content = base64.b64decode(image_base64)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"image_base64 inválido: {exc}", retryable=False
            )

        target_folder = folder_id or self._backend.create_folder(
            PRODUTOS_NOVOS_FOLDER_NAME
        )
        filename = build_filename(product_id, name, mime_type)

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
                "deduplicated": existing is not None,
            },
        )
