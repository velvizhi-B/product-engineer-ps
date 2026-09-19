#!/bin/bash
set -e

step() {
  echo ""
  echo "=================================================="
  echo "$1"
  echo "=================================================="
  read -p "Press Enter when ready to run this..." _
}

step "STEP 1: Submit a brand-new event (expect 201)"
curl -i -X POST localhost:8000/events -H "Content-Type: application/json" -d '{"eventId":"evt_demo_1","type":"incident.created","occurredAt":"2026-09-19T10:00:00Z","payload":{"a":1}}'

step "STEP 2: Resubmit the SAME event (expect 200, not 201 - idempotency)"
curl -i -X POST localhost:8000/events -H "Content-Type: application/json" -d '{"eventId":"evt_demo_1","type":"incident.created","occurredAt":"2026-09-19T10:00:00Z","payload":{"a":1}}'

step "STEP 3: Configure mock receiver to fail once then succeed"
curl -X POST localhost:9000/configure -H "Content-Type: application/json" -d '{"queue":[503,200]}'

step "STEP 4: Submit new event evt_demo_2 (will retry once)"
curl -i -X POST localhost:8000/events -H "Content-Type: application/json" -d '{"eventId":"evt_demo_2","type":"incident.created","occurredAt":"2026-09-19T10:00:00Z","payload":{"a":2}}'

step "STEP 5: Check evt_demo_2 - expect 2 attempts, final state succeeded"
sleep 2
curl localhost:8000/events/evt_demo_2

step "STEP 6: Configure mock receiver to always fail"
curl -X POST localhost:9000/configure -H "Content-Type: application/json" -d '{"queue":[503,503,503,503,503,503,503,503]}'

step "STEP 7: Submit new event evt_demo_3 (will exhaust retries)"
curl -i -X POST localhost:8000/events -H "Content-Type: application/json" -d '{"eventId":"evt_demo_3","type":"incident.created","occurredAt":"2026-09-19T10:00:00Z","payload":{"a":3}}'

step "STEP 8: Check evt_demo_3 - expect 5 attempts, state failed, next_attempt_at null"
sleep 8
curl localhost:8000/events/evt_demo_3

step "STEP 9: Run the automated test suite"
pytest -v

echo ""
echo "Demo complete."