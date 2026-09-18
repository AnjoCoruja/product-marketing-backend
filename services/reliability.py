"""Camada de confiabilidade — retry, backoff exponencial e classificação
de erros por serviço externo.

Serviços cobertos (chave em `ServiceName`):
    telegram, llm, langgraph, google_drive, google_sheets,
    image_generation, instagram, facebook, tiktok

Regras:
- Limite de tentativas por serviço (`MAX_ATTEMPTS`), sem loop infinito:
  o retry para SEMPRE no limite — erros não-retryable param na 1ª.
- Backoff exponencial com jitter (tenacity.wait_exponential_jitter):
  base 1s, teto 60s.
- Erros FATAL/BUSINESS (4xx de validação, autenticação, schema) NÃO são
  retentados — retry só em erros transientes (timeout, 429, 5xx, rede).
- Toda tentativa/falha gera log estruturado com correlation_id,
  product_id, campaign_id e timestamps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from config import get_logger

logger = get_logger("service.reliability")

T = TypeVar("T")


class ServiceName(str, Enum):
    TELEGRAM = "telegram"
    LLM = "llm"
    LANGGRAPH = "langgraph"
    GOOGLE_DRIVE = "google_drive"
    GOOGLE_SHEETS = "google_sheets"
    IMAGE_GENERATION = "image_generation"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"


# Limite de tentativas por serviço (a 1ª execução conta).
MAX_ATTEMPTS: dict[ServiceName, int] = {
    ServiceName.TELEGRAM: 3,
    ServiceName.LLM: 3,
    ServiceName.LANGGRAPH: 2,          # checkpoints já fazem resume; menos retry
    ServiceName.GOOGLE_DRIVE: 4,
    ServiceName.GOOGLE_SHEETS: 4,
    ServiceName.IMAGE_GENERATION: 2,   # caro — menos tentativas
    ServiceName.INSTAGRAM: 3,
    ServiceName.FACEBOOK: 3,
    ServiceName.TIKTOK: 3,
}

DEFAULT_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0    # segundos
_BACKOFF_CEILING = 60.0


class NonRetryableError(Exception):
    """Erro que não deve ser retentado (validação, auth, negócio).

    Envelopa a causa original para o retry parar na hora e o chamador
    receber o erro real via `unwrap_non_retryable`.
    """

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def unwrap_non_retryable(exc: BaseException) -> BaseException:
    return exc.original if isinstance(exc, NonRetryableError) else exc


def classify_exception(exc: BaseException, service: ServiceName) -> bool:
    """True = transient (retry); False = não-retryable (para na hora)."""

    # Envelopes explícitos vencem tudo.
    if isinstance(exc, NonRetryableError):
        return False

    # Erros de rede/timeout são transientes em qualquer serviço.
    if isinstance(exc, (TimeoutError, ConnectionError, httpx.TimeoutException,
                        httpx.NetworkError)):
        return True

    # HTTP: 429/5xx retenta; 4xx não (auth, validação, not-found).
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500

    # Erros de validação/negócio do próprio sistema nunca retentam.
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return False

    # googleapiclient.errors.HttpError e SDKs sociais: resposta com status.
    resp_status = getattr(exc, "status", None) or getattr(exc, "code", None)
    if isinstance(resp_status, int):
        return resp_status == 429 or resp_status >= 500

    # Heurística final por serviço: mensagens típicas de rate limit/timeout.
    message = str(exc).lower()
    transient_markers = (
        "rate limit", "rate_limit", "too many requests", "429",
        "timeout", "timed out", "temporarily unavailable", "503",
        "502", "500", "internal server error", "connection reset",
    )
    if any(marker in message for marker in transient_markers):
        return True

    # Desconhecido: retentamos; o LIMITE de tentativas garante que
    # nunca vira loop infinito.
    return True


@dataclass
class RetryContext:
    """Contexto de correlação carregado em todos os logs do retry."""

    service: ServiceName
    operation: str
    correlation_id: str = ""
    product_id: str | None = None
    campaign_id: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def log_extra(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id or None,
            "product_id": self.product_id,
            "campaign_id": self.campaign_id,
            "service": self.service.value,
            "operation": self.operation,
        }


class MaxAttemptsExceeded(Exception):
    """Limite de tentativas atingido — a operação vai para dead-letter."""

    def __init__(self, context: RetryContext, last_error: BaseException) -> None:
        super().__init__(
            f"{context.service.value}/{context.operation}: "
            f"{len(context.attempts)} tentativas esgotadas — último erro: {last_error}"
        )
        self.context = context
        self.last_error = unwrap_non_retryable(last_error)


def _log_attempt(context: RetryContext, attempt: RetryCallState) -> None:
    exc = attempt.outcome.exception() if attempt.outcome else None
    started = getattr(attempt, "idle_for", None)
    entry = {
        "attempt_number": attempt.attempt_number,
        "timestamp": time.time(),
        "error": str(exc) if exc else None,
        "error_type": type(exc).__name__ if exc else None,
        "next_wait_seconds": started,
    }
    context.attempts.append(entry)
    logger.warning(
        "tentativa %d falhou em %s/%s: %s",
        attempt.attempt_number,
        context.service.value,
        context.operation,
        exc,
        extra=context.log_extra(),
    )


def call_with_retry(
    fn: Callable[..., T],
    *,
    service: ServiceName,
    operation: str,
    correlation_id: str = "",
    product_id: str | None = None,
    campaign_id: str | None = None,
    max_attempts: int | None = None,
    **kwargs: Any,
) -> T:
    """Executa `fn` com retry + backoff exponencial e limite de tentativas.

    Levanta `MaxAttemptsExceeded` quando o limite esgota (nunca loopa) e
    re-levanta o erro original quando ele é não-retryable.
    """
    context = RetryContext(
        service=service,
        operation=operation,
        correlation_id=correlation_id,
        product_id=product_id,
        campaign_id=campaign_id,
    )
    limit = max_attempts or MAX_ATTEMPTS.get(service, DEFAULT_MAX_ATTEMPTS)

    def _is_retryable(exc: BaseException) -> bool:
        return classify_exception(exc, service)

    logger.info(
        "chamada %s/%s (máx %d tentativas)",
        service.value, operation, limit,
        extra=context.log_extra(),
    )

    retryer = Retrying(
        stop=stop_after_attempt(limit),
        wait=wait_exponential_jitter(initial=_BACKOFF_BASE, max=_BACKOFF_CEILING),
        retry=retry_if_exception(_is_retryable),
        before_sleep=lambda state: _log_attempt(context, state),
        reraise=False,
    )
    try:
        result = retryer(fn, **kwargs)
    except Exception as exc:  # tenacity RetryError ou erro final
        last = unwrap_non_retryable(exc)
        inner = getattr(exc, "last_attempt", None)
        if inner is not None and inner.failed:
            last = unwrap_non_retryable(inner.exception())
        logger.error(
            "limite de tentativas esgotado em %s/%s",
            service.value, operation,
            extra=context.log_extra(),
        )
        raise MaxAttemptsExceeded(context, last) from last

    if context.attempts:
        logger.info(
            "sucesso em %s/%s após %d tentativa(s)",
            service.value, operation, len(context.attempts) + 1,
            extra=context.log_extra(),
        )
    return result
