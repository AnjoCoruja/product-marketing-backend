import pytest

from services import LLMService


def test_llm_not_configured_by_default(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    service = LLMService()
    service.settings.llm_api_key = ""
    assert not service.configured
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        service.get_chat_model()
