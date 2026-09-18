from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Event, DeliveryAttempt, EventState, AttemptOutcome
from app.schemas import EventIn


def _insert_or_ignore(db: Session, values: dict) -> str | None:
    """Dialect-aware INSERT ... ON CONFLICT DO NOTHING ... RETURNING id.

    This is the actual idempotency guarantee (AC4), including under
    concurrent duplicate submissions: it relies on the primary-key
    constraint at the database level, not an application-level
    check-then-insert, which would have a race window.

    Returns the id if THIS call's insert actually happened, or None if a
    conflicting row already existed — i.e. some other request (concurrent
    or earlier) already created it. RETURNING is what makes this
    trustworthy: unlike re-reading the row afterwards, a losing
    transaction's RETURNING set is genuinely empty, so it can't be
    confused with "the row looks freshly created."
    """
    if db.bind.dialect.name == "sqlite":
        stmt = (
            sqlite_insert(Event)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Event.id)
        )
    else:
        stmt = (
            pg_insert(Event)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Event.id)
        )
    row = db.execute(stmt).first()
    db.commit()
    return row[0] if row else None


def create_or_get_event(db: Session, event_in: EventIn) -> tuple[Event, bool]:
    """Returns (event, created). `created=True` means this call is the
    one that actually inserted the row (determined via RETURNING, not a
    heuristic re-read — see _insert_or_ignore). `created=False` covers
    both a plain resubmission and losing a concurrent insert race; either
    way, the caller should treat the response the same way (AC4)."""
    values = dict(
        id=event_in.eventId,
        type=event_in.type,
        occurred_at=event_in.occurredAt,
        payload=event_in.payload,
        state=EventState.PENDING,
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )

    existing = db.get(Event, event_in.eventId)
    if existing is not None:
        return existing, False

    inserted_id = _insert_or_ignore(db, values)
    event = db.get(Event, event_in.eventId)
    return event, inserted_id is not None


def get_event(db: Session, event_id: str) -> Event | None:
    return db.get(Event, event_id)


def claim_due_events(db: Session, limit: int = 25) -> list[Event]:
    """Atomically claim due events for delivery by transitioning them
    PENDING -> DELIVERING.

    This is the concurrency guard: two workers (two processes, or two
    overlapping poll iterations) racing to claim the same event will both
    attempt the conditional UPDATE below, but only one can succeed per
    row. A conditional `UPDATE ... WHERE state = 'pending'` is atomic at
    the row level under normal DB isolation — the second writer's
    predicate simply no longer matches once the first has committed, so
    it claims zero rows for that id instead of double-claiming it.

    Without this claim step (the previous implementation was a plain
    SELECT with no state change), two concurrent pollers could both pick
    up the same event and both deliver it, and their independent
    `attempt_count` increments could clobber each other. This is also
    what makes the `DELIVERING` state in `EventState` actually meaningful
    instead of dead code.
    """
    now = datetime.now(timezone.utc)

    # Two-step: find candidates, then conditionally claim them. The
    # atomicity guarantee comes from the second step's WHERE clause, not
    # from this SELECT — a TOCTOU gap here is fine because the UPDATE
    # re-checks state.
    candidate_ids = list(
        db.execute(
            select(Event.id)
            .where(Event.state == EventState.PENDING)
            .where(Event.next_attempt_at <= now)
            .limit(limit)
        ).scalars()
    )
    if not candidate_ids:
        return []

    claim_stmt = (
        update(Event)
        .where(Event.id.in_(candidate_ids))
        .where(Event.state == EventState.PENDING)
        .values(state=EventState.DELIVERING)
        .returning(Event.id)
    )
    claimed_ids = list(db.execute(claim_stmt).scalars())
    db.commit()

    if not claimed_ids:
        return []
    return list(db.execute(select(Event).where(Event.id.in_(claimed_ids))).scalars())


def release_stuck_event(db: Session, event: Event) -> None:
    """If a delivery attempt raises an unexpected exception before
    record_attempt() can run, the event would otherwise be stuck in
    DELIVERING forever, since claim_due_events() only looks at PENDING
    rows. Put it back to PENDING with a short delay so it's picked up
    again instead of silently vanishing."""
    db.refresh(event)
    event.state = EventState.PENDING
    event.next_attempt_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.base_delay_seconds or 1.0
    )
    db.commit()


def record_attempt(
    db: Session,
    event: Event,
    outcome: AttemptOutcome,
    http_status: int | None,
    error_detail: str | None,
) -> DeliveryAttempt:
    event.attempt_count += 1
    attempt = DeliveryAttempt(
        event_id=event.id,
        attempt_number=event.attempt_count,
        attempted_at=datetime.now(timezone.utc),
        outcome=outcome,
        http_status=http_status,
        error_detail=error_detail,
    )
    db.add(attempt)

    if outcome == AttemptOutcome.SUCCESS:
        event.state = EventState.SUCCEEDED
        event.next_attempt_at = None
    elif outcome == AttemptOutcome.PERMANENT_FAILURE:
        event.state = EventState.FAILED
        event.next_attempt_at = None
    else:  # retryable failure
        if event.attempt_count >= settings.max_attempts:
            event.state = EventState.FAILED
            event.next_attempt_at = None
        else:
            from app.delivery import next_delay_seconds
            event.state = EventState.PENDING
            delay = next_delay_seconds(event.attempt_count)
            event.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

    db.commit()
    db.refresh(event)
    return attempt
