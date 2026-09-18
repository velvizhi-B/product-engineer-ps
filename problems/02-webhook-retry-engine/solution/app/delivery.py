"""
Isolates two decisions that must be easy to reason about and test in
isolation from the DB/HTTP plumbing:

  1. classify_outcome(): given an HTTP status (or a transport exception),
     is this attempt a success, a retryable failure, or a permanent one?
  2. next_delay_seconds(): given the attempt number, how long until the
     next retry?

Retry policy (documented per the brief's requirement):
  - Retryable:   connection errors, timeouts, HTTP 429, HTTP 5xx
  - Permanent:   any other 4xx (bad request / not found / etc. — retrying
                 an event the receiver has told us is malformed won't help)
  - Success:     2xx
"""
from __future__ import annotations

from app.config import settings
from app.models import AttemptOutcome


def classify_outcome(status_code: int | None, transport_error: bool) -> AttemptOutcome:
    if transport_error:
        return AttemptOutcome.RETRYABLE_FAILURE
    if status_code is None:
        return AttemptOutcome.RETRYABLE_FAILURE
    if 200 <= status_code < 300:
        return AttemptOutcome.SUCCESS
    if status_code == 429 or 500 <= status_code < 600:
        return AttemptOutcome.RETRYABLE_FAILURE
    return AttemptOutcome.PERMANENT_FAILURE


def next_delay_seconds(attempt_number: int) -> float:
    """attempt_number is the attempt that just happened (1-indexed).
    Returns the delay before the *next* attempt."""
    delay = settings.base_delay_seconds * (settings.backoff_factor ** (attempt_number - 1))
    return min(delay, settings.max_delay_seconds)
