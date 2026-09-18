import asyncio
import logging

from fastapi import FastAPI, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db, init_db, SessionLocal
from app.schemas import EventIn, EventOut
from app import crud, worker

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Webhook Retry Engine")


@app.on_event("startup")
def on_startup():
    init_db()
    # Launch the background delivery loop. In a real multi-worker
    # deployment this loop would run as a separate process/consumer so
    # ingestion (API) and delivery (worker) scale independently — see
    # SUBMISSION.md for the discussion of running this with many workers.
    asyncio.create_task(worker.run_forever(SessionLocal))


@app.post("/events", response_model=EventOut)
def submit_event(event_in: EventIn, response: Response, db: Session = Depends(get_db)):
    event, created = crud.create_or_get_event(db, event_in)
    # 201 for a genuinely new event, 200 for a resubmission of a known
    # eventId — same body either way (per AC4, the caller doesn't need to
    # branch on this), but the status code now accurately reflects which
    # happened, since `created` is derived from the DB's own RETURNING
    # result rather than guessed from the row's current shape.
    response.status_code = 201 if created else 200
    return event


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: str, db: Session = Depends(get_db)):
    event = crud.get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@app.get("/health")
def health():
    return {"status": "ok"}
