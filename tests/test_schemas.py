from schemas import (
    AgentEvent,
    AgentState,
    Campaign,
    CampaignStatus,
    ErrorCategory,
    ErrorEvent,
    EventType,
    Platform,
    Product,
    ProductLifecycle,
    PublicationStatus,
    RunType,
    SocialPost,
    Stage,
    StageAttempts,
    TriggerSource,
)


def test_product_defaults():
    p = Product(name="Tênis", category="Calçados")
    assert p.product_id  # uuid gerado
    assert p.lifecycle_status == ProductLifecycle.ACTIVE
    assert p.source.raw_text == ""
    assert p.assets.media == []


def test_product_source_roundtrip():
    p = Product.model_validate(
        {
            "name": "Bolsa",
            "source": {
                "telegram_message_id": 10,
                "raw_text": "bolsa de couro",
                "original_photo_file_ids": ["f1", "f2"],
            },
        }
    )
    assert p.source.telegram_message_id == 10
    assert len(p.source.original_photo_file_ids) == 2


def test_campaign_and_social_post():
    c = Campaign(product_id="prod-1")
    assert c.status == CampaignStatus.DRAFT

    post = SocialPost(campaign_id=c.campaign_id, platform=Platform.INSTAGRAM)
    assert post.status == PublicationStatus.PENDING
    assert post.external_post_id is None
    c.publications.append(post)
    assert c.publications[0].platform == Platform.INSTAGRAM


def test_agent_state_defaults():
    s = AgentState()
    assert s.run_id
    assert s.run_type == RunType.NEW_PRODUCT
    assert s.trigger_source == TriggerSource.TELEGRAM
    assert s.current_stage == Stage.INTAKE
    assert s.pending_human_action is None


def test_stage_attempts():
    a = StageAttempts()
    assert a.increment(Stage.MARKETING) == 1
    assert a.increment(Stage.MARKETING) == 2
    assert a.get(Stage.PUBLISHING) == 0


def test_agent_event():
    e = AgentEvent(event_type=EventType.PRODUCT_CATALOGED, run_id="r1", product_id="p1")
    assert e.event_id and e.timestamp
    assert e.event_type == EventType.PRODUCT_CATALOGED


def test_error_event_categories():
    e = ErrorEvent(
        run_id="r1",
        category=ErrorCategory.TRANSIENT,
        message="timeout",
        retryable=True,
        attempt=2,
    )
    assert e.retryable
    assert e.category == ErrorCategory.TRANSIENT
    fatal = ErrorEvent(run_id="r1", category=ErrorCategory.FATAL, message="credencial inválida")
    assert not fatal.retryable
