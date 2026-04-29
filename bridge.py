"""
SignalBerry Bridge

Connects to signal-cli-rest-api via WebSocket, persists every message to SQLite,
and serves them to the SignalBerry Android app over HTTP.

Environment variables:
  SIGNAL_API_URL   base URL of signal-cli-rest-api   (default: http://localhost:5000)
  SIGNAL_NUMBER    your Signal number in E.164 format (required)
  DB_PATH          SQLite file path                   (default: /data/bridge.db)
  PORT             HTTP port                          (default: 9099)
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote

import websocket
from flask import Flask, jsonify, request

# ── config ────────────────────────────────────────────────────────────────────

SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://localhost:5000").rstrip("/")
SIGNAL_NUMBER  = os.environ.get("SIGNAL_NUMBER", "")
DB_PATH        = os.environ.get("DB_PATH", "/data/bridge.db")
PORT           = int(os.environ.get("PORT", "9099"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# ── peer normalisation ────────────────────────────────────────────────────────

def normalize_peer(s: str) -> str:
    """
    Phone numbers  → digits only  ("+1 (555) 123-4567" → "15551234567")
    UUIDs          → lowercase    ("ABC-..." → "abc-...")
    Anything else  → lowercase stripped
    """
    s = (s or "").strip()
    if not s:
        return ""
    if _UUID_RE.match(s):
        return s.lower()
    digits = re.sub(r"\D", "", s)
    return digits if digits else s.lower()


def best_peer(number: str, uuid: str) -> str:
    """Return the normalised peer key, preferring phone number over UUID."""
    n = (number or "").strip()
    u = (uuid   or "").strip()
    return normalize_peer(n) if n else normalize_peer(u)

# ── database ──────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _conn() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                peer      TEXT    NOT NULL,
                dir       TEXT    NOT NULL,
                text      TEXT    NOT NULL DEFAULT '',
                server_ts INTEGER NOT NULL,
                status    INTEGER NOT NULL DEFAULT 1,
                att_id    TEXT    NOT NULL DEFAULT '',
                mime      TEXT    NOT NULL DEFAULT ''
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_peer_ts ON messages(peer, server_ts)"
        )


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def store(peer: str, direction: str, text: str, ts: int,
          status: int = 1, att_id: str = "", mime: str = ""):
    if not peer:
        return
    with _db_lock, _conn() as db:
        db.execute(
            "INSERT INTO messages (peer, dir, text, server_ts, status, att_id, mime) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (peer, direction, text or "", ts, status, att_id or "", mime or ""),
        )
    label = "img" if att_id else repr((text or "")[:50])
    log.info("stored %s %s  peer=%s  ts=%d", direction, label, peer, ts)


def upgrade_status(peer: str, new_status: int):
    """Promote the most recent outgoing message to this peer to at least new_status."""
    if not peer:
        return
    with _db_lock, _conn() as db:
        db.execute(
            "UPDATE messages SET status = ? "
            "WHERE id = ("
            "  SELECT id FROM messages "
            "  WHERE peer = ? AND dir = 'out' AND status < ? "
            "  ORDER BY server_ts DESC LIMIT 1"
            ")",
            (new_status, peer, new_status),
        )

# ── envelope parsing ──────────────────────────────────────────────────────────

def handle_envelope(env: dict):
    src_num  = env.get("sourceNumber") or env.get("source") or ""
    src_uuid = env.get("sourceUuid") or ""
    ts       = env.get("timestamp") or int(time.time() * 1000)

    # incoming message from a peer
    data = env.get("dataMessage")
    if data:
        peer = best_peer(src_num, src_uuid)
        text = (data.get("message") or data.get("text") or "").strip()
        if text:
            store(peer, "in", text, ts, status=2)  # status=2: we received it → delivered
        for att in (data.get("attachments") or []):
            cid  = att.get("id") or ""
            mime = att.get("contentType") or ""
            if cid and mime.startswith("image/"):
                store(peer, "in", "", ts, status=2, att_id=cid, mime=mime)

    # sync echo of a message we sent (from this device or another linked device)
    sync = env.get("syncMessage")
    if sync:
        sent = sync.get("sentMessage") or {}
        peer = best_peer(
            sent.get("destinationNumber") or "",
            sent.get("destinationUuid")   or "",
        )
        text    = (sent.get("message") or "").strip()
        sent_ts = sent.get("timestamp") or ts
        if text:
            store(peer, "out", text, sent_ts, status=1)
        for att in (sent.get("attachments") or []):
            cid  = att.get("id") or ""
            mime = att.get("contentType") or ""
            if cid and mime.startswith("image/"):
                store(peer, "out", "", sent_ts, status=1, att_id=cid, mime=mime)

    # delivery / read receipts → upgrade the latest outgoing message to that peer
    receipt = env.get("receiptMessage")
    if receipt:
        rtype = (receipt.get("type") or "").upper()
        peer  = best_peer(src_num, src_uuid)
        if rtype == "DELIVERY":
            upgrade_status(peer, 2)
        elif rtype in ("READ", "VIEWED"):
            upgrade_status(peer, 3)


def handle_payload(raw: str):
    try:
        parsed = json.loads(raw)
    except Exception:
        return
    envelopes = parsed if isinstance(parsed, list) else [parsed]
    for item in envelopes:
        if isinstance(item, dict):
            env = item.get("envelope")
            if env:
                handle_envelope(env)

# ── WebSocket listener ────────────────────────────────────────────────────────

def _ws_url() -> str:
    base = SIGNAL_API_URL
    if base.startswith("https://"):
        base = "wss://" + base[8:]
    elif base.startswith("http://"):
        base = "ws://" + base[7:]
    else:
        base = "ws://" + base
    return f"{base}/v1/receive/{quote(SIGNAL_NUMBER, safe='')}"


def run_ws():
    url   = _ws_url()
    retry = 1
    while True:
        log.info("ws connecting → %s", url)
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open    = lambda _ws:          log.info("ws open"),
                on_message = lambda _ws, msg:     handle_payload(msg),
                on_error   = lambda _ws, err:     log.warning("ws error: %s", err),
                on_close   = lambda _ws, code, r: log.info("ws closed %s %s", code, r),
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:
            log.warning("ws exception: %s", exc)
        log.info("reconnecting in %ds", retry)
        time.sleep(retry)
        retry = min(retry * 2, 30)

# ── HTTP API ──────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/messages")
def get_messages():
    raw_peer = request.args.get("peer", "").strip()
    try:
        after = int(request.args.get("after", 0))
        limit = min(int(request.args.get("limit", 50)), 1000)
    except ValueError:
        return jsonify({"error": "invalid parameter"}), 400

    if not raw_peer:
        return jsonify({"error": "peer required"}), 400

    peer = normalize_peer(raw_peer)

    with _conn() as db:
        rows = db.execute(
            "SELECT dir, text, server_ts, status, att_id, mime "
            "FROM messages "
            "WHERE peer = ? AND server_ts > ? "
            "ORDER BY server_ts ASC LIMIT ?",
            (peer, after, limit),
        ).fetchall()

    items = []
    for r in rows:
        item = {
            "dir":      r["dir"],
            "text":     r["text"],
            "serverTs": r["server_ts"],
            "status":   r["status"],
        }
        if r["att_id"]:
            item["attId"] = r["att_id"]
            item["mime"]  = r["mime"]
        items.append(item)

    return jsonify({"items": items})


@app.route("/health")
def health():
    return jsonify({"ok": True})

# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not SIGNAL_NUMBER:
        raise SystemExit("SIGNAL_NUMBER env var is required")
    init_db()
    threading.Thread(target=run_ws, daemon=True, name="ws-listener").start()
    log.info("bridge listening on :%d  number=%s  db=%s", PORT, SIGNAL_NUMBER, DB_PATH)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
