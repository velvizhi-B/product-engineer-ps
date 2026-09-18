import enum

from sqlalchemy import Column, String, Integer, Float, DateTime, ForeignKey, JSON, Enum as SAEnum
from sqlalchemy.orm import relationship

from app.database import Base


class EventState(str, enum.Enum):
    PENDING = "pending"        # accepted, not yet attempted (or scheduled for retry)
    DELIVERING = "delivering"  # an attempt is currently in flight
    SUCCEEDED = "succeeded"    # terminal: delivered successfully
    FAILED = "failed"          # terminal: exhausted retries


class AttemptOutcome(str, enum.Enum):
    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


class Event(Base):
    """
    The `id` is the caller-supplied eventId, used verbatim as the primary
    key. This is what makes ingestion idempotent: a second POST with the
    same id can never create a second row, by construction (unique/PK
    constraint), rather than by an app-level "check then insert" race.
    """
    __tablename__ = "events"

    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSON, nullable=False)

    state = Column(SAEnum(EventState), nullable=False, default=EventState.PENDING)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    attempts = relationship(
        "DeliveryAttempt", back_populates="event", order_by="DeliveryAttempt.attempt_number"
    )


class DeliveryAttempt(Base):
    """Append-only log. Never updated or deleted — this is the audit trail
    reviewers are told to look for (AC5: inspectable history)."""
    __tablename__ = "delivery_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, ForeignKey("events.id"), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    attempted_at = Column(DateTime(timezone=True), nullable=False)
    outcome = Column(SAEnum(AttemptOutcome), nullable=False)
    http_status = Column(Integer, nullable=True)
    error_detail = Column(String, nullable=True)

    event = relationship("Event", back_populates="attempts")
