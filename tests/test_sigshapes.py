"""Unit tests for the pure envelope/identifier helpers."""
from sigshapes import (normalize_peer, is_uuid_key, kind_from_mime,
                       attachments_of, quote_of)

UUID = "0f0bbaee-6fd7-4b35-a803-2479e9676df9"


def test_number_to_digits():
    assert normalize_peer("+1 (555) 123-4567") == "15551234567"


def test_uuid_lowercased():
    assert normalize_peer(UUID.upper()) == UUID


def test_group_verbatim():
    g = "group:WdZrxff4vPcDxPgk9WlwQO97g8LRP+7X9nOeElXbQps="
    assert normalize_peer(g) == g


def test_pni_service_id():
    assert normalize_peer("PNI:" + UUID.upper()) == UUID
    assert normalize_peer("ACI:" + UUID) == UUID


def test_overlong_digits_kept_verbatim():
    assert normalize_peer("0067435803247996769") == "0067435803247996769"


def test_is_uuid_key():
    assert is_uuid_key(UUID)
    assert not is_uuid_key("15551234567")
    assert not is_uuid_key("group:x")


def test_kind_from_mime():
    assert kind_from_mime("image/jpeg") == "image"
    assert kind_from_mime("video/mp4") == "video"
    assert kind_from_mime("audio/mp4") == "audio"
    assert kind_from_mime("application/pdf") == "file"
    assert kind_from_mime(None) == "file"


def test_attachments_of():
    msg = {"attachments": [{"id": "a1", "contentType": "image/png"}, {"id": "", "contentType": "x"}]}
    assert attachments_of(msg) == [("a1", "image/png")]
    assert attachments_of({}) == []


def test_quote_of():
    msg = {"quote": {"id": 7500, "author": "+15551234567", "text": "hi"}}
    assert quote_of(msg) == (7500, "15551234567", "hi")
    assert quote_of({}) == (0, "", "")
