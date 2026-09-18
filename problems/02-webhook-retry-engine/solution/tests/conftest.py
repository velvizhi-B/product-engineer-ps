import pytest
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401 registers models on Base
from app.database import Base
from app.mock_receiver import app as receiver_app, _state as receiver_state
from app.config import settings

# Speed up tests: no real backoff delay, small attempt limit unless a test
# overrides it directly on `settings`.
settings.base_delay_seconds = 0.0
settings.max_delay_seconds = 0.0
settings.max_attempts = 3


@pytest.fixture()
def db_session():
    """A fresh in-memory SQLite DB per test — fast, isolated, no external
    service required (per the brief: tests must not depend on a paid or
    external service)."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def receiver_client():
    """An httpx client talking to the mock receiver in-process (ASGI
    transport) — no real socket, so it's fast and needs no network."""
    receiver_state["queue"] = []
    receiver_state["received"] = []
    transport = httpx.ASGITransport(app=receiver_app)
    return httpx.AsyncClient(transport=transport, base_url="http://receiver")


def configure_receiver(status_queue: list[int]):
    receiver_state["queue"] = list(status_queue)
    receiver_state["received"] = []
