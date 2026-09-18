"""Enums compartilhados entre todos os schemas."""

from enum import Enum


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"


class RunType(str, Enum):
    NEW_PRODUCT = "new_product"
    RECAMPAIGN = "recampaign"


class TriggerSource(str, Enum):
    TELEGRAM = "telegram"
    SCHEDULER = "scheduler"
    MANUAL = "manual"


class Stage(str, Enum):
    INTAKE = "intake"
    CATALOGING = "cataloging"
    MARKETING = "marketing"
    IMAGE = "image"
    APPROVAL = "approval"
    PUBLISHING = "publishing"
    DONE = "done"
    FAILED = "failed"


class AgentName(str, Enum):
    PRODUCT = "product"
    MARKETING = "marketing"
    IMAGE = "image"
    SOCIAL = "social"
    ORCHESTRATOR = "orchestrator"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    FAILED = "failed"


class PublicationStatus(str, Enum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    # legados (mantidos para compatibilidade com dados antigos)
    SUCCESS = "success"
    SKIPPED = "skipped"


class ProductLifecycle(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class EventType(str, Enum):
    PRODUCT_RECEIVED = "product.received"
    PRODUCT_CATALOGED = "product.cataloged"
    CAMPAIGN_GENERATED = "campaign.generated"
    IMAGE_GENERATED = "image.generated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    POST_PUBLISHED = "post.published"
    POST_FAILED = "post.failed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


class ErrorCategory(str, Enum):
    TRANSIENT = "transient"    # retry automático (timeout, rate limit)
    BUSINESS = "business"      # pede esclarecimento humano
    FATAL = "fatal"            # falha a run, notifica
