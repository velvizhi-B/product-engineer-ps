"""
Central configuration. All retry-policy knobs live here so they can be
overridden by environment variables in tests / demos (e.g. shrinking
the retry delay from seconds to milliseconds without touching business
logic).
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Where events + attempts are persisted. Defaults to a local Postgres
    # (see docker-compose.yml). Tests override this to an in-memory SQLite
    # DB so they run fast and need no external service.
    database_url: str = "postgresql+psycopg2://webhook:webhook@localhost:5432/webhook_retry"

    # Where the service delivers events to. In the demo this points at the
    # mock receiver app that ships alongside this service.
    webhook_target_url: str = "http://localhost:9000/webhook"

    # Retry policy -----------------------------------------------------
    # Max number of delivery attempts (including the first) before an
    # event is moved to a terminal `failed` state.
    max_attempts: int = 5

    # Base delay before the *first* retry, in seconds. Exponential backoff
    # from here: delay = base_delay_seconds * (backoff_factor ** (attempt-1)),
    # capped at max_delay_seconds.
    base_delay_seconds: float = 2.0
    backoff_factor: float = 2.0
    max_delay_seconds: float = 60.0

    # How often the worker loop polls for events that are due for
    # (re)delivery.
    poll_interval_seconds: float = 1.0

    # Per-attempt HTTP timeout to the target webhook.
    delivery_timeout_seconds: float = 5.0

    class Config:
        env_prefix = "WEBHOOK_"


settings = Settings()
