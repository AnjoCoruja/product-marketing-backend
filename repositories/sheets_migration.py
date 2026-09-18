"""Migração de dados: Google Sheets -> PostgreSQL.

Lê as linhas atuais da planilha de produtos (via SheetsBackend — real ou
fake) e insere no `products` do PostgreSQL. Idempotente: produtos já
existentes (mesmo product_id) são pulados; o contador sequencial é
ajustado para continuar a numeração a partir do maior seq migrado.

Também popula o espelho de colunas da planilha (sheet_row) para manter
a referência cruzada usada pelo UpdateProductRecordTool.

CLI:
    python -m repositories.sheets_migration --dry-run
    python -m repositories.sheets_migration
"""

from __future__ import annotations

import sys
from typing import Any

from config import get_logger

logger = get_logger("repositories.sheets_migration")

# Mapa coluna da planilha (pt-BR) -> campo do produto (domínio)
COLUMN_TO_FIELD = {
    "product_id": "product_id",
    "data_cadastro": "created_at",
    "nome": "name",
    "status": "status",
    "drive_original_url": "drive_url",
}


def migrate_products_from_sheets(
    sheets_backend: Any,
    products_repo: Any,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copia produtos da planilha para o repositório PostgreSQL.

    Retorna um relatório {migrated, skipped, errors, max_seq}.
    """
    rows = sheets_backend.read_all()
    if not rows:
        return {"migrated": 0, "skipped": 0, "errors": [], "max_seq": 0}

    header = [str(c).strip() for c in rows[0]]
    report: dict[str, Any] = {
        "migrated": 0, "skipped": 0, "errors": [], "max_seq": 0,
    }

    for row_number, row in enumerate(rows[1:], start=2):
        record = dict(zip(header, row))
        product_id = (record.get("product_id") or "").strip()
        if not product_id:
            continue
        try:
            seq = int(product_id.lstrip("P"))
        except ValueError:
            report["errors"].append(f"linha {row_number}: id inválido {product_id}")
            continue
        report["max_seq"] = max(report["max_seq"], seq)

        if products_repo.get(product_id):
            report["skipped"] += 1
            continue
        if dry_run:
            report["migrated"] += 1
            continue

        name = (record.get("nome") or "").strip() or None
        status = (record.get("status") or "active").strip() or "active"
        drive_url = (record.get("drive_original_url") or "").strip() or None

        products_repo.create(product_id=product_id, name=name)
        products_repo.update(
            product_id,
            name=name,
            drive_url=drive_url,
            sheet_row=row_number,
            status=status if status != "created" else "created",
        )
        report["migrated"] += 1
        logger.info("migrado %s (linha %s)", product_id, row_number)

    if not dry_run and report["max_seq"]:
        _bump_product_counter(products_repo, report["max_seq"])

    return report


def _bump_product_counter(products_repo: Any, max_seq: int) -> None:
    """Garante que o próximo next_product_id() continue a sequência após
    os produtos migrados (nunca reutiliza P000001 se ele já existia)."""
    target = products_repo
    if hasattr(target, "_primary"):  # MirroredProductRepository
        target = target._primary
    conn = getattr(target, "_conn", None)
    if conn is None:
        return
    if hasattr(conn, "transaction"):  # psycopg (PostgreSQL)
        with conn.transaction():
            conn.execute(
                "INSERT INTO counters (name, value) VALUES ('product_seq', %s) "
                "ON CONFLICT (name) DO UPDATE SET "
                "value = GREATEST(counters.value, EXCLUDED.value)",
                (max_seq,),
            )
    else:  # sqlite3 (dev/testes)
        with conn:
            conn.execute(
                "INSERT INTO counters (name, value) VALUES ('product_seq', ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "value = MAX(value, excluded.value)",
                (max_seq,),
            )


if __name__ == "__main__":
    import os

    from config import get_settings
    from repositories.factory import build_repositories

    settings = get_settings()
    if not (
        settings.google_service_account_json
        and settings.google_sheets_spreadsheet_id
    ):
        sys.exit("Configure GOOGLE_SERVICE_ACCOUNT_JSON e "
                 "GOOGLE_SHEETS_SPREADSHEET_ID para migrar do Sheets.")
    if not os.environ.get("DATABASE_URL"):
        sys.exit("Configure DATABASE_URL para o PostgreSQL de destino.")

    from tools.product_sheet import GoogleSheetsBackend

    backend = GoogleSheetsBackend(
        settings.google_service_account_json,
        settings.google_sheets_spreadsheet_id,
    )
    repos = build_repositories(backend="postgres")
    report = migrate_products_from_sheets(
        backend, repos.products, dry_run="--dry-run" in sys.argv
    )
    print(report)
