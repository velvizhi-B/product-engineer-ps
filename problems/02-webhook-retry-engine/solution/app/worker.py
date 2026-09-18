import asyncio
import logging

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.delivery import classify_outcome
from app.models import Event
from app import crud

logger = logging.getLogger("webhook_retry.worker")


async def attempt_delivery(db: Session, event: Event, client: httpx.AsyncClient) -> None:
    """Make one delivery attempt for a single event and record the
    outcome. Isolated from the polling loop so tests can call it
    directly against a single event without running the loop."""
    status_code: int | None = None
    transport_error = False
    error_detail: str | None = None

    try:
        response = await client.post(
            settings.webhook_target_url,
            json={
                "eventId": event.id,
                "type": event.type,
                "occurredAt": event.occurred_at.isoformat(),
                "payload": event.payload,
            },
            timeout=settings.delivery_timeout_seconds,
        )
        status_code = response.status_code
    except httpx.HTTPError as exc:
        transport_error = True
        error_detail = f"{type(exc).__name__}: {exc}"
        logger.warning("delivery transport error for event=%s: %s", event.id, error_detail)

    outcome = classify_outcome(status_code, transport_error)
    if outcome.value != "success" and error_detail is None and status_code is not None:
        error_detail = f"HTTP {status_code}"

    crud.record_attempt(db, event, outcome, status_code, error_detail)
    logger.info(
        "event=%s attempt=%s outcome=%s status=%s",
        event.id, event.attempt_count, outcome.value, status_code,
    )


async def run_once(db: Session, client: httpx.AsyncClient) -> int:
    """Claim and process every event currently due. Returns the number
    claimed. This is the deterministic entry point tests use — no
    wall-clock sleeps involved.

    Each claimed event's delivery is isolated in its own try/except: one
    event raising an unexpected error must not stop the rest of the
    batch from being attempted, and must not leave that event stuck in
    DELIVERING forever (see crud.release_stuck_event)."""
    due = crud.claim_due_events(db)
    for event in due:
        try:
            await attempt_delivery(db, event, client)
        except Exception:
            logger.exception(
                "delivery attempt raised unexpectedly for event=%s; releasing back to pending",
                event.id,
            )
            crud.release_stuck_event(db, event)
    return len(due)


async def run_forever(session_factory) -> None:
    """Background polling loop used by the running service (not by tests).

    The poll iteration itself is also wrapped: without this, an
    unhandled exception (e.g. a transient DB connectivity error) would
    propagate out of this while loop and permanently kill the background
    task, leaving the API up and returning 202s while nothing is ever
    delivered again until a process restart."""
    async with httpx.AsyncClient() as client:
        while True:
            db = session_factory()
            try:
                await run_once(db, client)
            except Exception:
                logger.exception("worker poll iteration failed; will retry next interval")
            finally:
                db.close()
            await asyncio.sleep(settings.poll_interval_seconds)
