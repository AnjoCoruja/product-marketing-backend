"""Tools do Google Sheets — registro tabular dos produtos.

Colunas (linha 1 do Sheets, em português, na ordem do modelo de dados):

    product_id | data_cadastro | nome | descricao | cor | tamanho |
    valor_atacado | valor_varejo | drive_original_url | drive_marketing_url | status

Tools:
- `create_product_record` — insere a linha do produto (idempotente:
  se o product_id já existe no Sheets, retorna a linha existente);
- `update_product_record` — atualiza campos de uma linha existente;
- `get_product` — lê a linha de um product_id;
- `get_latest_product` — lê a linha mais recente.

Backend: real (Google Sheets API v4, service account) ou
`FakeSheetsBackend` em memória (dev/testes).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from .base import BaseTool, ToolResult

SHEET_COLUMNS = [
    "product_id",
    "data_cadastro",
    "nome",
    "descricao",
    "cor",
    "tamanho",
    "valor_atacado",
    "valor_varejo",
    "drive_original_url",
    "drive_marketing_url",
    "status",
]

FIELD_TO_COLUMN = {
    "product_id": "product_id",
    "created_at": "data_cadastro",
    "name": "nome",
    "description": "descricao",
    "color": "cor",
    "size": "tamanho",
    "wholesale_price": "valor_atacado",
    "retail_price": "valor_varejo",
    "drive_url": "drive_original_url",
    "drive_marketing_url": "drive_marketing_url",
    "status": "status",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SheetsBackend(Protocol):
    def read_all(self) -> list[list[Any]]: ...

    def append_row(self, values: list[Any]) -> int: ...

    def update_row(self, row_number: int, values: list[Any]) -> None: ...


class FakeSheetsBackend:
    """Planilha em memória para dev/testes."""

    def __init__(self) -> None:
        self.rows: list[list[Any]] = [SHEET_COLUMNS.copy()]

    def read_all(self) -> list[list[Any]]:
        return [row.copy() for row in self.rows]

    def append_row(self, values: list[Any]) -> int:
        self.rows.append(values.copy())
        return len(self.rows)

    def update_row(self, row_number: int, values: list[Any]) -> None:
        self.rows[row_number - 1] = values.copy()


class GoogleSheetsBackend:
    """Backend real — Google Sheets API v4 com service account."""

    def __init__(self, service_account_json: str, spreadsheet_id: str) -> None:
        import json

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = json.loads(service_account_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._spreadsheet_id = spreadsheet_id
        self._ensure_header()

    def _ensure_header(self) -> None:
        values = self.read_all()
        if not values:
            self.update_row(1, SHEET_COLUMNS)

    def read_all(self) -> list[list[Any]]:
        resp = (
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range="A:K")
            .execute()
        )
        return resp.get("values", [])

    def append_row(self, values: list[Any]) -> int:
        existing = self.read_all()
        row_number = len(existing) + 1
        self.update_row(row_number, values)
        return row_number

    def update_row(self, row_number: int, values: list[Any]) -> None:
        self._svc.spreadsheets().values().update(
            spreadsheetId=self._spreadsheet_id,
            range=f"A{row_number}:K{row_number}",
            valueInputOption="RAW",
            body={"values": [values]},
        ).execute()


def _row_to_product(row: list[Any], row_number: int) -> dict[str, Any]:
    product = {}
    for idx, col in enumerate(SHEET_COLUMNS):
        product[col] = row[idx] if idx < len(row) else None
    product["sheet_row"] = row_number
    return product


class _SheetToolBase(BaseTool):
    def __init__(self, backend: SheetsBackend | None = None) -> None:
        self._backend = backend or FakeSheetsBackend()

    def _find_row(self, product_id: str) -> tuple[int, list[Any]] | None:
        rows = self._backend.read_all()
        for idx, row in enumerate(rows):
            if row and row[0] == product_id:
                return idx + 1, row  # 1-based
        return None


class CreateProductRecordTool(_SheetToolBase):
    name = "create_product_record"
    description = (
        "Insere a linha do produto no Google Sheets. Idempotente: se "
        "product_id já existe, retorna a linha atual sem duplicar."
    )

    def run(self, *, product: dict[str, Any], **_: Any) -> ToolResult:
        product_id = product.get("product_id")
        if not product_id:
            return ToolResult(
                success=False, error="product sem product_id", retryable=False
            )

        existing = self._find_row(product_id)
        if existing:
            row_number, row = existing
            return ToolResult(
                success=True,
                data={
                    "product": _row_to_product(row, row_number),
                    "row_number": row_number,
                    "deduplicated": True,
                },
            )

        values = []
        for col in SHEET_COLUMNS:
            if col == "data_cadastro":
                values.append(product.get("created_at") or _now())
            else:
                field = next(
                    (f for f, c in FIELD_TO_COLUMN.items() if c == col), col
                )
                values.append(product.get(field))
        row_number = self._backend.append_row(values)
        return ToolResult(
            success=True,
            data={
                "product": _row_to_product(values, row_number),
                "row_number": row_number,
                "deduplicated": False,
            },
        )


class UpdateProductRecordTool(_SheetToolBase):
    name = "update_product_record"
    description = "Atualiza campos da linha de um product_id no Sheets."

    def run(
        self, *, product_id: str, updates: dict[str, Any], **_: Any
    ) -> ToolResult:
        found = self._find_row(product_id)
        if not found:
            return ToolResult(
                success=False,
                error=f"product_id {product_id} não encontrado no Sheets.",
                retryable=False,
            )
        row_number, row = list(found)
        row = list(row) + [None] * (len(SHEET_COLUMNS) - len(row))
        for field, value in updates.items():
            column = FIELD_TO_COLUMN.get(field)
            if column is None:
                continue
            row[SHEET_COLUMNS.index(column)] = value
        self._backend.update_row(row_number, row)
        return ToolResult(
            success=True,
            data={"product": _row_to_product(row, row_number), "row_number": row_number},
        )


class GetProductTool(_SheetToolBase):
    name = "get_product"
    description = "Lê a linha de um product_id no Sheets."

    def run(self, *, product_id: str, **_: Any) -> ToolResult:
        found = self._find_row(product_id)
        if not found:
            return ToolResult(
                success=False,
                error=f"product_id {product_id} não encontrado no Sheets.",
                retryable=False,
            )
        row_number, row = found
        return ToolResult(
            success=True,
            data={"product": _row_to_product(row, row_number), "row_number": row_number},
        )


class GetLatestProductTool(_SheetToolBase):
    name = "get_latest_product"
    description = "Lê a linha mais recente do Sheets (último produto cadastrado)."

    def run(self, **_: Any) -> ToolResult:
        rows = self._backend.read_all()
        data_rows = [(i + 1, r) for i, r in enumerate(rows) if r and r[0] != "product_id"]
        if not data_rows:
            return ToolResult(
                success=False, error="Nenhum produto cadastrado.", retryable=False
            )
        row_number, row = data_rows[-1]
        return ToolResult(
            success=True,
            data={"product": _row_to_product(row, row_number), "row_number": row_number},
        )
