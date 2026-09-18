# Webhook Retry Engine

A small backend service that accepts events and delivers them to one
configured webhook endpoint, with recorded delivery attempts, bounded
retries, and idempotent ingestion.

Built for the Caygnus Product Engineering Challenge (Problem 2).

## Stack

- **FastAPI** — ingestion API and query API
- **SQLAlchemy + PostgreSQL** — durable event/attempt storage (SQLite in-memory for tests)
- **httpx** — async delivery client
- **pytest + pytest-asyncio** — tests, driven deterministically (no real sleeps)

## Quick start (≈5 minutes)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 1. Start Postgres
docker compose up -d

# 2. Start the mock webhook receiver (a separate small FastAPI app)
uvicorn app.mock_receiver:app --port 9000 &

# 3. Start the service (creates tables on startup, launches the delivery worker loop)
uvicorn app.main:app --port 8000 --reload
```
**Port conflict?** If `docker compose up -d` fails with `address already in
use` on 5432, something else on your machine already has that port —
commonly a native Postgres install, or another project's Postgres
container. This is common if you run more than one Postgres-backed
project locally. Fix: change the `ports:` line in `docker-compose.yml`
from `"5432:5432"` to an open port, e.g. `"5434:5432"`, then point the
service at it before starting:
```bash
export WEBHOOK_DATABASE_URL="postgresql+psycopg2://webhook:webhook@localhost:5434/webhook_retry"
```
### Try it

```bash
# Make the mock receiver fail once, then succeed, so you can see a retry happen
curl -X POST localhost:9000/configure -H "Content-Type: application/json" \
  -d '{"queue": [503, 200]}'

# Submit an event
curl -X POST localhost:8000/events -H "Content-Type: application/json" -d '{
  "eventId": "evt_123",
  "type": "incident.created",
  "occurredAt": "2026-09-18T10:00:00Z",
  "payload": {"incidentId": "inc_456", "severity": "high"}
}'

# Watch it transition pending -> succeeded (after one retry) over the next couple seconds
curl localhost:8000/events/evt_123

# Resubmit the same eventId — same response, no second delivery job
curl -X POST localhost:8000/events -H "Content-Type: application/json" -d '{
  "eventId": "evt_123",
  "type": "incident.created",
  "occurredAt": "2026-09-18T10:00:00Z",
  "payload": {"incidentId": "inc_456", "severity": "high"}
}'
```

To shrink retry delays for a live demo, set env vars before starting the
service, e.g. `WEBHOOK_BASE_DELAY_SECONDS=0.2 WEBHOOK_POLL_INTERVAL_SECONDS=0.3`.

## Running tests

```bash
pytest -v
```

No Postgres or network access required — tests use an in-memory SQLite DB
and talk to the mock receiver in-process via `httpx.ASGITransport`.

## Architecture

```
POST /events ──▶ Ingestion (FastAPI) ──▶ Postgres (events, delivery_attempts)
                                                  ▲
GET /events/{id} ──▶ Query (FastAPI) ────────────┘
                                                  │
                                        polled by │
                                                  ▼
                                        Delivery worker loop (async)
                                                  │
                                        HTTP POST │
                                                  ▼
                                        Configured webhook endpoint
```

- **Ingestion**: validates the event, does an idempotent insert (`INSERT
  ... ON CONFLICT (id) DO NOTHING`, PK on the caller-supplied `eventId`).
  This is enforced at the database level, so it's safe under concurrent
  duplicate submissions, not just sequential ones.
- **Storage**: `events` holds current state; `delivery_attempts` is an
  append-only log of every attempt (never mutated/deleted).
- **Delivery**: an async worker loop polls for events whose
  `next_attempt_at` is due and atomically claims them (`pending →
  delivering`, a conditional `UPDATE ... WHERE state='pending'`) so two
  overlapping poll iterations — or two worker processes against the same
  DB — can't both deliver the same event. It then attempts delivery,
  classifies the outcome, and either marks the event terminal
  (`succeeded`/`failed`) or reschedules it (`delivering → pending`) with
  exponential backoff. State machine: `pending → delivering →
  succeeded | failed | pending (retry)`. See `SUBMISSION.md`'s "Known
  limitations" for what this claim step doesn't cover (a hard process
  kill mid-delivery, as opposed to an in-process exception, which is
  handled).
- **Observability**: `GET /events/{id}` returns current state plus the
  full ordered attempt history.

See `SUBMISSION.md` for the retry policy, trade-offs, and answers to the
required discussion questions.

## Repo layout

```
app/
  main.py          FastAPI app: POST /events, GET /events/{id}
  worker.py         Delivery loop; run_once() is the deterministic entry point tests use
  delivery.py       Pure functions: outcome classification, backoff calculation
  crud.py           DB access: idempotent insert, attempt recording, due-event polling
  models.py         SQLAlchemy models (Event, DeliveryAttempt)
  schemas.py        Pydantic request/response models
  config.py         All retry-policy knobs, env-overridable
  mock_receiver.py  Standalone controllable receiver, used for both the demo and tests
tests/
  test_delivery.py  AC1–AC4, plus a small classify_outcome unit test
docker-compose.yml  Local Postgres
```
