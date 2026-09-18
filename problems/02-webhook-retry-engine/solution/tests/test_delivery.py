from datetime import datetime, timezone
import threading

import pytest

from app import crud, worker
from app.database import Base
from app.models import EventState, AttemptOutcome
from app.schemas import EventIn
from tests.conftest import configure_receiver


def make_event_in(event_id="evt_1"):
    return EventIn(
        eventId=event_id,
        type="incident.created",
        occurredAt=datetime.now(timezone.utc),
        payload={"incidentId": "inc_1", "severity": "high"},
    )


@pytest.mark.asyncio
async def test_ac1_successful_delivery(db_session, receiver_client):
    """A reachable receiver returning 200 -> event succeeded, one attempt recorded."""
    configure_receiver([200])
    event, created = crud.create_or_get_event(db_session, make_event_in("evt_ac1"))
    assert created is True

    async with receiver_client as client:
        await worker.run_once(db_session, client)

    event = crud.get_event(db_session, "evt_ac1")
    assert event.state == EventState.SUCCEEDED
    assert len(event.attempts) == 1
    assert event.attempts[0].outcome == AttemptOutcome.SUCCESS


@pytest.mark.asyncio
async def test_ac2_temporary_failure_then_retry_succeeds(db_session, receiver_client):
    """Receiver fails once (503, retryable) then succeeds -> two attempts,
    final state succeeded."""
    configure_receiver([503, 200])
    crud.create_or_get_event(db_session, make_event_in("evt_ac2"))

    async with receiver_client as client:
        # First pass: due event fails with 503, gets rescheduled (delay=0 in tests).
        await worker.run_once(db_session, client)
        event = crud.get_event(db_session, "evt_ac2")
        assert event.state == EventState.PENDING
        assert event.attempts[0].outcome == AttemptOutcome.RETRYABLE_FAILURE
        assert event.attempts[0].http_status == 503

        # Second pass: retry succeeds.
        await worker.run_once(db_session, client)

    event = crud.get_event(db_session, "evt_ac2")
    assert event.state == EventState.SUCCEEDED
    assert len(event.attempts) == 2
    assert event.attempts[1].outcome == AttemptOutcome.SUCCESS


@pytest.mark.asyncio
async def test_ac3_bounded_failure_stops_at_attempt_limit(db_session, receiver_client):
    """Receiver always fails -> attempts stop exactly at max_attempts, final
    state is failed, and re-polling schedules no further attempts."""
    from app.config import settings

    configure_receiver([503] * 10)  # more failures than max_attempts
    crud.create_or_get_event(db_session, make_event_in("evt_ac3"))

    async with receiver_client as client:
        for _ in range(settings.max_attempts + 2):  # try to over-poll on purpose
            await worker.run_once(db_session, client)

    event = crud.get_event(db_session, "evt_ac3")
    assert event.state == EventState.FAILED
    assert event.attempt_count == settings.max_attempts
    assert len(event.attempts) == settings.max_attempts
    assert event.next_attempt_at is None


@pytest.mark.asyncio
async def test_ac4_idempotent_ingestion(db_session, receiver_client):
    """Resubmitting the same eventId does not create a second logical
    event or schedule a second independent delivery."""
    configure_receiver([200])

    event1, created1 = crud.create_or_get_event(db_session, make_event_in("evt_ac4"))
    event2, created2 = crud.create_or_get_event(db_session, make_event_in("evt_ac4"))

    assert created1 is True
    assert created2 is False
    assert event1.id == event2.id

    async with receiver_client as client:
        processed = await worker.run_once(db_session, client)

    # Only one due event existed, regardless of how many times it was submitted.
    assert processed == 1
    event = crud.get_event(db_session, "evt_ac4")
    assert len(event.attempts) == 1


def test_claim_due_events_does_not_reclaim_already_claimed(db_session):
    """Regression test for the concurrent-double-delivery fix: once
    claim_due_events() marks an event DELIVERING, a second poll (as would
    happen from a second worker process, or an overlapping loop tick)
    must not select it again."""
    crud.create_or_get_event(db_session, make_event_in("evt_claim"))

    first_batch = crud.claim_due_events(db_session)
    second_batch = crud.claim_due_events(db_session)

    assert len(first_batch) == 1
    assert first_batch[0].id == "evt_claim"
    assert first_batch[0].state == EventState.DELIVERING
    assert second_batch == []


class _ExplodingClient:
    """Simulates a bug/unexpected error during delivery — something that
    is NOT an httpx.HTTPError, so it isn't caught inside attempt_delivery
    itself and must be handled by run_once's per-event try/except."""

    async def post(self, *args, **kwargs):
        raise RuntimeError("simulated unexpected failure")


@pytest.mark.asyncio
async def test_unexpected_exception_does_not_strand_event_or_crash_batch(db_session):
    """Regression test for the worker fault-isolation fix: an unexpected
    exception during one event's delivery must not crash run_once(), and
    must not leave the event stuck in DELIVERING forever."""
    crud.create_or_get_event(db_session, make_event_in("evt_explode"))

    # Should not raise, despite the client always raising RuntimeError.
    processed = await worker.run_once(db_session, _ExplodingClient())
    assert processed == 1

    event = crud.get_event(db_session, "evt_explode")
    # Released back to pending (not stuck in DELIVERING), no attempt recorded
    # since the failure happened before record_attempt could run.
    assert event.state == EventState.PENDING
    assert event.attempt_count == 0
    assert len(event.attempts) == 0


def test_ac4_idempotent_ingestion_permanent_failure_is_not_retried(db_session):
    """Bonus: a permanent (4xx, non-429) failure should not be retried —
    sanity check on classify_outcome used directly, no I/O needed."""
    from app.delivery import classify_outcome
    from app.models import AttemptOutcome as O

    assert classify_outcome(400, transport_error=False) == O.PERMANENT_FAILURE
    assert classify_outcome(429, transport_error=False) == O.RETRYABLE_FAILURE
    assert classify_outcome(503, transport_error=False) == O.RETRYABLE_FAILURE
    assert classify_outcome(None, transport_error=True) == O.RETRYABLE_FAILURE
    assert classify_outcome(200, transport_error=False) == O.SUCCESS


def test_create_or_get_event_concurrent_duplicate_only_one_created(tmp_path):
    """True concurrency test for the create_or_get_event() `created`-flag
    fix (review item A3 / E.3). Uses a file-based SQLite DB (not
    :memory:), not the shared single-session db_session fixture, so two
    threads with independent connections genuinely race on the insert.

    Before the fix, `created` was derived by re-reading the row's shape
    after the fact (attempt_count == 0 and state == PENDING), which both
    racing callers would see as true — a false positive. This test would
    have failed under that old implementation; with the RETURNING-based
    fix, exactly one caller should see created=True.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "concurrent_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)

    results = []
    barrier = threading.Barrier(2)

    def worker_fn():
        session = SessionLocal()
        barrier.wait()  # maximize the chance both threads race on the insert
        try:
            _event, created = crud.create_or_get_event(session, make_event_in("evt_concurrent"))
            results.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=worker_fn) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]
