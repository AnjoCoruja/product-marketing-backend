-- 001_init.sql — Esquema inicial PostgreSQL (camada Repository)
--
-- Tabelas: products, campaigns, social_posts, agent_states, agent_events,
--          logs (+ campaign_images, campaign_approvals, daily_runs,
--          counters — suporte dos repositories).
-- Idempotente: seguro rodar mais de uma vez (IF NOT EXISTS / DO $$).

BEGIN;

-- ---------------------------------------------------------------- counters
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);

-- ---------------------------------------------------------------- products
CREATE TABLE IF NOT EXISTS products (
    product_id       TEXT PRIMARY KEY,
    seq              BIGINT      NOT NULL UNIQUE,
    telegram_chat_id BIGINT,
    message_id       BIGINT,
    name             TEXT,
    drive_file_id    TEXT,
    drive_url        TEXT,
    sheet_row        BIGINT,
    status           TEXT        NOT NULL DEFAULT 'created'
                                 CHECK (status IN (
                                     'created', 'active', 'completed',
                                     'published', 'partially_published',
                                     'failed', 'cancelled')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Idempotência de intake: a mesma mensagem do Telegram nunca cria
-- dois produtos. NULLs distintos não conflitam (comportamento do PG).
CREATE UNIQUE INDEX IF NOT EXISTS uq_products_message
    ON products (telegram_chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_products_status ON products (status);
CREATE INDEX IF NOT EXISTS idx_products_created ON products (created_at);

-- --------------------------------------------------------------- campaigns
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    seq         BIGINT       NOT NULL UNIQUE,
    product_id  TEXT         NOT NULL REFERENCES products (product_id),
    status      TEXT         NOT NULL DEFAULT 'draft'
                             CHECK (status IN (
                                 'draft', 'awaiting_approval', 'published',
                                 'partially_published', 'failed', 'cancelled')),
    fingerprint TEXT,
    content     JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_campaigns_product ON campaigns (product_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status  ON campaigns (status);

CREATE TABLE IF NOT EXISTS campaign_images (
    campaign_id  TEXT        NOT NULL REFERENCES campaigns (campaign_id),
    image_seq    INTEGER     NOT NULL,
    file_name    TEXT        NOT NULL,
    drive_file_id TEXT,
    drive_url    TEXT,
    status       TEXT        NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, image_seq)   -- idempotência por campanha
);

CREATE TABLE IF NOT EXISTS campaign_approvals (
    id               BIGSERIAL PRIMARY KEY,
    campaign_id      TEXT        NOT NULL REFERENCES campaigns (campaign_id),
    telegram_chat_id BIGINT      NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN (
                                     'pending', 'published', 'cancelled',
                                     'redone')),
    decision         TEXT,
    decision_options JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_approvals_chat_pending
    ON campaign_approvals (telegram_chat_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_approvals_campaign_pending
    ON campaign_approvals (campaign_id) WHERE status = 'pending';

-- Rotina diária 08:00 — PK em run_date bloqueia execução duplicada no dia
CREATE TABLE IF NOT EXISTS daily_runs (
    run_date    DATE PRIMARY KEY,
    product_id  TEXT REFERENCES products (product_id),
    campaign_id TEXT REFERENCES campaigns (campaign_id),
    reason      TEXT        NOT NULL,
    status      TEXT        NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------ social_posts
-- Chave de idempotência: (campaign_id, platform) — publicar duas vezes a
-- mesma campanha na mesma rede nunca cria dois registros.
CREATE TABLE IF NOT EXISTS social_posts (
    campaign_id  TEXT        NOT NULL REFERENCES campaigns (campaign_id),
    platform     TEXT        NOT NULL
                             CHECK (platform IN (
                                 'instagram', 'facebook', 'tiktok')),
    status       TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN (
                                 'pending', 'publishing', 'published',
                                 'failed', 'skipped')),
    post_id      TEXT,
    published_at TIMESTAMPTZ,
    error        TEXT,
    caption      TEXT        NOT NULL DEFAULT '',
    attempts     INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, platform)
);
-- post_id único por rede quando presente (dedup contra re-publicação)
CREATE UNIQUE INDEX IF NOT EXISTS uq_social_posts_post
    ON social_posts (platform, post_id) WHERE post_id IS NOT NULL;

-- ----------------------------------------------------------- agent_states
-- Operações (FASE 9): estado do ciclo de vida + dead-letter. Guarda
-- EXATAMENTE onde cada etapa falhou (service, stage, error_type, error).
CREATE TABLE IF NOT EXISTS agent_states (
    operation_id   TEXT PRIMARY KEY,
    correlation_id TEXT        NOT NULL,
    kind           TEXT        NOT NULL,
    status         TEXT        NOT NULL DEFAULT 'pending'
                               CHECK (status IN (
                                   'pending', 'processing', 'waiting_user',
                                   'completed', 'failed', 'cancelled')),
    service        TEXT,
    stage          TEXT,
    product_id     TEXT,
    campaign_id    TEXT,
    attempt_count  INTEGER     NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts   INTEGER     NOT NULL DEFAULT 3,
    error_type     TEXT,
    error          TEXT,
    dead_letter    BOOLEAN     NOT NULL DEFAULT FALSE,
    payload        JSONB,
    result         JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_agent_states_correlation
    ON agent_states (correlation_id);
CREATE INDEX IF NOT EXISTS idx_agent_states_product
    ON agent_states (product_id);
CREATE INDEX IF NOT EXISTS idx_agent_states_campaign
    ON agent_states (campaign_id);
CREATE INDEX IF NOT EXISTS idx_agent_states_status
    ON agent_states (status);
CREATE INDEX IF NOT EXISTS idx_agent_states_dead_letter
    ON agent_states (dead_letter) WHERE dead_letter;

-- ----------------------------------------------------------- agent_events
-- Eventos de transição entre agentes/estágios (FASE 1) — append-only.
CREATE TABLE IF NOT EXISTS agent_events (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    run_id      TEXT        NOT NULL,
    product_id  TEXT,
    campaign_id TEXT,
    from_agent  TEXT,
    to_agent    TEXT,
    payload_ref TEXT,
    detail      TEXT        NOT NULL DEFAULT '',
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run     ON agent_events (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_product ON agent_events (product_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_campaign ON agent_events (campaign_id);

-- ------------------------------------------------------------------- logs
-- Log estruturado persistido (além do stdout JSON).
CREATE TABLE IF NOT EXISTS logs (
    id             BIGSERIAL PRIMARY KEY,
    level          TEXT        NOT NULL,
    logger         TEXT        NOT NULL,
    message        TEXT        NOT NULL,
    correlation_id TEXT,
    product_id     TEXT,
    campaign_id    TEXT,
    context        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_logs_correlation ON logs (correlation_id);
CREATE INDEX IF NOT EXISTS idx_logs_product     ON logs (product_id);
CREATE INDEX IF NOT EXISTS idx_logs_campaign    ON logs (campaign_id);
CREATE INDEX IF NOT EXISTS idx_logs_created     ON logs (created_at);
CREATE INDEX IF NOT EXISTS idx_logs_level       ON logs (level);

-- Registro das migrations aplicadas
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
