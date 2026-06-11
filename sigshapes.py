"""
Pure, environment-free Signal envelope/identifier helpers.

Extracted from bridge.py so the parsing rules that have repeatedly surprised us
(PNI service ids, group-id verbatim handling, E.164 length, attachment/quote
shapes) can be unit-tested in isolation — no DB, no env vars, no Flask. bridge.py
re-exports these names, so existing imports and the reload-based test fixture
keep working unchanged.
"""

import re

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_UUID_SEARCH = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

KIND_BY_MIME = (("image/", "image"), ("video/", "video"), ("audio/", "audio"))


def normalize_peer(s: str) -> str:
    """
    Phone numbers  → digits only  ("+1 (555) 123-4567" → "15551234567")
    UUIDs          → lowercase    ("ABC-..." → "abc-...")
    Service IDs    → bare uuid    ("PNI:<uuid>" → "<uuid>"; sent after a
                                   contact re-registers / changes phones)
    Group ids      → verbatim     ("group:<b64>" — case/symbols significant)
    Anything else  → lowercase stripped; digit-stripping is refused for
                     strings whose digits exceed E.164's 15-digit maximum
                     (that would forge a fake number out of a uuid)
    """
    s = (s or "").strip()
    if not s:
        return ""
    if s.startswith("group:"):
        return s
    if _UUID_RE.match(s):
        return s.lower()
    m = _UUID_SEARCH.search(s)
    if m:
        return m.group(0).lower()
    digits = re.sub(r"\D", "", s)
    if digits and len(digits) <= 15:
        return digits
    return s.lower()


def is_uuid_key(key: str) -> bool:
    return bool(_UUID_RE.match(key or ""))


def kind_from_mime(mime: str) -> str:
    for prefix, kind in KIND_BY_MIME:
        if (mime or "").startswith(prefix):
            return kind
    return "file"


def attachments_of(msg: dict):
    """List of (attachment_id, content_type) for a data/sent message."""
    out = []
    for att in (msg.get("attachments") or []):
        cid = att.get("id") or ""
        if cid:
            out.append((cid, att.get("contentType") or ""))
    return out


def quote_of(msg: dict):
    """(quote_ts, normalized_author, quote_text) or (0, '', '') if no quote."""
    q = msg.get("quote") or {}
    if not q:
        return 0, "", ""
    qt = q.get("text")
    qa = q.get("author") or q.get("authorNumber") or q.get("authorUuid") or ""
    return q.get("id") or 0, normalize_peer(qa), (qt if isinstance(qt, str) else "") or ""
