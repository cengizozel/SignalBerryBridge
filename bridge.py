"""
SignalBerry Bridge v2

Connects to signal-cli-rest-api via WebSocket, persists messages to SQLite, and
serves them to the SignalBerry Android app over HTTP.

v1 surface (FROZEN — the legacy app on real devices depends on byte-identical
behavior): table `messages`, endpoints GET /messages, POST /read, GET /unread,
and the `ok` field of GET /health.

v2 surface: table `messages_v2` (+ markers/peer_map/orphan_receipts/meta),
a mod_seq change feed (GET /v2/changes), scrollback paging (GET /v2/messages),
app send reporting (POST /v2/sent), server-side read-receipt fan-out
(POST /v2/read-receipts). Design: SignalBerry/docs/REDESIGN.md.

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
from urllib.request import Request, urlopen

import websocket
from flask import Flask, jsonify, request

# ── config ────────────────────────────────────────────────────────────────────

SIGNAL_API_URL = os.environ.get("SIGNAL_API_URL", "http://localhost:5000").rstrip("/")
SIGNAL_NUMBER  = os.environ.get("SIGNAL_NUMBER", "")
DB_PATH        = os.environ.get("DB_PATH", "/data/bridge.db")
PORT           = int(os.environ.get("PORT", "9099"))

SCHEMA_VERSION = 2
# bump when the /v2/changes row shape changes in a way the app must match
API_VERSION = 3
ORPHAN_TTL_MS  = 30 * 24 * 3600 * 1000
CONTACTS_REFRESH_S = 6 * 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

from sigshapes import (  # pure helpers (unit-tested in tests/test_sigshapes.py)
    normalize_peer, is_uuid_key, kind_from_mime,
    attachments_of as _attachments_of, quote_of as _quote_of,
    KIND_BY_MIME, _UUID_RE, _UUID_SEARCH,
)

# runtime state for /health
_state = {
    "ws_connected": False,
    "last_envelope_ms": 0,
}

# ── peer normalisation ────────────────────────────────────────────────────────

def best_peer(number: str, uuid: str) -> str:
    """Normalised peer key, preferring phone number over UUID, consulting peer_map."""
    n = (number or "").strip()
    u = (uuid   or "").strip()
    if n:
        return normalize_peer(n)
    key = normalize_peer(u)
    if key and is_uuid_key(key):
        mapped = peer_map_lookup(key)
        if mapped:
            return mapped
    return key

# ── database ──────────────────────────────────────────────────────────────────

# ONE global write lock: guarantees mod_seq monotone visibility (seq bump and row
# writes commit atomically before any later writer starts) and v1/v2 dual-write
# coherence. All write paths must go through write_tx().
_db_lock = threading.Lock()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _conn() as db:
        # v1 — FROZEN schema
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
        db.execute("CREATE INDEX IF NOT EXISTS idx_peer_ts ON messages(peer, server_ts)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS read_markers (
                peer         TEXT    PRIMARY KEY,
                last_read_ts INTEGER NOT NULL DEFAULT 0
            )
        """)
        # v2
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages_v2 (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                peer         TEXT    NOT NULL,
                dir          TEXT    NOT NULL,
                kind         TEXT    NOT NULL DEFAULT 'text',
                body         TEXT    NOT NULL DEFAULT '',
                server_ts    INTEGER NOT NULL,
                status       INTEGER NOT NULL DEFAULT 1,
                att_id       TEXT    NOT NULL DEFAULT '',
                mime         TEXT    NOT NULL DEFAULT '',
                quote_ts     INTEGER NOT NULL DEFAULT 0,
                quote_author TEXT    NOT NULL DEFAULT '',
                quote_text   TEXT    NOT NULL DEFAULT '',
                reactions    TEXT    NOT NULL DEFAULT '{}',
                edited_ts    INTEGER NOT NULL DEFAULT 0,
                edit_count   INTEGER NOT NULL DEFAULT 0,
                deleted      INTEGER NOT NULL DEFAULT 0,
                mod_seq      INTEGER NOT NULL,
                UNIQUE(peer, dir, server_ts, att_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_v2_seq ON messages_v2(mod_seq)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_v2_peer_ts ON messages_v2(peer, server_ts)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS markers (
                peer         TEXT    PRIMARY KEY,
                last_read_ts INTEGER NOT NULL DEFAULT 0,
                mod_seq      INTEGER NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS peer_map (
                uuid          TEXT PRIMARY KEY,
                number_digits TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS orphan_receipts (
                peer    TEXT    NOT NULL,
                ts      INTEGER NOT NULL,
                status  INTEGER NOT NULL,
                seen_at INTEGER NOT NULL,
                PRIMARY KEY (peer, ts)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS receipt_queue (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient TEXT    NOT NULL,
                ts        INTEGER NOT NULL,
                attempts  INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER)")
        db.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('seq', 0)")
        db.execute(f"INSERT OR IGNORE INTO meta (k, v) VALUES ('schema', {SCHEMA_VERSION})")
        try:
            db.execute("ALTER TABLE messages_v2 ADD COLUMN author TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # already present
        if not db.execute("SELECT 1 FROM messages_v2 LIMIT 1").fetchone():
            _backfill_v2(db)


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


class _Tx:
    """Connection wrapper for write transactions: adds the mod_seq allocator."""

    def __init__(self, con):
        self._con = con

    def execute(self, *a, **kw):
        return self._con.execute(*a, **kw)

    def next_seq(self) -> int:
        self._con.execute("UPDATE meta SET v = v + 1 WHERE k = 'seq'")
        return self._con.execute("SELECT v FROM meta WHERE k = 'seq'").fetchone()[0]


@contextmanager
def write_tx():
    """All writes: one global lock, one transaction, seq allocator attached."""
    with _db_lock, _conn() as db:
        yield _Tx(db)


def peer_map_lookup(uuid_key: str) -> str:
    with _conn() as db:
        row = db.execute(
            "SELECT number_digits FROM peer_map WHERE uuid = ?", (uuid_key,)
        ).fetchone()
        return row["number_digits"] if row else ""


def learn_mapping(uuid: str, number: str, db=None):
    """Record uuid→number and re-key any uuid-keyed rows. Idempotent.
    Pass the active _Tx when already inside write_tx() — the lock is not
    reentrant and nesting would deadlock."""
    u = normalize_peer(uuid)
    n = normalize_peer(number)
    if not u or not n or not is_uuid_key(u) or is_uuid_key(n):
        return
    if db is None:
        with write_tx() as tx:
            _learn_mapping_in_tx(tx, u, n)
    else:
        _learn_mapping_in_tx(db, u, n)


def _learn_mapping_in_tx(db, u: str, n: str):
    existing = db.execute(
        "SELECT number_digits FROM peer_map WHERE uuid = ?", (u,)
    ).fetchone()
    if existing and existing["number_digits"] == n:
        return
    db.execute(
        "INSERT INTO peer_map (uuid, number_digits) VALUES (?, ?) "
        "ON CONFLICT(uuid) DO UPDATE SET number_digits = excluded.number_digits",
        (u, n),
    )
    _rekey(db, u, n)
    log.info("peer_map learned %s -> %s", u, n)


def _rekey(db, old_key: str, new_key: str):
    """Merge old_key rows into new_key across v2 tables, bumping mod_seq."""
    rows = db.execute(
        "SELECT * FROM messages_v2 WHERE peer = ?", (old_key,)
    ).fetchall()
    for r in rows:
        seq = db.next_seq()
        moved = db.execute(
            "UPDATE OR IGNORE messages_v2 SET peer = ?, mod_seq = ? WHERE id = ?",
            (new_key, seq, r["id"]),
        )
        if moved.rowcount == 0:
            # collision: merge into the occupant, drop the uuid row
            occ = db.execute(
                "SELECT * FROM messages_v2 WHERE peer=? AND dir=? AND server_ts=? AND att_id=?",
                (new_key, r["dir"], r["server_ts"], r["att_id"]),
            ).fetchone()
            if occ:
                merged_reactions = _merge_reactions(occ["reactions"], r["reactions"])
                seq2 = db.next_seq()
                db.execute(
                    "UPDATE messages_v2 SET status=MAX(status,?), reactions=?, "
                    "deleted=MAX(deleted,?), mod_seq=? WHERE id=?",
                    (r["status"], merged_reactions, r["deleted"], seq2, occ["id"]),
                )
            db.execute("DELETE FROM messages_v2 WHERE id = ?", (r["id"],))
    # markers: keep the max watermark
    old_m = db.execute("SELECT last_read_ts FROM markers WHERE peer = ?", (old_key,)).fetchone()
    if old_m:
        seq = db.next_seq()
        db.execute(
            "INSERT INTO markers (peer, last_read_ts, mod_seq) VALUES (?, ?, ?) "
            "ON CONFLICT(peer) DO UPDATE SET "
            "last_read_ts = MAX(last_read_ts, excluded.last_read_ts), mod_seq = excluded.mod_seq",
            (new_key, old_m["last_read_ts"], seq),
        )
        db.execute("DELETE FROM markers WHERE peer = ?", (old_key,))
    # orphans move wholesale (no merge semantics needed: PK collision keeps newer)
    db.execute("UPDATE OR IGNORE orphan_receipts SET peer = ? WHERE peer = ?", (new_key, old_key))
    db.execute("DELETE FROM orphan_receipts WHERE peer = ?", (old_key,))


def _merge_reactions(a: str, b: str) -> str:
    try:
        m = json.loads(a or "{}")
        m.update(json.loads(b or "{}"))
        return json.dumps(m)
    except Exception:
        return a or "{}"

# ── v1 write paths (FROZEN semantics) ─────────────────────────────────────────

def _v1_store(db, peer, direction, text, ts, status=1, att_id="", mime=""):
    db.execute(
        "INSERT INTO messages (peer, dir, text, server_ts, status, att_id, mime) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (peer, direction, text or "", ts, status, att_id or "", mime or ""),
    )


def _v1_upgrade_status(db, peer, new_status):
    db.execute(
        "UPDATE messages SET status = ? "
        "WHERE id = ("
        "  SELECT id FROM messages "
        "  WHERE peer = ? AND dir = 'out' AND status < ? "
        "  ORDER BY server_ts DESC LIMIT 1"
        ")",
        (new_status, peer, new_status),
    )

# ── v2 write paths ────────────────────────────────────────────────────────────

def _v2_upsert_message(db, peer, dir_, kind, body, server_ts, status,
                       att_id="", mime="", quote_ts=0, quote_author="", quote_text="",
                       author=""):
    """INSERT OR IGNORE + status-bump UPDATE; idempotent. Returns True if inserted."""
    seq = db.next_seq()
    cur = db.execute(
        "INSERT OR IGNORE INTO messages_v2 "
        "(peer, dir, kind, body, server_ts, status, att_id, mime, "
        " quote_ts, quote_author, quote_text, mod_seq, author) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (peer, dir_, kind, body or "", server_ts, status, att_id or "", mime or "",
         quote_ts, quote_author or "", quote_text or "", seq, author or ""),
    )
    inserted = cur.rowcount > 0
    if not inserted:
        seq = db.next_seq()
        db.execute(
            "UPDATE messages_v2 SET status = MAX(status, ?), mod_seq = ? "
            "WHERE peer=? AND dir=? AND server_ts=? AND att_id=? AND deleted=0",
            (status, seq, peer, dir_, server_ts, att_id or ""),
        )
    if inserted and dir_ == "out":
        _apply_orphan_receipts(db, peer, server_ts)
    return inserted


def _apply_orphan_receipts(db, peer, ts, row_id=None):
    """Apply a stored orphan receipt keyed (peer, ts). Targets the row at
    server_ts=ts unless row_id pins a specific row (edit case: receipt keyed by
    the edit envelope ts, row identity is the original ts)."""
    row = db.execute(
        "SELECT status FROM orphan_receipts WHERE peer = ? AND ts = ?", (peer, ts)
    ).fetchone()
    if not row:
        return
    seq = db.next_seq()
    if row_id is not None:
        db.execute(
            "UPDATE messages_v2 SET status = MAX(status, ?), mod_seq = ? "
            "WHERE id = ? AND dir='out' AND deleted=0",
            (row["status"], seq, row_id),
        )
    else:
        db.execute(
            "UPDATE messages_v2 SET status = MAX(status, ?), mod_seq = ? "
            "WHERE peer=? AND dir='out' AND server_ts=? AND deleted=0",
            (row["status"], seq, peer, ts),
        )
    db.execute("DELETE FROM orphan_receipts WHERE peer = ? AND ts = ?", (peer, ts))
    log.info("orphan receipt applied peer=%s ts=%d -> %d", peer, ts, row["status"])


def _v2_match_target(db, peer, target_ts):
    """Rows whose identity OR latest edit matches target_ts (chained edits)."""
    return db.execute(
        "SELECT * FROM messages_v2 WHERE peer = ? AND (server_ts = ? OR edited_ts = ?)",
        (peer, target_ts, target_ts),
    ).fetchall()


def _v2_apply_receipt(db, peer, ts, new_status):
    # group sends live under "group:<id>", but their receipts arrive from
    # individual members — match those by timestamp across group threads
    cur = db.execute(
        "UPDATE messages_v2 SET status = MAX(status, ?), mod_seq = ? "
        "WHERE dir='out' AND deleted=0 AND peer LIKE 'group:%' "
        "AND (server_ts = ? OR edited_ts = ?)",
        (new_status, db.next_seq(), ts, ts),
    )
    if cur.rowcount > 0:
        return
    rows = _v2_match_target(db, peer, ts)
    matched = False
    for r in rows:
        if r["dir"] != "out" or r["deleted"]:
            continue
        matched = True
        seq = db.next_seq()
        db.execute(
            "UPDATE messages_v2 SET status = MAX(status, ?), mod_seq = ? WHERE id = ?",
            (new_status, seq, r["id"]),
        )
    if not matched:
        db.execute(
            "INSERT INTO orphan_receipts (peer, ts, status, seen_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(peer, ts) DO UPDATE SET status = MAX(status, excluded.status)",
            (peer, ts, new_status, int(time.time() * 1000)),
        )


def _v2_apply_reaction(db, peer, dir_, target_ts, author_key, emoji, is_remove):
    for r in _v2_match_target(db, peer, target_ts):
        if r["dir"] != dir_:
            continue
        try:
            m = json.loads(r["reactions"] or "{}")
        except Exception:
            m = {}
        if is_remove:
            m.pop(author_key, None)
        else:
            m[author_key] = emoji
        seq = db.next_seq()
        db.execute(
            "UPDATE messages_v2 SET reactions = ?, mod_seq = ? WHERE id = ?",
            (json.dumps(m), seq, r["id"]),
        )


def _v2_apply_edit(db, peer, target_ts, new_body, edit_ts):
    rows = _v2_match_target(db, peer, target_ts)
    first = True
    for r in rows:
        seq = db.next_seq()
        db.execute(
            "UPDATE messages_v2 SET body = ?, edited_ts = ?, edit_count = edit_count + 1, "
            "mod_seq = ? WHERE id = ?",
            (new_body if first or r["kind"] == "text" else r["body"], edit_ts, seq, r["id"]),
        )
        first = False
        # a receipt for the edit envelope may have arrived before this edit
        if r["dir"] == "out":
            _apply_orphan_receipts(db, peer, edit_ts, row_id=r["id"])


def _v2_apply_delete(db, peer, target_ts):
    for r in _v2_match_target(db, peer, target_ts):
        seq = db.next_seq()
        db.execute(
            "UPDATE messages_v2 SET deleted = 1, mod_seq = ? WHERE id = ?",
            (seq, r["id"]),
        )


def _v2_set_marker(db, peer, ts):
    seq = db.next_seq()
    db.execute(
        "INSERT INTO markers (peer, last_read_ts, mod_seq) VALUES (?, ?, ?) "
        "ON CONFLICT(peer) DO UPDATE SET "
        "last_read_ts = MAX(last_read_ts, excluded.last_read_ts), mod_seq = excluded.mod_seq",
        (peer, ts, seq),
    )

# ── v1→v2 backfill ────────────────────────────────────────────────────────────

def _backfill_v2(db):
    """Populate empty messages_v2 from legacy messages. Caption-split pairs
    (text row + att rows at the same (peer,dir,ts)) coalesce into the att row."""
    log.info("backfilling messages_v2 from v1 ...")

    def bf_seq():
        db.execute("UPDATE meta SET v = v + 1 WHERE k = 'seq'")
        return db.execute("SELECT v FROM meta WHERE k = 'seq'").fetchone()[0]

    rows = db.execute(
        "SELECT peer, dir, text, server_ts, status, att_id, mime FROM messages "
        "ORDER BY server_ts ASC, id ASC"
    ).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r["peer"], r["dir"], r["server_ts"]), []).append(r)

    n = 0
    for (peer, dir_, ts), grp in groups.items():
        texts = [r for r in grp if not r["att_id"]]
        atts  = [r for r in grp if r["att_id"]]
        caption = ""
        for t in texts:
            if t["text"] and t["text"] != "null":
                caption = t["text"]
                break
        if atts:
            first = True
            for a in atts:
                db.execute(
                    "INSERT OR IGNORE INTO messages_v2 "
                    "(peer, dir, kind, body, server_ts, status, att_id, mime, mod_seq) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (peer, dir_, kind_from_mime(a["mime"]),
                     caption if first else "", ts, a["status"],
                     a["att_id"], a["mime"], bf_seq()),
                )
                first = False
                n += 1
        elif caption:
            db.execute(
                "INSERT OR IGNORE INTO messages_v2 "
                "(peer, dir, kind, body, server_ts, status, att_id, mime, mod_seq) "
                "VALUES (?, ?, 'text', ?, ?, ?, '', '', ?)",
                (peer, dir_, caption, ts, grp[0]["status"], bf_seq()),
            )
            n += 1
    for m in db.execute("SELECT peer, last_read_ts FROM read_markers").fetchall():
        db.execute(
            "INSERT OR IGNORE INTO markers (peer, last_read_ts, mod_seq) VALUES (?, ?, ?)",
            (m["peer"], m["last_read_ts"], bf_seq()),
        )
    log.info("backfill done: %d v2 rows", n)

# ── envelope parsing ──────────────────────────────────────────────────────────

def _self_keys():
    return {normalize_peer(SIGNAL_NUMBER)}


def handle_envelope(env: dict):
    _state["last_envelope_ms"] = int(time.time() * 1000)

    src_num  = env.get("sourceNumber") or env.get("source") or ""
    src_uuid = env.get("sourceUuid") or ""
    ts       = env.get("timestamp") or int(time.time() * 1000)

    if src_num and src_uuid:
        learn_mapping(src_uuid, src_num)

    data = env.get("dataMessage")
    sync = env.get("syncMessage")
    receipt = env.get("receiptMessage")
    edit_env = env.get("editMessage")  # nesting pinned in M1; tolerate both shapes

    with write_tx() as db:
        if data:
            group = data.get("groupInfo") or {}
            if not group:
                # edit envelopes nest the group marker inside the new revision
                _em = data.get("editMessage") or edit_env or {}
                group = (_em.get("dataMessage") or {}).get("groupInfo") or {}
            gid = (group.get("groupId") or "").strip()
            if group and not gid:
                return
            sender = best_peer(src_num, src_uuid)
            # groups: thread keyed by group id; the sender becomes the row author
            peer = ("group:" + gid) if gid else sender
            author = sender if gid else ""

            reaction = data.get("reaction")
            if reaction:
                target_ts = reaction.get("targetSentTimestamp") or 0
                target_author = normalize_peer(reaction.get("targetAuthor") or "")
                dir_ = "out" if target_author in _self_keys() else "in"
                _v2_apply_reaction(db, peer, dir_, target_ts,
                                   "peer:" + sender,
                                   reaction.get("emoji") or "",
                                   bool(reaction.get("isRemove")))
                return

            rd = data.get("remoteDelete")
            if rd:
                _v2_apply_delete(db, peer, rd.get("timestamp") or 0)
                return

            em = data.get("editMessage") or edit_env
            if em:
                target = em.get("targetSentTimestamp") or 0
                inner = em.get("dataMessage") or {}
                new_body = (inner.get("message") or "").strip()
                if target and new_body:
                    _v2_apply_edit(db, peer, target, new_body, ts)
                return

            text = (data.get("message") or data.get("text") or "").strip()
            atts = _attachments_of(data)
            q_ts, q_author, q_text = _quote_of(data)

            # v1 frozen semantics (v1 never understood groups — keep it that way)
            if not gid:
                if text:
                    _v1_store(db, peer, "in", text, ts, status=2)
                for cid, mime in atts:
                    if mime.startswith("image/"):
                        _v1_store(db, peer, "in", "", ts, status=2, att_id=cid, mime=mime)

            # v2 semantics: caption merged onto first attachment row
            if atts:
                first = True
                for cid, mime in atts:
                    _v2_upsert_message(db, peer, "in", kind_from_mime(mime),
                                       text if first else "", ts, 2, cid, mime,
                                       q_ts, q_author, q_text, author=author)
                    first = False
            elif text:
                _v2_upsert_message(db, peer, "in", "text", text, ts, 2,
                                   quote_ts=q_ts, quote_author=q_author, quote_text=q_text,
                                   author=author)

        if sync:
            sent = sync.get("sentMessage") or {}
            if sent:
                s_group = sent.get("groupInfo") or {}
                if not s_group:
                    _sem = sent.get("editMessage") or {}
                    s_group = (_sem.get("dataMessage") or {}).get("groupInfo") or {}
                s_gid = (s_group.get("groupId") or "").strip()
                if s_group and not s_gid:
                    return
                dest_n = sent.get("destinationNumber") or sent.get("destination") or ""
                dest_u = sent.get("destinationUuid") or ""
                if dest_n and dest_u:
                    learn_mapping(dest_u, dest_n, db=db)
                peer = ("group:" + s_gid) if s_gid else best_peer(dest_n, dest_u)
                sent_ts = sent.get("timestamp") or ts

                reaction = sent.get("reaction")
                if reaction and peer:
                    target_ts = reaction.get("targetSentTimestamp") or 0
                    author = normalize_peer(reaction.get("targetAuthor") or "")
                    dir_ = "out" if author in _self_keys() else "in"
                    _v2_apply_reaction(db, peer, dir_, target_ts, "me",
                                       reaction.get("emoji") or "",
                                       bool(reaction.get("isRemove")))
                    return

                em = sent.get("editMessage")
                if em and peer:
                    target = em.get("targetSentTimestamp") or 0
                    inner = em.get("dataMessage") or {}
                    new_body = (inner.get("message") or "").strip()
                    if target and new_body:
                        _v2_apply_edit(db, peer, target, new_body, sent_ts)
                    return

                rd = sent.get("remoteDelete")
                if rd and peer:
                    _v2_apply_delete(db, peer, rd.get("timestamp") or 0)
                    return

                text = (sent.get("message") or "").strip()
                atts = _attachments_of(sent)
                q_ts, q_author, q_text = _quote_of(sent)
                # Note-to-Self echoes are delivered by definition; no delivery
                # receipts ever arrive in the self thread to upgrade them
                out_status = 2 if peer in _self_keys() else 1

                if peer:
                    # v1 frozen (never group rows)
                    if not s_gid:
                        if text:
                            _v1_store(db, peer, "out", text, sent_ts, status=1)
                        for cid, mime in atts:
                            if mime.startswith("image/"):
                                _v1_store(db, peer, "out", "", sent_ts, status=1,
                                          att_id=cid, mime=mime)
                    # v2
                    if atts:
                        first = True
                        for cid, mime in atts:
                            _v2_upsert_message(db, peer, "out", kind_from_mime(mime),
                                               text if first else "", sent_ts, out_status,
                                               cid, mime, q_ts, q_author, q_text)
                            first = False
                    elif text:
                        _v2_upsert_message(db, peer, "out", "text", text, sent_ts, out_status,
                                           quote_ts=q_ts, quote_author=q_author,
                                           quote_text=q_text)

            # phone read a chat → marker event for app badge clearing
            for rm in (sync.get("readMessages") or []):
                rm_peer = best_peer(rm.get("senderNumber") or rm.get("sender") or "",
                                    rm.get("senderUuid") or "")
                rm_ts = rm.get("timestamp") or 0
                if rm_peer and rm_ts:
                    _v2_set_marker(db, rm_peer, rm_ts)

        if receipt:
            peer = best_peer(src_num, src_uuid)
            if receipt.get("isDelivery"):
                new_status = 2
            elif receipt.get("isRead") or receipt.get("isViewed"):
                new_status = 3
            else:
                new_status = 0
            if new_status and peer:
                # v1 frozen: newest-row guess
                _v1_upgrade_status(db, peer, new_status)
                # v2 exact
                for t in (receipt.get("timestamps") or []):
                    _v2_apply_receipt(db, peer, t, new_status)


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
                try:
                    handle_envelope(env)
                except Exception:
                    log.exception("envelope handling failed")

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

    def on_open(_ws):
        nonlocal retry
        retry = 1
        _state["ws_connected"] = True
        log.info("ws open")

    def on_close(_ws, code, reason):
        _state["ws_connected"] = False
        log.info("ws closed %s %s", code, reason)

    def on_error(_ws, err):
        _state["ws_connected"] = False
        log.warning("ws error: %s", err)

    while True:
        log.info("ws connecting → %s", url)
        try:
            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=lambda _ws, msg: handle_payload(msg),
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as exc:
            log.warning("ws exception: %s", exc)
        _state["ws_connected"] = False
        log.info("reconnecting in %ds", retry)
        time.sleep(retry)
        retry = min(retry * 2, 30)

# ── signal-api helpers (receipt fan-out, contacts refresh) ───────────────────

def _api_post(path: str, body: dict, timeout=15) -> int:
    req = Request(SIGNAL_API_URL + path,
                  data=json.dumps(body).encode(),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.status


def _api_get(path: str, timeout=15):
    with urlopen(SIGNAL_API_URL + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def receipt_fanout_worker():
    """Drain receipt_queue: one signal-api POST /v1/receipts per row."""
    number = quote(SIGNAL_NUMBER, safe="")
    while True:
        try:
            with _conn() as db:
                rows = db.execute(
                    "SELECT id, recipient, ts, attempts FROM receipt_queue "
                    "ORDER BY id LIMIT 20"
                ).fetchall()
            if not rows:
                time.sleep(2)
                continue
            for r in rows:
                try:
                    status = _api_post(f"/v1/receipts/{number}", {
                        "receipt_type": "read",
                        "recipient": r["recipient"],
                        "timestamp": r["ts"],
                    })
                    ok = 200 <= status < 300
                except Exception as exc:
                    log.warning("receipt fan-out failed (ts=%d): %s", r["ts"], exc)
                    ok = False
                with write_tx() as db:
                    if ok or r["attempts"] >= 5:
                        db.execute("DELETE FROM receipt_queue WHERE id = ?", (r["id"],))
                    else:
                        db.execute(
                            "UPDATE receipt_queue SET attempts = attempts + 1 WHERE id = ?",
                            (r["id"],),
                        )
                if not ok:
                    time.sleep(min(2 ** r["attempts"], 60))
        except Exception:
            log.exception("receipt fan-out worker error")
            time.sleep(5)


def contacts_refresh_worker():
    number = quote(SIGNAL_NUMBER, safe="")
    while True:
        try:
            contacts = _api_get(f"/v1/contacts/{number}", timeout=30)
            n = 0
            for c in contacts or []:
                num = c.get("number") or ""
                uid = c.get("uuid") or ""
                if num and uid:
                    learn_mapping(uid, num)
                    n += 1
            log.info("contacts refresh: %d mappings", n)
        except Exception as exc:
            log.warning("contacts refresh failed: %s", exc)
        time.sleep(CONTACTS_REFRESH_S)


def orphan_gc_worker():
    while True:
        try:
            cutoff = int(time.time() * 1000) - ORPHAN_TTL_MS
            with write_tx() as db:
                db.execute("DELETE FROM orphan_receipts WHERE seen_at < ?", (cutoff,))
        except Exception:
            log.exception("orphan gc error")
        time.sleep(24 * 3600)

# ── HTTP API ──────────────────────────────────────────────────────────────────

app = Flask(__name__)


# ---- v1 endpoints (FROZEN — do not change response shapes) ----

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


@app.route("/read", methods=["POST"])
def mark_read():
    raw_peer = request.args.get("peer", "").strip()
    try:
        ts = int(request.args.get("ts", 0))
    except ValueError:
        return jsonify({"error": "invalid ts"}), 400
    if not raw_peer:
        return jsonify({"error": "peer required"}), 400

    peer = normalize_peer(raw_peer)
    with write_tx() as db:
        db.execute(
            "INSERT INTO read_markers (peer, last_read_ts) VALUES (?, ?) "
            "ON CONFLICT(peer) DO UPDATE SET last_read_ts = MAX(last_read_ts, excluded.last_read_ts)",
            (peer, ts),
        )
    return jsonify({"ok": True})


@app.route("/unread")
def get_unread():
    with _conn() as db:
        peers = db.execute(
            "SELECT DISTINCT peer FROM messages WHERE dir = 'in'"
        ).fetchall()

        result = {}
        for row in peers:
            peer = row["peer"]
            marker = db.execute(
                "SELECT last_read_ts FROM read_markers WHERE peer = ?", (peer,)
            ).fetchone()
            last_read_ts = marker["last_read_ts"] if marker else 0
            count = db.execute(
                "SELECT COUNT(*) AS cnt FROM messages "
                "WHERE peer = ? AND dir = 'in' AND server_ts > ?",
                (peer, last_read_ts),
            ).fetchone()["cnt"]
            result[peer] = count

    return jsonify(result)


@app.route("/health")
def health():
    now = int(time.time() * 1000)
    age = (now - _state["last_envelope_ms"]) // 1000 if _state["last_envelope_ms"] else -1
    return jsonify({
        "ok": True,                       # frozen field
        "schema": SCHEMA_VERSION,
        "api_version": API_VERSION,       # app warns on mismatch
        "ws_connected": _state["ws_connected"],
        "account_present": bool(SIGNAL_NUMBER),
        "last_envelope_age_s": age,
    })


# ---- v2 endpoints ----

def _v2_row_json(r, include_uuid=True):
    item = {
        "peer": r["peer"],
        "dir": r["dir"],
        "kind": r["kind"],
        "body": r["body"],
        "serverTs": r["server_ts"],
        "status": r["status"],
        "modSeq": r["mod_seq"],
    }
    try:
        if r["author"]:
            item["author"] = r["author"]
    except (IndexError, KeyError):
        pass
    if r["att_id"]:
        item["attId"] = r["att_id"]
        item["mime"] = r["mime"]
    if r["quote_ts"]:
        item["quoteTs"] = r["quote_ts"]
        item["quoteAuthor"] = r["quote_author"]
        item["quoteText"] = r["quote_text"]
    if r["reactions"] != "{}":
        item["reactions"] = json.loads(r["reactions"])
    else:
        item["reactions"] = {}  # explicit empty: reaction REMOVAL must propagate
    if r["edited_ts"]:
        item["editedTs"] = r["edited_ts"]
        item["editCount"] = r["edit_count"]
    if r["deleted"]:
        item["deleted"] = True
    return item


@app.route("/v2/changes")
def v2_changes():
    try:
        since = int(request.args.get("since_seq", 0))
        limit = min(int(request.args.get("limit", 200)), 1000)
    except ValueError:
        return jsonify({"error": "invalid parameter"}), 400

    with _conn() as db:
        # one BEGIN so items/markers/max_seq share a single WAL snapshot —
        # separate autocommit SELECTs let a concurrent ingest land between
        # the page and max_seq, and a client trusting max_seq would skip it
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT * FROM messages_v2 WHERE mod_seq > ? ORDER BY mod_seq ASC LIMIT ?",
            (since, limit),
        ).fetchall()
        marker_rows = db.execute(
            "SELECT peer, last_read_ts, mod_seq FROM markers WHERE mod_seq > ? "
            "ORDER BY mod_seq ASC LIMIT ?",
            (since, limit),
        ).fetchall()
        max_seq = db.execute("SELECT v FROM meta WHERE k = 'seq'").fetchone()[0]
        # uuid mapping hints for any uuid-keyed peers in this page
        uuid_keys = {r["peer"] for r in rows if is_uuid_key(r["peer"])}
        mappings = {}
        for u in uuid_keys:
            m = db.execute(
                "SELECT number_digits FROM peer_map WHERE uuid = ?", (u,)
            ).fetchone()
            if m:
                mappings[u] = m["number_digits"]

    return jsonify({
        "items": [_v2_row_json(r) for r in rows],
        "markers": [{"peer": m["peer"], "lastReadTs": m["last_read_ts"],
                     "modSeq": m["mod_seq"]} for m in marker_rows],
        "peerMap": mappings,
        "max_seq": max_seq,
    })


@app.route("/v2/messages")
def v2_messages():
    raw_peer = request.args.get("peer", "").strip()
    try:
        before_ts = int(request.args.get("before_ts", 0)) or None
        before_id = int(request.args.get("before_id", 0)) or None
        limit = min(int(request.args.get("limit", 50)), 500)
    except ValueError:
        return jsonify({"error": "invalid parameter"}), 400
    if not raw_peer:
        return jsonify({"error": "peer required"}), 400

    peer = normalize_peer(raw_peer)
    with _conn() as db:
        if before_ts is not None and before_id is not None:
            rows = db.execute(
                "SELECT * FROM messages_v2 WHERE peer = ? "
                "AND (server_ts < ? OR (server_ts = ? AND id < ?)) "
                "ORDER BY server_ts DESC, id DESC LIMIT ?",
                (peer, before_ts, before_ts, before_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM messages_v2 WHERE peer = ? "
                "ORDER BY server_ts DESC, id DESC LIMIT ?",
                (peer, limit),
            ).fetchall()

    items = []
    for r in rows:
        j = _v2_row_json(r)
        j["id"] = r["id"]
        items.append(j)
    return jsonify({"items": items})


@app.route("/v2/sent", methods=["POST"])
def v2_sent():
    body = request.get_json(silent=True) or {}
    peer = normalize_peer(body.get("peer") or "")
    try:
        server_ts = int(body.get("server_ts") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid server_ts"}), 400
    if not peer or not server_ts:
        return jsonify({"error": "peer and server_ts required"}), 400

    kind = body.get("kind") or "text"
    text = (body.get("body") or "").strip()
    att_id = body.get("att_id") or ""
    mime = body.get("mime") or ""
    try:
        quote_ts = int(body.get("quote_ts") or 0)
    except (TypeError, ValueError):
        quote_ts = 0

    with write_tx() as db:
        if body.get("edited_ts"):
            # app-originated edit (no self-echo carries it here)
            _v2_apply_edit(db, peer, server_ts,
                           (body.get("body") or "").strip(),
                           int(body.get("edited_ts")))
            return jsonify({"ok": True})
        if body.get("deleted"):
            # app-originated remote delete: no self-echo exists to carry it here
            _v2_apply_delete(db, peer, server_ts)
            return jsonify({"ok": True})
        _v2_upsert_message(db, peer, "out", kind, text, server_ts, 1,
                           att_id, mime, quote_ts,
                           normalize_peer(body.get("quote_author") or ""),
                           body.get("quote_text") or "")
        # dual-write v1 with old-echo semantics so the legacy app sees this send
        exists = db.execute(
            "SELECT 1 FROM messages WHERE peer=? AND dir='out' AND server_ts=? "
            "AND text=? AND att_id=?",
            (peer, server_ts, text if not att_id else "", att_id),
        ).fetchone()
        if not exists:
            if att_id and mime.startswith("image/"):
                _v1_store(db, peer, "out", "", server_ts, status=1,
                          att_id=att_id, mime=mime)
            elif text:
                _v1_store(db, peer, "out", text, server_ts, status=1)
    return jsonify({"ok": True})


@app.route("/v2/read-receipts", methods=["POST"])
def v2_read_receipts():
    body = request.get_json(silent=True) or {}
    peer = normalize_peer(body.get("peer") or "")
    recipient = (body.get("recipient") or body.get("peer") or "").strip()
    timestamps = body.get("timestamps") or []
    if not peer or not recipient or not isinstance(timestamps, list) or not timestamps:
        return jsonify({"error": "peer/recipient/timestamps required"}), 400
    if normalize_peer(recipient) in _self_keys():
        return jsonify({"ok": True, "skipped": "self-thread"})

    clean = []
    for t in timestamps[:100]:
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        if t > 0:
            clean.append(t)

    with write_tx() as db:
        for t in clean:
            db.execute(
                "INSERT INTO receipt_queue (recipient, ts) VALUES (?, ?)",
                (recipient, t),
            )
        if clean:
            _v2_set_marker(db, peer, max(clean))
    return jsonify({"ok": True, "queued": len(clean)})

@app.route("/v2/purge", methods=["POST"])
def v2_purge():
    """Wipe message data — everything, or one peer's thread when `peer` is
    given. The mod_seq counter in `meta` is deliberately PRESERVED: clients
    hold their cursor in prefs, and a counter reset would strand them past the
    new head. Never touches signal-api account state — the device link
    survives. VACUUM afterwards so deleted content doesn't linger in free
    pages / WAL (a purge should mean the bytes are gone)."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "purge":
        return jsonify({"error": "confirm required"}), 400
    peer = normalize_peer(body.get("peer") or "")
    with write_tx() as db:
        if peer:
            for t in ("messages", "messages_v2", "read_markers", "markers",
                      "orphan_receipts"):
                db.execute("DELETE FROM " + t + " WHERE peer = ?", (peer,))
            for row in db.execute("SELECT id, recipient FROM receipt_queue").fetchall():
                if normalize_peer(row["recipient"]) == peer:
                    db.execute("DELETE FROM receipt_queue WHERE id = ?", (row["id"],))
        else:
            for t in ("messages", "messages_v2", "read_markers", "markers",
                      "orphan_receipts", "receipt_queue", "peer_map"):
                db.execute("DELETE FROM " + t)
    with _conn() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
    log.warning("PURGE: %s wiped on request", peer or "ALL message data")
    return jsonify({"ok": True})


# ── entry point ───────────────────────────────────────────────────────────────

def start_workers():
    threading.Thread(target=run_ws, daemon=True, name="ws-listener").start()
    threading.Thread(target=receipt_fanout_worker, daemon=True, name="receipt-fanout").start()
    threading.Thread(target=contacts_refresh_worker, daemon=True, name="contacts-refresh").start()
    threading.Thread(target=orphan_gc_worker, daemon=True, name="orphan-gc").start()


if __name__ == "__main__":
    if not SIGNAL_NUMBER:
        raise SystemExit("SIGNAL_NUMBER env var is required")
    init_db()
    start_workers()
    log.info("bridge v2 listening on :%d  number=%s  db=%s", PORT, SIGNAL_NUMBER, DB_PATH)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        log.warning("waitress not installed; using Flask dev server")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
