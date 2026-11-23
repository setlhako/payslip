"""POST/GET /webhook/whatsapp, status ordering, and the fake_webhook script."""
import hmac
import json
import logging
from datetime import datetime
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog, Batch, Payslip
from app.routers import webhook as webhook_module
from app.scripts import fake_webhook
from app.services.send_status import RANK, apply_status, apply_status_event, may_apply
from tests.batch_helpers import page
from tests.send_helpers import ready_batch, send_now, statuses

SECRET = "test-app-secret-not-real"          # set in conftest
VERIFY = "test-verify-token-not-real"
URL = "/webhook/whatsapp"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


def payload(*statuses_, **extra) -> bytes:
    """A status webhook shaped like Meta's: [(message id, status, timestamp, errors?)]."""
    items = []
    for item in statuses_:
        message_id, status, ts = item[0], item[1], item[2] if len(item) > 2 else "1750030073"
        entry = {"id": message_id, "status": status, "timestamp": str(ts), "recipient_id": "26771234567"}
        if len(item) > 3:
            entry["errors"] = item[3]
        items.append(entry)
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": "1"}, "statuses": items, **extra}
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": value}]}]}).encode()


def hook(client, body: bytes, signature: str | None = "auto", headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if signature == "auto":
        h["X-Hub-Signature-256"] = sign(body)
    elif signature is not None:
        h["X-Hub-Signature-256"] = signature
    return client.post(URL, content=body, headers=h)


@pytest.fixture
def anon(app):
    return TestClient(app)          # no login, no cookies: exactly what Meta is


@pytest.fixture
def sent_batch(logged_in, db):
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    return bid, [p.whatsapp_message_id for p in db.scalars(select(Payslip).where(Payslip.batch_id == bid).order_by(Payslip.id))]


def rows(db, bid):
    db.expire_all()
    return list(db.scalars(select(Payslip).where(Payslip.batch_id == bid).order_by(Payslip.id)))


# ---- verification handshake (GET) --------------------------------------------------------------------------------

def test_handshake_echoes_the_challenge_for_the_right_token(anon):
    r = anon.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"})
    assert r.status_code == 200 and r.text == "1158201444" and r.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.verify_token": VERIFY},
    {"hub.verify_token": VERIFY, "hub.challenge": "1"},
    {},
])
def test_handshake_refuses_anything_else(anon, params):
    r = anon.get(URL, params=params)
    assert r.status_code == 403 and "1" not in r.text.replace("Forbidden", "")


def test_handshake_refuses_when_no_verify_token_is_configured(anon, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_verify_token", get_settings().whatsapp_verify_token.__class__(""))
    assert anon.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}).status_code == 403


def test_the_verify_token_is_compared_in_constant_time(anon, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(webhook_module.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    anon.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "5"})
    assert calls


# ---- signature (POST) --------------------------------------------------------------------------------------------

def test_a_valid_signature_is_accepted_without_login_or_csrf(anon, db, sent_batch):
    bid, ids = sent_batch
    body = payload((ids[0], "delivered"))
    r = hook(anon, body)
    assert r.status_code == 200 and r.json() == {"status": "ok", "updated": 1}
    assert [p.status for p in rows(db, bid)] == ["delivered", "sent", "sent"]


@pytest.mark.parametrize("signature", [
    None, "", "sha256=", "sha256=deadbeef", "md5=abc", "sha1=" + "0" * 40, "sha256=" + "0" * 64,
])
def test_missing_or_wrong_signatures_are_rejected_with_403_and_change_nothing(anon, db, sent_batch, signature):
    bid, ids = sent_batch
    r = hook(anon, payload((ids[0], "read")), signature)
    assert r.status_code == 403 and r.json() == {"status": "rejected"}
    assert [p.status for p in rows(db, bid)] == ["sent"] * 3


def test_the_signature_is_over_the_raw_bytes_not_the_parsed_json(anon, db, sent_batch):
    bid, ids = sent_batch
    body = payload((ids[0], "delivered"))
    pretty = json.dumps(json.loads(body), indent=2).encode()                  # same JSON, different bytes
    assert hook(anon, pretty, sign(body)).status_code == 403
    assert hook(anon, pretty, sign(pretty)).status_code == 200
    escaped = json.dumps(json.loads(body), ensure_ascii=True).replace("whatsapp", "whats\\u0061pp").encode()
    assert hook(anon, escaped, sign(escaped)).status_code == 200              # escaped unicode in the raw bytes is fine when signed as received


def test_the_wrong_secret_is_rejected_and_the_secret_is_the_app_secret(anon, sent_batch):
    bid, ids = sent_batch
    body = payload((ids[0], "read"))
    assert hook(anon, body, sign(body, "some-other-secret")).status_code == 403
    assert hook(anon, body, sign(body, VERIFY)).status_code == 403            # the verify token is NOT the signing key
    assert hook(anon, body, sign(body, SECRET)).status_code == 200


def test_the_signature_is_compared_in_constant_time(anon, sent_batch, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(webhook_module.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    bid, ids = sent_batch
    hook(anon, payload((ids[0], "read")))
    assert calls


def test_without_an_app_secret_everything_is_rejected(anon, sent_batch, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", get_settings().whatsapp_app_secret.__class__(""))
    body = payload((sent_batch[1][0], "read"))
    assert hook(anon, body, sign(body, "")).status_code == 403


def test_a_rejected_webhook_is_audited_once_a_minute_without_personal_data(anon, db, sent_batch, monkeypatch):
    monkeypatch.setattr(webhook_module, "_last_reject_audit", 0.0)
    for _ in range(3):
        hook(anon, payload((sent_batch[1][0], "read")), "sha256=" + "0" * 64)
    db.expire_all()
    entries = [a for a in db.scalars(select(AuditLog)) if a.action == "webhook_rejected"]
    assert len(entries) == 1 and entries[0].details == "reason=bad_signature" and entries[0].user_id is None
    assert entries[0].ip_address == "unknown"            # the test client has no real address; real ones are stored masked (192.168.1.x)


def test_the_webhook_has_a_bigger_body_limit_than_normal_forms(anon):
    big = b"{" + b'"a":"' + b"x" * 150_000 + b'"}'
    assert hook(anon, big, sign(big)).status_code == 200                       # 150 KB: over the normal 100 KB form limit
    huge = b"x" * 2_100_000
    assert hook(anon, huge, sign(huge)).status_code == 413


# ---- processing statuses -----------------------------------------------------------------------------------------

def test_sent_delivered_read_in_one_batch_of_updates_and_in_order(anon, db, sent_batch):
    bid, ids = sent_batch
    body = payload((ids[0], "sent", 1750000001), (ids[0], "delivered", 1750000002), (ids[0], "read", 1750000003), (ids[1], "delivered"))
    assert hook(anon, body).json()["updated"] == 3                             # "sent" was already known: no change
    p = rows(db, bid)
    assert [x.status for x in p] == ["read", "delivered", "sent"]
    assert p[0].read_at == datetime(2025, 6, 15, 15, 6, 43) and p[0].delivered_at == datetime(2025, 6, 15, 15, 6, 42)


def test_status_never_moves_backwards(anon, db, sent_batch):
    bid, ids = sent_batch
    hook(anon, payload((ids[0], "read")))
    for late in ("delivered", "sent", "failed"):
        hook(anon, payload((ids[0], late, 1750000999, [{"code": 131026, "title": "x", "message": "x"}])))
    first = rows(db, bid)[0]
    assert first.status == "read" and first.error_code is None and first.error is None


def test_events_are_idempotent_and_timestamps_are_set_once(anon, db, sent_batch):
    bid, ids = sent_batch
    assert hook(anon, payload((ids[0], "delivered", 1750000100))).json()["updated"] == 1
    for _ in range(3):
        assert hook(anon, payload((ids[0], "delivered", 1750009999))).json()["updated"] == 0
    assert rows(db, bid)[0].delivered_at == datetime(2025, 6, 15, 15, 8, 20)       # the FIRST report's time


def test_read_without_delivered_also_counts_as_delivered(anon, db, sent_batch):
    bid, ids = sent_batch
    hook(anon, payload((ids[2], "read", 1750000500)))
    p = rows(db, bid)[2]
    assert p.status == "read" and p.delivered_at == p.read_at


def test_a_failed_status_stores_meta_code_and_message_and_the_page_explains_it(anon, logged_in, db, sent_batch):
    bid, ids = sent_batch
    error = [{"code": 131026, "title": "Message undeliverable", "message": "Message undeliverable",
              "error_data": {"details": "Recipient 26771234567 is not a WhatsApp number"}}]
    assert hook(anon, payload((ids[1], "failed", 1750000100, error))).json()["updated"] == 1
    p = rows(db, bid)[1]
    assert (p.status, p.error_code) == ("failed", "131026") and "26771234567" not in p.error and "•••" in p.error
    html = page(logged_in, bid)
    assert "not be on WhatsApp" in html and "Retry failed (1)" in html


def test_an_unknown_error_code_shows_the_raw_code_and_message(anon, logged_in, db, sent_batch):
    bid, ids = sent_batch
    hook(anon, payload((ids[0], "failed", 1750000100, [{"code": 999555, "title": "Odd", "message": "Something new"}])))
    assert "WhatsApp returned error 999555: Something new" in page(logged_in, bid)


def test_a_failure_resolves_the_batch_and_unknown_message_ids_are_ignored(anon, db, sent_batch):
    bid, ids = sent_batch
    assert hook(anon, payload(("wamid.NOBODY", "delivered"))).json() == {"status": "ok", "updated": 0}
    for mid in ids:
        hook(anon, payload((mid, "read")))
    db.expire_all()
    assert db.get(Batch, bid).status == "completed" and statuses(db, bid) == ["read"] * 3


def test_inbound_customer_messages_are_ignored_and_never_logged(anon, db, sent_batch, caplog):
    caplog.set_level(logging.DEBUG)
    bid, ids = sent_batch
    inbound = {"contacts": [{"profile": {"name": "Secret Name"}, "wa_id": "26771234567"}],
               "messages": [{"from": "26771234567", "id": "wamid.IN", "timestamp": "1749416383", "type": "text", "text": {"body": "my secret salary question"}}]}
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp", **inbound}}]}]}).encode()
    r = hook(anon, body)
    assert r.status_code == 200 and r.json() == {"status": "ok", "updated": 0}
    assert "my secret salary question" not in caplog.text and "Secret Name" not in caplog.text and "26771234567" not in caplog.text
    assert [p.status for p in rows(db, bid)] == ["sent"] * 3


def test_odd_but_signed_payloads_never_crash_the_endpoint(anon):
    for body in (b"not json at all", b"[]", b'{"entry": "x"}', b'{"entry": [1, 2]}', b'{"entry": [{"changes": [{"value": {"statuses": [null, 5, {"id": 7}]}}]}]}',
                 b'{"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.X", "status": "banana"}]}}]}]}'):
        assert hook(anon, body, sign(body)).status_code == 200


def test_webhook_logs_hold_no_message_ids_phones_or_secrets(anon, sent_batch, caplog):
    caplog.set_level(logging.DEBUG)
    bid, ids = sent_batch
    hook(anon, payload((ids[0], "delivered")))
    hook(anon, payload((ids[0], "read")), "sha256=" + "0" * 64)
    for needle in (ids[0], "26771234567", SECRET, VERIFY):
        assert needle not in caplog.text


def test_the_mock_client_and_the_webhook_use_the_same_status_code_path(app, db, sent_batch, monkeypatch):
    """Both call apply_status_event: the mock's delayed updates are not a separate implementation."""
    import app.routers.webhook as hook_mod
    import tests.conftest as conftest

    assert hook_mod.apply_status_event is apply_status_event and conftest.apply_status_event is apply_status_event
    bid, ids = sent_batch
    from tests.send_helpers import fire_events
    fire_events(app)
    assert statuses(db, bid) == ["read"] * 3


# ---- status ordering rules (pure) --------------------------------------------------------------------------------

@pytest.mark.parametrize("current,new,allowed", [
    ("sending", "sent", True), ("uncertain", "sent", True), ("uncertain", "delivered", True), ("failed", "delivered", True), ("sent", "delivered", True),
    ("sent", "read", True), ("delivered", "read", True), ("sending", "read", True),
    ("sent", "sent", False), ("delivered", "delivered", False), ("delivered", "sent", False), ("read", "delivered", False),
    ("read", "sent", False), ("read", "read", False),
    ("sending", "failed", True), ("uncertain", "failed", True), ("sent", "failed", True),
    ("delivered", "failed", False), ("read", "failed", False), ("failed", "failed", False), ("pending", "failed", False), ("queued", "failed", False),
    ("sent", "queued", False), ("sent", "pending", False), ("sent", "banana", False),
])
def test_may_apply_table(current, new, allowed):
    assert may_apply(current, new) is allowed


def test_apply_status_sets_fields_clears_errors_and_sets_times_once():
    p = Payslip(status="uncertain", error="old", error_code="LOCAL_TIMEOUT", batch_id=1, original_filename="x.pdf")
    when = datetime(2026, 1, 2, 3, 4, 5)
    assert apply_status(p, "delivered", when) and (p.status, p.error, p.error_code, p.delivered_at, p.sent_at) == ("delivered", None, None, when, None)
    assert not apply_status(p, "delivered", datetime(2030, 1, 1)) and p.delivered_at == when
    assert apply_status(p, "read", datetime(2026, 1, 2, 3, 5, 0)) and p.read_at == datetime(2026, 1, 2, 3, 5, 0) and p.delivered_at == when
    assert RANK["sent"] < RANK["delivered"] < RANK["read"]


# ---- fake_webhook script -----------------------------------------------------------------------------------------

def test_the_fake_payload_has_metas_shape_and_signature_matches_hmac_of_the_exact_body():
    body = fake_webhook.build_payload("wamid.ABC", "failed", 131026, when=1750030073)
    data = json.loads(body)
    status = data["entry"][0]["changes"][0]["value"]["statuses"][0]
    assert data["object"] == "whatsapp_business_account" and data["entry"][0]["changes"][0]["field"] == "messages"
    assert status["id"] == "wamid.ABC" and status["status"] == "failed" and status["timestamp"] == "1750030073"
    assert status["errors"][0]["code"] == 131026 and "message" in status["errors"][0]
    assert fake_webhook.sign("s3cret", body) == "sha256=" + hmac.new(b"s3cret", body, sha256).hexdigest()


def test_the_script_updates_a_payslip_through_the_real_endpoint_and_a_bad_signature_is_rejected(app, db, sent_batch, capsys):
    bid, ids = sent_batch
    http = TestClient(app)
    assert fake_webhook.main(["--url", URL, "--message-id", ids[0]], client=http) == 0
    out = capsys.readouterr().out
    assert "delivered: HTTP 200" in out and "read: HTTP 200" in out and SECRET not in out and ids[0] not in out
    assert rows(db, bid)[0].status == "read"
    assert fake_webhook.main(["--url", URL, "--message-id", ids[1], "--bad-signature", "--status", "read"], client=http) == 0
    assert "HTTP 403" in capsys.readouterr().out and rows(db, bid)[1].status == "sent"


def test_the_script_can_fail_a_payslip_and_check_the_handshake(app, db, sent_batch, capsys):
    bid, ids = sent_batch
    http = TestClient(app)
    fake_webhook.main(["--url", URL, "--message-id", ids[2], "--status", "failed", "--error-code", "131026"], client=http)
    assert rows(db, bid)[2].status == "failed" and rows(db, bid)[2].error_code == "131026"
    capsys.readouterr()
    fake_webhook.main(["--url", URL, "--verify"], client=http)
    out = capsys.readouterr().out
    assert "HTTP 200, challenge echoed: True" in out and "wrong verify token: HTTP 403" in out and VERIFY not in out
