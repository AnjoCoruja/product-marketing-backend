# Camada Repository — PostgreSQL em Produção

## Arquitetura

```
agents / services / api
        │
        ▼   dependem APENAS de interfaces (Protocols)
repositories/base.py
  ProductRepository · CampaignRepository · SocialPostRepository
  AgentStateRepository · LogRepository
        │
        ▼   escolhidas pela factory (repositories/factory.py)
┌─────────────────────┬──────────────────────────────┐
│  postgres.py        │  registries SQLite legados   │
│  (DATABASE_BACKEND= │  (dev/testes — padrão,       │
│   postgres)         │   nada muda)                 │
└─────────────────────┴──────────────────────────────┘
        │
        ▼   opcional: SHEETS_MIRROR_ENABLED=true
Google Sheets (somente visualização administrativa;
o PostgreSQL é a fonte da verdade)
```

**Nenhum agente foi alterado.** Os pontos de injeção que já existiam
(`registry=...`, `sheet_tool=...`, `backend=...`, `checkpointer=...`)
recebem objetos que satisfazem as mesmas interfaces.

## Tabelas (migrations/001_init.sql)

| Tabela | Conteúdo | Chaves / constraints |
|---|---|---|
| `products` | produtos | PK product_id, UNIQUE seq, UNIQUE (telegram_chat_id, message_id), CHECK status |
| `campaigns` | campanhas | PK campaign_id, UNIQUE seq, FK→products, CHECK status, content JSONB |
| `social_posts` | publicações | PK (campaign_id, platform), FK→campaigns, UNIQUE (platform, post_id), CHECK status |
| `agent_states` | operações/dead-letter | PK operation_id, CHECK status, índices correlation/product/campaign/status |
| `agent_events` | eventos dos agentes | PK event_id, append-only, índices run/product/campaign |
| `logs` | log estruturado | BIGSERIAL, índices correlation/product/campaign/created_at |
| suporte | `campaign_images` (PK composta), `campaign_approvals`, `daily_runs` (PK run_date), `counters` | FKs + índices parciais em pendências |

Todas com `created_at`/`updated_at` (TIMESTAMPTZ default now()).

## Como ativar em produção

```bash
# .env
DATABASE_BACKEND=postgres
DATABASE_URL=postgresql://user:pass@host:5432/db
SHEETS_MIRROR_ENABLED=true   # opcional — espelho admin no Google Sheets
CHECKPOINTER_BACKEND=postgres
```

```bash
# aplicar migrations (idempotente; também roda no build_repositories)
DATABASE_URL=... python -m repositories.migrate
DATABASE_URL=... python -m repositories.migrate --status

# migrar dados atuais do Google Sheets -> PostgreSQL (idempotente)
DATABASE_URL=... python -m repositories.sheets_migration --dry-run
DATABASE_URL=... python -m repositories.sheets_migration
```

## Uso no código

```python
from repositories import build_repositories
from repositories.wiring import (
    build_product_agent_runner, build_recampaign_service,
    build_social_media_agent, build_checkpointer,
)

repos = build_repositories()                       # backend conforme env
runner = build_product_agent_runner(repos, settings.database_url)
```

## Testes

- `tests/test_repositories.py` — 22 testes de contrato sobre SQLite/fakes
  (interfaces, idempotência, máquina de estados, espelho Sheets, migração).
- Os mesmos cenários rodam contra PostgreSQL real quando
  `DATABASE_URL_TEST=postgresql://...` está definida (9 testes; skipped sem PG).

```bash
DATABASE_URL_TEST=postgresql://localhost/test .venv/bin/python -m pytest tests/test_repositories.py
```
