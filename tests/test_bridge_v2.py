"""Bridge v2 behavior: envelope handling, change feed, dedup, receipts,
reactions, edits, deletes, re-keying, /v2/sent, v1 contract freeze."""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from mock_signal_api import (  # noqa: E402
    data_message, sync_sent, receipt, attachment, PEER_NUMBER, PEER_UUID,
)

PEER_KEY = "15550001111"  # digits of PEER_NUMBER


def env(frame):
    return frame["envelope"]


def changes(client, since=0):
    r = client.get(f"/v2/changes?since_seq={since}&limit=1000")
    assert r.status_code == 200
    return r.get_json()


def drain_items(client):
    return changes(client)["items"]


# ── ingestion ────────────────────────────────────────────────────────────────

def test_incoming_text(bridge, client):
    bridge.handle_envelope(env(data_message(text="hello", ts=1000)))
    items = drain_items(client)
    assert len(items) == 1
    it = items[0]
    assert it["peer"] == PEER_KEY and it["dir"] == "in"
    assert it["kind"] == "text" and it["body"] == "hello"
    assert it["serverTs"] == 1000 and it["status"] == 2


def test_incoming_redelivery_is_idempotent(bridge, client):
    frame = data_message(text="dup", ts=2000)
    bridge.handle_envelope(env(frame))
    bridge.handle_envelope(env(frame))
    assert len(drain_items(client)) == 1


def test_caption_and_attachment_one_row(bridge, client):
    bridge.handle_envelope(env(data_message(
        text="look at this", ts=3000,
        attachments=[attachment("att-1", "image/jpeg")])))
    items = drain_items(client)
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "image" and it["body"] == "look at this"
    assert it["attId"] == "att-1"


def test_multi_attachment_caption_on_first(bridge, client):
    bridge.handle_envelope(env(data_message(
        text="three pics", ts=4000,
        attachments=[attachment(f"att-{i}", "image/png") for i in range(3)])))
    items = sorted(drain_items(client), key=lambda i: i["attId"])
    assert len(items) == 3
    assert items[0]["body"] == "three pics"
    assert items[1]["body"] == "" and items[2]["body"] == ""


def test_attachment_only_never_null_text(bridge, client):
    bridge.handle_envelope(env(data_message(
        ts=5000, attachments=[attachment("att-x", "image/jpeg")])))
    it = drain_items(client)[0]
    assert it["body"] == ""  # not None, not "null"


def test_video_and_file_kinds(bridge, client):
    bridge.handle_envelope(env(data_message(
        ts=6000, attachments=[attachment("v1", "video/mp4")])))
    bridge.handle_envelope(env(data_message(
        ts=6001, attachments=[attachment("f1", "application/pdf")])))
    kinds = {i["attId"]: i["kind"] for i in drain_items(client)}
    assert kinds == {"v1": "video", "f1": "file"}


def test_group_messages_skipped(bridge, client):
    frame = data_message(text="group chatter", ts=7000)
    frame["envelope"]["dataMessage"]["groupInfo"] = {"groupId": "abc"}
    bridge.handle_envelope(env(frame))
    assert drain_items(client) == []


def test_quote_passthrough(bridge, client):
    frame = data_message(text="replying", ts=8000,
                         quote={"id": 7500, "author": PEER_NUMBER, "text": "original"})
    bridge.handle_envelope(env(frame))
    it = drain_items(client)[0]
    assert it["quoteTs"] == 7500 and it["quoteText"] == "original"


def test_sync_sent_out_row(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="from my phone", ts=9000)))
    it = drain_items(client)[0]
    assert it["dir"] == "out" and it["status"] == 1 and it["body"] == "from my phone"


# ── receipts ─────────────────────────────────────────────────────────────────

def test_receipt_exact_match(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="a", ts=10000)))
    bridge.handle_envelope(env(sync_sent(text="b", ts=10001)))
    bridge.handle_envelope(env(receipt([10000], kind="delivery")))
    by_ts = {i["serverTs"]: i["status"] for i in drain_items(client)}
    assert by_ts[10000] == 2 and by_ts[10001] == 1  # ONLY the targeted row


def test_delivery_after_read_never_downgrades(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="x", ts=11000)))
    bridge.handle_envelope(env(receipt([11000], kind="read")))
    bridge.handle_envelope(env(receipt([11000], kind="delivery")))  # late delivery
    assert drain_items(client)[0]["status"] == 3


def test_orphan_receipt_applied_on_late_sent_report(bridge, client):
    # receipt arrives BEFORE the app reports its send
    bridge.handle_envelope(env(receipt([12000], kind="read")))
    assert drain_items(client) == []  # nothing matched, orphaned silently
    r = client.post("/v2/sent", json={
        "peer": PEER_NUMBER, "kind": "text", "body": "late report",
        "server_ts": 12000})
    assert r.status_code == 200
    it = drain_items(client)[0]
    assert it["status"] == 3  # orphan applied at insert


def test_receipt_multiple_timestamps(bridge, client):
    for ts in (13000, 13001, 13002):
        bridge.handle_envelope(env(sync_sent(text=f"m{ts}", ts=ts)))
    bridge.handle_envelope(env(receipt([13000, 13002], kind="delivery")))
    by_ts = {i["serverTs"]: i["status"] for i in drain_items(client)}
    assert by_ts[13000] == 2 and by_ts[13001] == 1 and by_ts[13002] == 2


# ── reactions ────────────────────────────────────────────────────────────────

def _react(bridge, target_ts, emoji="👍", remove=False, target_author=None):
    frame = data_message(ts=target_ts + 500, reaction={
        "emoji": emoji, "targetAuthor": target_author or "+15550009999",
        "targetSentTimestamp": target_ts, "isRemove": remove})
    bridge.handle_envelope(env(frame))


def test_peer_reacts_to_my_message(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="react to me", ts=14000)))
    _react(bridge, 14000, "🔥")
    it = drain_items(client)[0]
    assert it["reactions"] == {f"peer:{PEER_KEY}": "🔥"}


def test_reaction_remove(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="t", ts=15000)))
    _react(bridge, 15000, "🔥")
    _react(bridge, 15000, "🔥", remove=True)
    it = drain_items(client)[0]
    assert "reactions" not in it  # empty map serialized away


def test_reaction_replace(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="t", ts=16000)))
    _react(bridge, 16000, "🔥")
    _react(bridge, 16000, "😂")
    assert drain_items(client)[0]["reactions"] == {f"peer:{PEER_KEY}": "😂"}


def test_own_reaction_via_sync(bridge, client):
    bridge.handle_envelope(env(data_message(text="peer msg", ts=17000)))
    bridge.handle_envelope(env(sync_sent(
        ts=17500, reaction={"emoji": "❤️", "targetAuthor": PEER_NUMBER,
                            "targetSentTimestamp": 17000, "isRemove": False})))
    it = drain_items(client)[0]
    assert it["reactions"] == {"me": "❤️"}


def test_reaction_while_app_closed_reaches_change_feed(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="closed-app msg", ts=18000)))
    cursor = changes(client)["items"][-1]["modSeq"]
    _react(bridge, 18000, "🎉")
    fresh = changes(client, since=cursor)
    assert len(fresh["items"]) == 1
    assert fresh["items"][0]["reactions"] == {f"peer:{PEER_KEY}": "🎉"}


# ── edits ────────────────────────────────────────────────────────────────────

def _edit(bridge, target_ts, new_text, edit_ts):
    frame = data_message(ts=edit_ts)
    frame["envelope"]["dataMessage"] = {
        "timestamp": edit_ts,
        "editMessage": {"targetSentTimestamp": target_ts,
                        "dataMessage": {"timestamp": edit_ts, "message": new_text}},
    }
    bridge.handle_envelope(env(frame))


def test_edit_updates_body_keeps_identity(bridge, client):
    bridge.handle_envelope(env(data_message(text="speling", ts=19000)))
    _edit(bridge, 19000, "spelling", 19100)
    it = drain_items(client)[0]
    assert it["body"] == "spelling" and it["serverTs"] == 19000
    assert it["editedTs"] == 19100 and it["editCount"] == 1


def test_chained_edit_matches_edited_ts(bridge, client):
    bridge.handle_envelope(env(data_message(text="v1", ts=20000)))
    _edit(bridge, 20000, "v2", 20100)
    _edit(bridge, 20100, "v3", 20200)  # targets the PREVIOUS EDIT's ts
    it = drain_items(client)[0]
    assert it["body"] == "v3" and it["editCount"] == 2 and it["serverTs"] == 20000


def test_reaction_targeting_edited_revision(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="orig", ts=21000)))
    bridge.handle_envelope(env(sync_sent(
        ts=21100,
        dest_number=PEER_NUMBER)))  # noop sync frame guard
    # peer edits nothing here; we edit via sync, then peer reacts to the revision ts
    sync_frame = sync_sent(ts=21100)
    sync_frame["envelope"]["syncMessage"]["sentMessage"]["editMessage"] = {
        "targetSentTimestamp": 21000,
        "dataMessage": {"timestamp": 21100, "message": "orig v2"}}
    bridge.handle_envelope(env(sync_frame))
    _react(bridge, 21100, "👀")  # targets the revision, not the original
    it = [i for i in drain_items(client) if i["serverTs"] == 21000][0]
    assert it["body"] == "orig v2"
    assert it["reactions"] == {f"peer:{PEER_KEY}": "👀"}


# ── remote delete ────────────────────────────────────────────────────────────

def test_remote_delete_tombstones(bridge, client):
    bridge.handle_envelope(env(data_message(text="regret", ts=22000)))
    frame = data_message(ts=22100, remote_delete=22000)
    bridge.handle_envelope(env(frame))
    it = drain_items(client)[0]
    assert it.get("deleted") is True


# ── markers (phone reads) ────────────────────────────────────────────────────

def test_read_messages_sets_marker(bridge, client):
    bridge.handle_envelope(env(data_message(text="m", ts=23000)))
    frame = sync_sent(ts=23001)
    frame["envelope"]["syncMessage"] = {
        "readMessages": [{"sender": PEER_NUMBER, "senderUuid": PEER_UUID,
                          "timestamp": 23000}]}
    bridge.handle_envelope(env(frame))
    res = changes(client)
    assert res["markers"] and res["markers"][0]["peer"] == PEER_KEY
    assert res["markers"][0]["lastReadTs"] == 23000


# ── change feed mechanics ────────────────────────────────────────────────────

def test_mod_seq_strictly_increasing_and_cursorable(bridge, client):
    for i in range(5):
        bridge.handle_envelope(env(data_message(text=f"m{i}", ts=24000 + i)))
    res = changes(client)
    seqs = [i["modSeq"] for i in res["items"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # paged drain with last-returned-row cursor reaches the same set
    seen, cursor = [], 0
    while True:
        page = client.get(f"/v2/changes?since_seq={cursor}&limit=2").get_json()
        if not page["items"]:
            break
        seen.extend(i["modSeq"] for i in page["items"])
        cursor = page["items"][-1]["modSeq"]
    assert seen == seqs


def test_update_moves_row_past_old_cursor(bridge, client):
    bridge.handle_envelope(env(sync_sent(text="will be read", ts=25000)))
    cursor = changes(client)["items"][-1]["modSeq"]
    bridge.handle_envelope(env(receipt([25000], kind="read")))
    fresh = changes(client, since=cursor)["items"]
    assert len(fresh) == 1 and fresh[0]["status"] == 3


# ── peer re-keying ───────────────────────────────────────────────────────────

def test_uuid_only_envelope_rekeys_when_mapping_learned(bridge, client):
    # envelope with ONLY uuid → keyed by uuid
    frame = data_message(text="who dis", ts=26000, src_number="", src_uuid=PEER_UUID)
    frame["envelope"]["sourceNumber"] = ""
    frame["envelope"]["source"] = ""
    bridge.handle_envelope(env(frame))
    assert drain_items(client)[0]["peer"] == PEER_UUID.lower()
    # later envelope carries both → mapping learned → rows re-keyed
    bridge.handle_envelope(env(data_message(text="hi again", ts=26001)))
    items = drain_items(client)
    assert all(i["peer"] == PEER_KEY for i in items)
    assert len(items) == 2


def test_rekey_collision_merges(bridge, client):
    # same logical message under both keys, then mapping learned
    frame = data_message(text="dup", ts=27000, src_number="", src_uuid=PEER_UUID)
    frame["envelope"]["sourceNumber"] = ""
    frame["envelope"]["source"] = ""
    bridge.handle_envelope(env(frame))
    bridge.handle_envelope(env(data_message(text="dup", ts=27000)))  # number-keyed twin
    bridge.learn_mapping(PEER_UUID, PEER_NUMBER)
    items = [i for i in drain_items(client) if i["serverTs"] == 27000]
    assert len(items) == 1 and items[0]["peer"] == PEER_KEY


# ── /v2/sent ─────────────────────────────────────────────────────────────────

def test_v2_sent_idempotent_and_dual_writes_v1(bridge, client):
    body = {"peer": PEER_NUMBER, "kind": "text", "body": "app send",
            "server_ts": 28000}
    assert client.post("/v2/sent", json=body).status_code == 200
    assert client.post("/v2/sent", json=body).status_code == 200
    items = [i for i in drain_items(client) if i["serverTs"] == 28000]
    assert len(items) == 1 and items[0]["dir"] == "out"
    # v1 view sees it too (legacy app catch-up)
    v1 = client.get(f"/messages?peer={PEER_KEY}&after=0").get_json()["items"]
    assert [m for m in v1 if m["serverTs"] == 28000 and m["text"] == "app send"]


def test_v2_sent_image(bridge, client):
    r = client.post("/v2/sent", json={
        "peer": PEER_NUMBER, "kind": "image", "body": "cap",
        "server_ts": 29000, "att_id": "app-att-1", "mime": "image/jpeg"})
    assert r.status_code == 200
    it = [i for i in drain_items(client) if i["serverTs"] == 29000][0]
    assert it["kind"] == "image" and it["attId"] == "app-att-1" and it["body"] == "cap"


# ── /v2/read-receipts ────────────────────────────────────────────────────────

def test_read_receipts_queued_and_marker_bumped(bridge, client):
    r = client.post("/v2/read-receipts", json={
        "peer": PEER_NUMBER, "recipient": PEER_NUMBER,
        "timestamps": [30000, 30001]})
    assert r.get_json()["queued"] == 2
    res = changes(client)
    assert res["markers"][0]["lastReadTs"] == 30001
    with bridge._conn() as db:
        n = db.execute("SELECT COUNT(*) FROM receipt_queue").fetchone()[0]
    assert n == 2


def test_read_receipts_self_thread_skipped(bridge, client):
    r = client.post("/v2/read-receipts", json={
        "peer": "+15550009999", "recipient": "+15550009999",
        "timestamps": [31000]})
    assert r.get_json().get("skipped") == "self-thread"
    with bridge._conn() as db:
        assert db.execute("SELECT COUNT(*) FROM receipt_queue").fetchone()[0] == 0


# ── v1 contract freeze (golden) ──────────────────────────────────────────────

def test_v1_messages_contract_golden(bridge, client):
    bridge.handle_envelope(env(data_message(text="plain", ts=32000)))
    bridge.handle_envelope(env(data_message(
        ts=32001, attachments=[attachment("g-att", "image/jpeg")])))
    body = client.get(f"/messages?peer={PEER_KEY}&after=31999").get_json()
    assert body == {"items": [
        {"dir": "in", "text": "plain", "serverTs": 32000, "status": 2},
        {"dir": "in", "text": "", "serverTs": 32001, "status": 2,
         "attId": "g-att", "mime": "image/jpeg"},
    ]}


def test_v1_receipt_still_latest_row_guess(bridge, client):
    """v1 table keeps the OLD (buggy) semantics — frozen for the legacy app."""
    bridge.handle_envelope(env(sync_sent(text="m1", ts=33000)))
    bridge.handle_envelope(env(sync_sent(text="m2", ts=33001)))
    bridge.handle_envelope(env(receipt([33000], kind="delivery")))
    v1 = client.get(f"/messages?peer={PEER_KEY}&after=0").get_json()["items"]
    by_ts = {m["serverTs"]: m["status"] for m in v1}
    assert by_ts[33001] == 2  # latest-row guess (old behavior preserved)
    assert by_ts[33000] == 1


def test_health_shape(bridge, client):
    h = client.get("/health").get_json()
    assert h["ok"] is True and h["schema"] == 2
    assert "ws_connected" in h and "account_present" in h
