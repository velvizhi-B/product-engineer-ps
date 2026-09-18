from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class EventIn(BaseModel):
    eventId: str
    type: str
    occurredAt: datetime
    payload: dict[str, Any]


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_number: int
    attempted_at: datetime
    outcome: str
    http_status: Optional[int] = None
    error_detail: Optional[str] = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: str
    occurred_at: datetime
    state: str
    attempt_count: int
    next_attempt_at: Optional[datetime] = None
    attempts: list[AttemptOut] = []
