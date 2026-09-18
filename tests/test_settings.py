from config.settings import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.app_env == "dev"
    assert s.is_dev and not s.is_prod
    assert s.checkpointer_backend == "memory"
    assert s.llm_api_key == ""  # nenhum segredo por padrão


def test_allowed_chat_ids_parsing():
    s = Settings(_env_file=None, telegram_allowed_chat_ids="123, 456 ,")
    assert s.allowed_chat_ids == [123, 456]


def test_allowed_chat_ids_empty():
    s = Settings(_env_file=None)
    assert s.allowed_chat_ids == []
