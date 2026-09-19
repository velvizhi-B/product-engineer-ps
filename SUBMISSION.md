# Submission — Webhook Retry Engine

**Demo video:** https://youtu.be/Y_nF_qbRMuU

## Selected problem

Problem 2: Webhook Retry Engine.

## Setup and run instructions

See `README.md` — Quick Start section. Summary:

```bash
pip install -r requirements.txt
docker compose up -d
uvicorn app.mock_receiver:app --port 9000 &
uvicorn app.main:app --port 8000 --reload
```

Tests: `pytest -v` (no external services required).

## Architecture and data flow
POST /events ──▶ Ingestion (FastAPI) ──▶ Postgres (events, delivery_attempts)
GET /events/{id} ──▶ Query (FastAPI) ────────┘
│ polled by
▼
Delivery worker loop (async)
│ HTTP POST
▼
Configured webhook endpoint


Four separated responsibilities:
- **Ingestion** (`app/main.py`, `app/crud.py`) — validates the event and performs an
  idempotent insert.
- **Storage** (`app/models.py`) — `events` (current state, source of truth) and
  `delivery_attempts` (append-only attempt log).
- **Scheduling & delivery** (`app/worker.py`, `app/delivery.py`) — an async loop
  polls for due events and *atomically claims* them (`PENDING → DELIVERING`,
  a conditional `UPDATE ... WHERE state='pending'`) before attempting
  delivery, so two overlapping poll iterations — or two separate worker
  processes — can't both pick up and deliver the same event.
- **Observability** (`GET /events/{id}`) — returns current state plus the full
  ordered attempt history.

## Technology choices and trade-offs

- **FastAPI + SQLAlchemy + PostgreSQL**: matches the stack I already build in
  (React/FastAPI/PostgreSQL), so effort went into the reliability logic
  rather than learning new tooling under time pressure.
- **Polling worker loop instead of a message queue (e.g. Celery/RQ/SQS)**:
  the brief explicitly scopes out a distributed queue. A single async
  poll loop is simple to reason about and easy to demo. To make it safe
  even if more than one worker process polls the same DB, claiming a due
  event is a conditional `UPDATE events SET state='delivering' WHERE
  state='pending' ...` — atomic at the row level regardless of how many
  processes issue it, since the guarantee comes from the DB's own
  row-level locking, not from being single-process. What this *doesn't*
  cover: if a worker process is killed outright (not just an exception —
  an actual process crash) after claiming an event but before recording
  an outcome, that event stays `DELIVERING` forever, since nothing
  currently sweeps stale claims back to `PENDING`. An in-process
  exception during delivery is handled (the event is released back to
  `PENDING`); a hard process kill is not. See "Known limitations" below.
- **Idempotency via `INSERT ... ON CONFLICT DO NOTHING` on the
  caller-supplied `eventId` primary key**, not an app-level
  check-then-insert. This is a deliberate choice: the DB constraint is
  the only thing that's actually safe under concurrent duplicate
  submissions.
- **SQLite for tests, Postgres for the running service**: SQLAlchemy
  abstracts the dialect difference (see the `_insert_or_ignore` dialect
  branch in `app/crud.py`), so tests run in milliseconds with zero
  external dependencies while the real service still runs against
  Postgres.

## Assumptions and limitations

- Single configured webhook endpoint (as scoped) — no multi-subscriber fan-out.
- Retry delay/backoff/poll interval are read from `app/config.py` at
  process start (env-overridable) rather than being per-event or
  dynamically adjustable at runtime.
- The worker loop runs in-process with the API (one `uvicorn` process). No
  authentication, as explicitly out of scope.
- `create_all()` is used for schema setup instead of migrations — fine for
  this exercise, not for production (see below).

## Decisions documented (per the problem brief)

- **Retryable vs. permanent**: connection errors, timeouts, HTTP 429, and
  HTTP 5xx are retryable. Any other 4xx is treated as permanent — retrying
  a request the receiver has told us is malformed won't change the
  outcome. See `app/delivery.py::classify_outcome`.
- **Retry limit and backoff**: exponential backoff, `base_delay_seconds *
  backoff_factor^(attempt-1)`, capped at `max_delay_seconds`, up to
  `max_attempts` total attempts (defaults: 2s base, factor 2, 60s cap, 5
  attempts). All configurable via env vars for tests/demos.
- **Delivery guarantee**: at-least-once. A receiver can observe more than
  one delivery for the same `eventId` (e.g. if our process crashes after
  a successful POST but before recording the outcome). Receivers should
  treat `eventId` as a dedup key on their side.
- **Concurrent duplicate submissions**: handled by the database's primary
  key constraint on `events.id` (ingestion side), not application logic —
  see "Technology choices" above. **Concurrent delivery of the same
  event** is a separate concern, handled by the atomic
  `PENDING → DELIVERING` claim in `crud.claim_due_events` — see the same
  section and "Known limitations" below for what it doesn't cover.
- **What's retained per attempt**: attempt number, timestamp, outcome,
  HTTP status (if any), and error detail (if any) — enough to answer "what
  happened and when" without storing full request/response bodies.

## Production and scale considerations

- **What could still cause a duplicate delivery?** A crash between a
  successful HTTP POST and the `record_attempt` DB commit — the receiver
  saw a delivery we don't know succeeded, so on restart we'd retry it.
  This is inherent to at-least-once delivery across an HTTP boundary
  without distributed transactions; the fix is receiver-side
  idempotency keyed on `eventId`, not something the sender can fully
  eliminate.
- **Running with many workers**: the delivery-claim step (`PENDING →
  DELIVERING`) already makes it safe for more than one worker *process*
  to poll the same Postgres DB without double-delivering — that part
  doesn't require new infrastructure. What I'd still add before actually
  running more than one: a periodic sweep that releases events stuck in
  `DELIVERING` past a timeout (covers a worker process being killed
  outright, not just an in-process exception, which is already handled),
  and, at higher volume, `SELECT ... FOR UPDATE SKIP LOCKED` or a real
  message broker instead of every worker polling the full due-set.
- **Isolating one failing endpoint**: per-endpoint concurrency caps and
  circuit-breaking (stop attempting a receiver that's failing every
  request for a cooldown window) so one bad endpoint can't starve the
  worker pool of capacity meant for healthy ones. Not implemented here —
  scoped out since there's only one configured endpoint.
- **Metrics/alerts I'd add**: delivery success rate and p95/p99 latency
  per endpoint, attempts-until-terminal distribution, count of events
  currently in `failed` state, queue depth (events due but not yet
  attempted) as a backlog signal.

## Known limitations

- **A killed worker process leaves its claimed event stuck.** The
  `PENDING → DELIVERING` claim protects against two workers racing for
  the same event and against a single worker's in-process exceptions
  (both are handled and tested — see `tests/test_delivery.py`). It does
  *not* protect against the process itself being killed after claiming
  an event but before recording an outcome — that event stays
  `DELIVERING` with no automatic sweep back to `PENDING`. A
  timeout-based reclaim (e.g. "anything `DELIVERING` for longer than N ×
  the delivery timeout is stale, release it") would close this; not
  implemented given the time box.
- **No automated tests run against real PostgreSQL.** The full suite
  runs against SQLite (in-memory for the AC/unit tests, a temp file for
  the one true-concurrency test) so it's fast and needs no external
  service. The Postgres-specific `INSERT ... ON CONFLICT ... RETURNING`
  branch and Postgres' native `ENUM`/`TIMESTAMPTZ` behavior are only
  verified by manual smoke testing (see the demo video), not by CI.
- **No automated tests go through the FastAPI HTTP layer itself.**
  Everything is tested at the `crud`/`worker` level, which is where the
  actual business logic lives, but request validation (a malformed
  body → expected `422`) and the exact response shapes were only
  checked manually via `curl`.
- **3xx responses from the target webhook are classified as permanent
  failures** (the fallback branch in `classify_outcome`) since `httpx`
  doesn't follow redirects by default in this client. Not explicitly
  required by the brief; noting it as a deliberate default rather than
  an oversight.

## AI usage disclosure

I used Claude to help scaffold this FastAPI service — including the data
model, the idempotency/retry logic, and the test suite — and to think
through the architecture and trade-offs documented above. I then had it
perform a structured code review against this exact brief, which
surfaced concrete issues (a race condition allowing double-delivery if
more than one worker instance ran, a dead/unused `DELIVERING` state, a
flawed heuristic for detecting newly-created vs. duplicate events, and
missing test coverage for the concurrent cases specifically); I reviewed
those findings, decided which to act on given the time box, and had the
fixes implemented and re-verified against the test suite (see
`tests/test_delivery.py::test_claim_due_events_does_not_reclaim_already_claimed`,
`test_unexpected_exception_does_not_strand_event_or_crash_batch`, and
`test_create_or_get_event_concurrent_duplicate_only_one_created`, all
added specifically to prove those fixes rather than just implementation
details).

Beyond that, I personally set up and ran the whole service end-to-end
outside of any automated test — Postgres via Docker, both FastAPI
processes, and a full manual pass through all four acceptance scenarios
via curl, cross-checking each response against what the code should
produce before recording the demo video. I can explain and modify any
part of this codebase.

## Credibility note

The closest prior work I've done to this exact problem is my Multi-Tenant
Document Approval Flow Tracker (Python, TypeScript, PostgreSQL, React) —
it's also state-transition and workflow logic on essentially the same
stack. It handled [X tenants/organizations] with [Y roles] moving
documents through [Z approval states] (e.g. draft → submitted →
reviewed → approved/rejected). I built [describe your specific
contribution — e.g. the full backend state machine and API, or a
specific piece if it was a team project]. One hard decision I had to
make on that project was around [pick one: how state transitions were
validated and enforced so an invalid transition couldn't be persisted;
how concurrent approvals/edits on the same document were handled; or how
tenant data isolation was enforced at the query level] — [one sentence
on how you actually solved it]. [If the repo or a writeup is public, link
it here; otherwise delete this sentence.]