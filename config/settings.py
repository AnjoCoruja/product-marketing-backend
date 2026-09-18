"""Configuração central da aplicação.

Lê variáveis de ambiente via pydantic-settings. Nenhum segredo
é hardcoded — tudo vem de .env (dev) ou do ambiente (staging/prod).
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Ambiente ---
    app_env: Literal["dev", "staging", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- LLM (FASE 2+) ---
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    llm_api_key: str = ""
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    # --- Geração de Imagem (FASE 6) ---
    image_provider: str = "openai"
    image_model: str = "gpt-image-1"
    image_api_key: str = ""

    # --- Telegram (FASE 2) ---
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""

    # --- Google (FASE 2) ---
    google_service_account_json: str = ""
    google_drive_root_folder_id: str = ""
    google_sheets_spreadsheet_id: str = ""

    # --- Redes Sociais (FASE 7 — Social Media Agent) ---
    # Tokens NUNCA no código: vêm de .env (dev) ou do ambiente (prod).
    # Uma rede fica habilitada quando SOCIAL_ENABLE_<REDE>=true; em prod
    # a publicação real exige também o token correspondente.
    social_enable_instagram: bool = False
    social_enable_facebook: bool = False
    social_enable_tiktok: bool = False
    meta_access_token: str = ""            # Graph API (Instagram + Facebook)
    instagram_business_account_id: str = ""
    facebook_page_id: str = ""
    tiktok_access_token: str = ""
    tiktok_open_id: str = ""

    # --- Aprovação de campanha (FASE 10) ---
    # AUTO_PUBLISH=true  -> publica direto após gerar imagem (padrão).
    # AUTO_PUBLISH=false -> envia prévia ao Telegram e aguarda decisão
    #   (PUBLICAR / CANCELAR / REFAZER) antes do Social Media Agent.
    auto_publish: bool = True
    # --- n8n ---
    n8n_base_url: str = "https://orquestrador.app.n8n.cloud"
    n8n_webhook_secret: str = ""

    # --- Persistência / Camada Repository ---
    # DATABASE_BACKEND=postgres + DATABASE_URL -> repositórios PostgreSQL
    # (produção). Ausente -> SQLite legado (dev/testes), sem mudar nada.
    database_backend: Literal["sqlite", "postgres"] = "sqlite"
    database_url: str = ""
    # Espelho opcional: grava também no Google Sheets (visualização
    # administrativa). O PostgreSQL segue sendo a fonte da verdade.
    sheets_mirror_enabled: bool = False

    # --- Checkpointer ---
    checkpointer_backend: Literal["memory", "postgres"] = "memory"

    @field_validator("app_env", mode="before")
    @classmethod
    def _lower_env(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def allowed_chat_ids(self) -> list[int]:
        """IDs de chat do Telegram autorizados (whitelist)."""
        if not self.telegram_allowed_chat_ids:
            return []
        return [
            int(x.strip())
            for x in self.telegram_allowed_chat_ids.split(",")
            if x.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """Singleton de configuração (cache por processo)."""
    return Settings()
