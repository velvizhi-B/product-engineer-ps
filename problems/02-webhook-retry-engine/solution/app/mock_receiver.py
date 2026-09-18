"""
A tiny, controllable webhook receiver.

For the demo: run this as its own process (`uvicorn app.mock_receiver:app
--port 9000`) and use the /configure endpoint to script its behaviour
live — e.g. "fail the next 2 calls with a 503, then succeed" — so you can
show retry behaviour on camera without editing code between takes.

For tests: import `app` directly and drive it in-process via
`httpx.ASGITransport`, so no real network/socket is involved.
"""
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel

app = FastAPI(title="Mock Webhook Receiver")

# In-memory, intentionally simple: a queue of status codes to return, in
# order. Once exhausted, the receiver returns 200 for everything.
_state = {"queue": [], "received": []}


class ConfigureRequest(BaseModel):
    queue: list[int]


@app.post("/configure")
def configure(body: ConfigureRequest):
    """Set the sequence of HTTP status codes to return on the next N
    deliveries. E.g. {"queue": [503, 503, 200]} fails twice then succeeds."""
    _state["queue"] = list(body.queue)
    _state["received"] = []
    return {"ok": True}


@app.get("/received")
def received():
    return {"count": len(_state["received"]), "events": _state["received"]}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    _state["received"].append(body.get("eventId"))
    status = _state["queue"].pop(0) if _state["queue"] else 200
    return Response(status_code=status, content=b"{}", media_type="application/json")
