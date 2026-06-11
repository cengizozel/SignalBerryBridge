"""
Mock signal-cli-rest-api for SignalBerry testing.

Serves the REST + WebSocket surface the app and bridge actually use, on ONE port
(matching the real api where ws and http share a base URL). A control surface
under /_test lets tests and adb-driven gauntlets inject envelopes and inspect
recorded calls.

Run standalone for emulator gauntlets:
    python3 tests/mock_signal_api.py --port 5001
    # emulator app connects to 10.0.2.2:5001
    # inject a peer message:
    #   curl -X POST localhost:5001/_test/envelope -d '{"envelope": {...}}'

Or in pytest via aiohttp's test utilities (see conftest.py).
"""

import argparse
import asyncio
import json
import time

from aiohttp import web, WSMsgType

SELF_NUMBER = "+15550009999"
SELF_UUID = "00000000-0000-4000-8000-0000000000ff"


def make_app(self_number: str = SELF_NUMBER) -> web.Application:
    app = web.Application(client_max_size=256 * 1024 * 1024)
    state = {
        "ws_clients": set(),        # live /v1/receive sockets
        "calls": [],                # every REST call: {method, path, json, ts}
        "attachments": {},          # att_id -> bytes
        "contacts": [],             # objects served by /v1/contacts
        "send_counter": int(time.time() * 1000),
        "send_timestamp_as_string": False,  # toggle to probe app parsing
        "self_number": self_number,
    }
    app["state"] = state

    def record(request, body):
        state["calls"].append({
            "method": request.method,
            "path": request.path,
            "json": body,
            "ts": int(time.time() * 1000),
        })

    async def broadcast(payload: dict):
        msg = json.dumps(payload)
        dead = set()
        for ws in state["ws_clients"]:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        state["ws_clients"] -= dead

    # ── real API surface ─────────────────────────────────────────────────────

    async def ws_receive(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        state["ws_clients"].add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            state["ws_clients"].discard(ws)
        return ws

    async def v2_send(request):
        body = await request.json()
        record(request, body)
        state["send_counter"] += 7  # distinct, monotonic, not wall-clock
        ts = state["send_counter"]
        val = str(ts) if state["send_timestamp_as_string"] else ts
        return web.json_response({"timestamp": val}, status=201)

    async def get_attachment(request):
        att_id = request.match_info["att_id"]
        data = state["attachments"].get(att_id)
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data, content_type="application/octet-stream")

    async def reactions(request):
        record(request, await request.json())
        return web.json_response({}, status=204 if request.method == "DELETE" else 201)

    async def receipts(request):
        body = await request.json()
        record(request, body)
        # mirror real validation: singular non-zero timestamp required
        if not body.get("timestamp"):
            return web.json_response({"error": "timestamp required"}, status=400)
        return web.json_response({}, status=204)

    async def typing(request):
        record(request, await request.json())
        return web.json_response({}, status=204)

    async def contacts(request):
        return web.json_response(state["contacts"])

    async def accounts(request):
        return web.json_response([state["self_number"]])

    async def about(request):
        return web.json_response({
            "versions": ["v1", "v2"], "build": 2, "mode": "json-rpc",
            "version": "0.99",
            "capabilities": {"v2/send": ["quotes", "mentions"]},
        })

    async def health(request):
        return web.Response(status=204)

    # ── control surface ──────────────────────────────────────────────────────

    async def t_envelope(request):
        payload = await request.json()
        # accept bare envelope or full receive-frame
        frame = payload if "envelope" in payload else {"envelope": payload}
        frame.setdefault("account", state["self_number"])
        await broadcast(frame)
        return web.json_response({"delivered_to": len(state["ws_clients"])})

    async def t_calls(request):
        return web.json_response(state["calls"])

    async def t_attachment(request):
        att_id = request.match_info["att_id"]
        state["attachments"][att_id] = await request.read()
        return web.json_response({"ok": True})

    async def t_contacts(request):
        state["contacts"] = await request.json()
        return web.json_response({"ok": True})

    async def t_reset(request):
        state["calls"].clear()
        state["attachments"].clear()
        state["contacts"].clear()
        return web.json_response({"ok": True})

    async def t_config(request):
        cfg = await request.json()
        if "send_timestamp_as_string" in cfg:
            state["send_timestamp_as_string"] = bool(cfg["send_timestamp_as_string"])
        return web.json_response({"ok": True})

    app.router.add_get("/v1/receive/{number}", ws_receive)
    app.router.add_post("/v2/send", v2_send)
    app.router.add_get("/v1/attachments/{att_id}", get_attachment)
    app.router.add_post("/v1/reactions/{number}", reactions)
    app.router.add_delete("/v1/reactions/{number}", reactions)
    app.router.add_post("/v1/receipts/{number}", receipts)
    app.router.add_put("/v1/typing-indicator/{number}", typing)
    app.router.add_delete("/v1/typing-indicator/{number}", typing)
    app.router.add_get("/v1/contacts/{number}", contacts)
    app.router.add_get("/v1/accounts", accounts)
    app.router.add_get("/v1/about", about)
    app.router.add_get("/v1/health", health)
    app.router.add_post("/_test/envelope", t_envelope)
    app.router.add_get("/_test/calls", t_calls)
    app.router.add_post("/_test/attachment/{att_id}", t_attachment)
    app.router.add_post("/_test/contacts", t_contacts)
    app.router.add_post("/_test/reset", t_reset)
    app.router.add_post("/_test/config", t_config)
    return app


# ── envelope factories (shared by pytest + gauntlet scripts) ─────────────────

PEER_NUMBER = "+15550001111"
PEER_UUID = "11111111-2222-4333-8444-555566667777"


def data_message(text=None, ts=None, attachments=None, quote=None, reaction=None,
                 remote_delete=None, src_number=PEER_NUMBER, src_uuid=PEER_UUID):
    ts = ts or int(time.time() * 1000)
    dm = {"timestamp": ts, "viewOnce": False}
    if text is not None:
        dm["message"] = text
    if attachments:
        dm["attachments"] = attachments
    if quote:
        dm["quote"] = quote
    if reaction:
        dm["reaction"] = reaction
    if remote_delete:
        dm["remoteDelete"] = {"timestamp": remote_delete}
    return _env(src_number, src_uuid, ts, {"dataMessage": dm})


def edit_message(target_ts, new_text, ts=None,
                 src_number=PEER_NUMBER, src_uuid=PEER_UUID):
    """Peer edit: envelope.editMessage = {targetSentTimestamp, dataMessage}.
    NOTE: exact nesting is pinned in M1 from live capture; adjust if it differs."""
    ts = ts or int(time.time() * 1000)
    return _env(src_number, src_uuid, ts, {"editMessage": {
        "targetSentTimestamp": target_ts,
        "dataMessage": {"timestamp": ts, "message": new_text},
    }})


def sync_sent(text=None, ts=None, dest_number=PEER_NUMBER, dest_uuid=PEER_UUID,
              attachments=None, reaction=None, self_number=SELF_NUMBER):
    ts = ts or int(time.time() * 1000)
    sent = {"destination": dest_number, "destinationNumber": dest_number,
            "destinationUuid": dest_uuid, "timestamp": ts}
    if text is not None:
        sent["message"] = text
    if attachments:
        sent["attachments"] = attachments
    if reaction:
        sent["reaction"] = reaction
    return _env(self_number, SELF_UUID, ts, {"syncMessage": {"sentMessage": sent}})


def receipt(timestamps, kind="delivery", src_number=PEER_NUMBER, src_uuid=PEER_UUID):
    now = int(time.time() * 1000)
    return _env(src_number, src_uuid, now, {"receiptMessage": {
        "when": now,
        "isDelivery": kind == "delivery",
        "isRead": kind == "read",
        "isViewed": kind == "viewed",
        "timestamps": list(timestamps),
    }})


def typing(action="STARTED", src_number=PEER_NUMBER, src_uuid=PEER_UUID):
    now = int(time.time() * 1000)
    return _env(src_number, src_uuid, now,
                {"typingMessage": {"action": action, "timestamp": now}})


def attachment(att_id, mime="image/jpeg", filename="photo.jpg", size=1024):
    return {"contentType": mime, "filename": filename, "id": att_id, "size": size}


def _env(src_number, src_uuid, ts, body):
    env = {"source": src_number, "sourceNumber": src_number,
           "sourceUuid": src_uuid, "sourceName": "Mock Peer", "sourceDevice": 1,
           "timestamp": ts}
    env.update(body)
    return {"envelope": env, "account": SELF_NUMBER}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    web.run_app(make_app(), host=args.host, port=args.port)
