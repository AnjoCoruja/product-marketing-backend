"""Testes da camada de confiabilidade: retry, estados e dead-letter."""

import pytest
from fastapi.testclient import TestClient

from api import app
from services.operation_registry import OperationRegistry, OperationStatus
from services.reliability import (
    MaxAttemptsExceeded,
    NonRetryableError,
    ServiceName,
    call_with_retry,
    classify_exception,
)

client = TestClient(app)


@pytest.fixture()
def registry(tmp_path):
    return OperationRegistry(db_path=str(tmp_path / "ops.db"))


# ---------------------------------------------------------------- retry

def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("timeout")
        return "ok"

    result = call_with_retry(
        flaky, service=ServiceName.GOOGLE_DRIVE, operation="upload"
    )
    assert result == "ok"
    assert calls["n"] == 3


def test_retry_stops_at_max_attempts_never_loops():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise ConnectionError("down")

    with pytest.raises(MaxAttemptsExceeded) as exc_info:
        call_with_retry(
            always_fails,
            service=ServiceName.LLM,
            operation="extract",
            max_attempts=3,
        )
    assert calls["n"] == 3  # exatamente o limite — sem loop infinito
    assert exc_info.value.context.service == ServiceName.LLM


def test_non_retryable_error_stops_immediately():
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        raise ValueError("dados inválidos")

    with pytest.raises(MaxAttemptsExceeded):
        call_with_retry(
            bad_request, service=ServiceName.TELEGRAM, operation="send"
        )
    assert calls["n"] == 1  # ValueError nunca retenta


def test_classify_http_statuses():
    class FakeHttpError(Exception):
        def __init__(self, code):
            super().__init__(str(code))
            self.status_code = code

    assert classify_exception(FakeHttpError(429), ServiceName.INSTAGRAM) is True
    assert classify_exception(FakeHttpError(500), ServiceName.INSTAGRAM) is True
    assert classify_exception(FakeHttpError(401), ServiceName.INSTAGRAM) is False
    assert classify_exception(FakeHttpError(404), ServiceName.TIKTOK) is False
    assert classify_exception(
        NonRetryableError(RuntimeError("x")), ServiceName.LLM
    ) is False


def test_max_attempts_per_service_defined():
    for service in ServiceName:
        from services.reliability import MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS

        assert MAX_ATTEMPTS.get(service, DEFAULT_MAX_ATTEMPTS) >= 1


# ------------------------------------------------------------ lifecycle

def test_operation_lifecycle_happy_path(registry):
    op = registry.create(kind="k", product_id="P000001")
    assert op["status"] == "pending"
    op = registry.start(op["operation_id"])
    assert op["status"] == "processing"
    assert op["attempt_count"] == 1
    assert op["started_at"]
    op = registry.complete(op["operation_id"], result={"ok": True})
    assert op["status"] == "completed"
    assert op["finished_at"]
    assert op["result"] == {"ok": True}


def test_invalid_transition_rejected(registry):
    op = registry.create(kind="k")
    registry.start(op["operation_id"])
    registry.complete(op["operation_id"])
    with pytest.raises(ValueError, match="Transição inválida"):
        registry.start(op["operation_id"])  # completed não volta


def test_fail_registers_exactly_where_it_failed(registry):
    op = registry.create(kind="k", product_id="P000001", campaign_id="C000001")
    registry.start(op["operation_id"])
    failed = registry.fail(
        op["operation_id"],
        error="429 rate limit",
        error_type="HttpError",
        service="instagram",
        stage="publish",
    )
    assert failed["status"] == "failed"
    assert failed["dead_letter"] is True
    assert failed["service"] == "instagram"
    assert failed["stage"] == "publish"
    assert failed["error_type"] == "HttpError"
    assert failed["finished_at"]


def test_waiting_user_state(registry):
    op = registry.create(kind="k")
    registry.start(op["operation_id"])
    waiting = registry.mark_waiting_user(op["operation_id"])
    assert waiting["status"] == "waiting_user"
    resumed = registry.start(op["operation_id"])
    assert resumed["status"] == "processing"


def test_cancel_flow(registry):
    op = registry.create(kind="k")
    cancelled = registry.cancel(op["operation_id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ValueError):
        registry.retry(op["operation_id"])


# -------------------------------------------------------------- retry op

def test_retry_by_product_id_requeues_failed(registry):
    op = registry.create(kind="k", product_id="P000001")
    registry.start(op["operation_id"])
    registry.fail(op["operation_id"], error="boom")
    requeued = registry.retry("P000001")
    assert requeued["status"] == "pending"
    assert requeued["correlation_id"] != op["correlation_id"]  # nova cadeia
    assert requeued["attempt_count"] == 0
    assert requeued["error"] is None


def test_retry_completed_rejected(registry):
    op = registry.create(kind="k", product_id="P000002")
    registry.start(op["operation_id"])
    registry.complete(op["operation_id"])
    with pytest.raises(ValueError, match="não reprocessável"):
        registry.retry("P000002")


def test_retry_unknown_ref(registry):
    with pytest.raises(KeyError):
        registry.retry("P999999")


def test_dead_letter_listing(registry):
    op = registry.create(kind="k", product_id="P000003")
    registry.start(op["operation_id"])
    registry.fail(op["operation_id"], error="x", stage="publish")
    dead = registry.list_dead_letters()
    assert len(dead) == 1
    assert dead[0]["stage"] == "publish"


def test_summary_counts(registry):
    a = registry.create(kind="k", service="llm")
    registry.start(a["operation_id"])
    registry.complete(a["operation_id"])
    b = registry.create(kind="k", service="instagram")
    registry.start(b["operation_id"])
    registry.fail(b["operation_id"], error="x")
    summary = registry.summary()
    assert summary["by_status"]["completed"] == 1
    assert summary["by_status"]["failed"] == 1
    assert summary["dead_letters"] == 1
    assert summary["total_attempts"] == 2
    assert summary["by_service"]["llm"] == 1
    assert len(summary["recent_failures"]) == 1


# ----------------------------------------------------------------- API

def test_api_error_report_and_retry():
    resp = client.post(
        "/errors/report",
        json={
            "workflow_name": "Product Agent — Intake Telegram",
            "node_name": "Call Product Agent",
            "error": "connection refused",
            "product_id": "P000001",
            "service": "llm",
        },
    )
    assert resp.status_code == 200
    operation_id = resp.json()["operation_id"]

    op = client.get(f"/operations/{operation_id}").json()
    assert op["status"] == "failed"
    assert op["stage"] == "Call Product Agent"
    assert op["service"] == "llm"

    retry = client.post("/retry", json={"ref": "P000001"})
    assert retry.status_code == 200
    assert retry.json()["status"] == "pending"


def test_api_summary_endpoint():
    resp = client.get("/executions/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "by_status" in body
    assert "dead_letters" in body
    assert "recent_failures" in body


def test_api_retry_unknown_returns_404():
    resp = client.post("/retry", json={"ref": "P000000"})
    assert resp.status_code == 404
