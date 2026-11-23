"""The sending engine and its pages: queueing, protection, retries, uncertain, halting, stop/resume, restart, report."""
import csv
import io
import json
import logging
import re

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.config import get_settings
from app.models import Batch, Employee, Payslip
from app.services import batch_storage, sender
from app.services.sender import RateLimiter, SendConfig, SendError
from tests.batch_helpers import add_more, batch_id_of, create, page, post
from tests.conftest import csrf_from, make_employee
from tests.pdfs import named_pdf
from tests.send_helpers import (
    CFG, PEOPLE, PHONE_ID, TOKEN, ScriptedClient, audit_actions, batch, err, error_response, fire_events, live_client, live_mode,
    ok_handler, payslips, people, ready_batch, send_now, statuses, use_client,
)

AMBIG, TRANS, PERM, AUTH_ = "ambiguous", "transient", "permanent", "auth"


# ---- starting ----------------------------------------------------------------------------------------------------

def test_send_needs_a_login_and_csrf(app, logged_in):
    from fastapi.testclient import TestClient

    anonymous = TestClient(app)                                         # a fresh client with no session
    assert anonymous.post("/batches/1/send", data={"confirm_label": "x"}, follow_redirects=False).status_code in (303, 403)
    for path in ("send", "stop", "resume", "retry-failed", "retry-uncertain", "payslips/1/retry"):
        assert logged_in.post(f"/batches/1/{path}").status_code == 403           # logged in, but no CSRF token
    for path in ("/batches/1/send", "/batches/1/status.json", "/batches/1/report.csv"):
        r = anonymous.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_only_a_ready_batch_can_be_sent(logged_in, db):
    people(db)
    bid = batch_id_of(create(logged_in, [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))]))        # still a draft
    r = send_now(logged_in, bid)
    assert r.headers["location"] == f"/batches/{bid}/send" and statuses(db, bid) == ["pending"]
    assert "Only a batch that is marked ready can be sent" in logged_in.get(f"/batches/{bid}/send").text
    assert logged_in.get(f"/batches/{bid}/send", follow_redirects=False).headers["location"] == f"/batches/{bid}"


def test_the_pay_month_must_be_typed_exactly_to_confirm(logged_in, db):
    bid = ready_batch(logged_in, db)
    for wrong in ("", "november 2026", "October 2026", "November"):
        send_now(logged_in, bid, wrong)
        assert statuses(db, bid) == ["pending"] * 3 and batch(db, bid).status == "ready"
    assert "does not match this batch" in logged_in.get(f"/batches/{bid}/send").text
    assert send_now(logged_in, bid, "  November 2026 ").headers["location"] == f"/batches/{bid}"
    assert batch(db, bid).status == "completed"


def test_confirmation_page_shows_counts_and_a_very_visible_mode(logged_in, db):
    bid = ready_batch(logged_in, db)
    make_employee(db, "No", "File", "+26770000999", "1111")
    html = logged_in.get(f"/batches/{bid}/send").text
    assert re.search(r'class="mode-banner mode-mock big"[^>]*>MOCK MODE: nothing is really sent', html)
    assert ">3</div><div class=\"summary-label\">Payslips to send" in html and ">1</div><div class=\"summary-label\">Active employees with no payslip" in html
    assert "Type <strong>November 2026</strong> to confirm" in html and "Send 3 payslips (mock)" in html and "000" in html and "111" in html and "222" in html


def test_live_mode_is_unmissable_and_warns_about_the_allowlist(logged_in, db, live_mode, monkeypatch):
    bid = ready_batch(logged_in, db)
    html = logged_in.get(f"/batches/{bid}/send").text
    assert 'mode-banner mode-live big' in html and "LIVE MODE: real WhatsApp messages" in html and "Send 3 payslips (LIVE)" in html
    assert "No allowed-recipients list is set" in html and "MOCK MODE" not in html.split("<main")[1]
    monkeypatch.setattr(get_settings(), "whatsapp_allowed_recipients", "+26771234567")
    html = logged_in.get(f"/batches/{bid}/send").text
    assert "only 1 allowed number" in html and "2 payslips go to numbers that are not on the list and will fail" in html


def test_the_mock_banner_is_on_every_page_in_mock_mode_and_gone_in_live(logged_in, db, live_mode):
    pages = ("/", "/employees", "/batches", "/batches/new")
    assert not any("MOCK MODE" in logged_in.get(p).text for p in pages)
    get_settings().whatsapp_mode = "mock"
    assert all("MOCK MODE." in logged_in.get(p).text and 'role="status"' in logged_in.get(p).text for p in pages)


def test_the_send_flow_queues_everything_and_audits_counts(logged_in, db):
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    b = batch(db, bid)
    assert b.send_mode == "mock" and b.send_started_at is not None and b.status == "completed"
    assert ("send_started", "files=3 mode=mock") in audit_actions(db)
    assert "Sending started: 3 payslips queued." in logged_in.get(f"/batches/{bid}").text


def test_a_batch_cannot_be_started_twice(logged_in, db):
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    before = [(p.status, p.attempts) for p in payslips(db, bid)]
    send_now(logged_in, bid)
    assert [(p.status, p.attempts) for p in payslips(db, bid)] == before and len([a for a in audit_actions(db) if a[0] == "send_started"]) == 1


def test_a_file_that_became_a_problem_after_ready_blocks_the_start(logged_in, db):
    bid = ready_batch(logged_in, db)
    db.query(Employee).filter(Employee.first_name == "Neo").one().phone = None
    db.commit()
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["pending"] * 3 and "1 file have problems now" in logged_in.get(f"/batches/{bid}/send").text


# ---- the happy path in mock mode ---------------------------------------------------------------------------------

def test_mock_send_goes_sent_then_delivered_then_read_through_the_status_path(logged_in, db, app):
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["sent"] * 3 and all(p.sent_at and p.whatsapp_message_id.startswith("wamid.MOCK") and p.whatsapp_media_id.startswith("mock-media-") for p in payslips(db, bid))
    assert len(app.state.mock_events) == 9                              # 3 events (sent, delivered, read) per payslip
    fire_events(app)
    rows = payslips(db, bid)
    assert [p.status for p in rows] == ["read"] * 3 and all(p.delivered_at and p.read_at for p in rows)
    assert batch(db, bid).status == "completed" and batch(db, bid).completed_at is not None


def test_every_payslip_sent_is_protected_with_that_employees_password(logged_in, db, app, db_factory):
    client = ScriptedClient()
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    passwords = {"Thabo": "6789", "Neo": "4321", "Chipo": "AB12"}
    assert len(client.uploads) == 3
    for (pdf, filename), send in zip(client.uploads, client.sends):
        first = send["name"].split()[0]
        assert filename == "Payslip - November 2026.pdf" and send["month"] == "November 2026"
        assert send["template"] == "payslip_" and send["language"] == "en"
        assert PdfReader(io.BytesIO(pdf)).is_encrypted
        for wrong in ("", "0000", passwords[first].lower() if passwords[first].isalpha() or any(c.isalpha() for c in passwords[first]) else "x"):
            if wrong != passwords[first]:
                assert int(PdfReader(io.BytesIO(pdf)).decrypt(wrong)) == 0
        opened = PdfReader(io.BytesIO(pdf))
        assert int(opened.decrypt(passwords[first])) != 0 and first in opened.pages[0].extract_text()


def test_no_protected_copy_is_ever_left_on_disk(logged_in, db, app, tmp_path):
    folder_before = sorted(p.name for p in tmp_path.rglob("*"))
    bid = ready_batch(logged_in, db)
    created = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    send_now(logged_in, bid)
    after = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    assert after == created and not any(n.endswith((".part", ".enc", ".tmp")) for n in after)         # only the original uploads
    stored = [p for p in payslips(db, bid)]
    for p in stored:                                                    # the stored originals are still unprotected PDFs
        assert not PdfReader(str(batch_storage.stored_path(bid, p.stored_filename))).is_encrypted


def test_the_template_message_uses_the_employee_name_and_pay_month(logged_in, db, app, db_factory):
    client = ScriptedClient()
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    assert [(s["to"], s["name"], s["month"]) for s in client.sends] == [
        ("+26771234567", "Thabo Molefe", "November 2026"), ("+26772345678", "Neo Dube", "November 2026"), ("+26773456789", "Chipo Fakename", "November 2026")]
    assert client.checked == [s["to"] for s in client.sends]            # the allowlist check ran before every send


# ---- a missing ID password ---------------------------------------------------------------------------------------

def test_a_row_without_an_id_password_is_refused_with_a_clear_reason_and_nothing_is_uploaded(logged_in, db, app, db_factory):
    client = ScriptedClient()
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    db.query(Employee).filter(Employee.first_name == "Neo").one().id_last4 = None      # removed AFTER the batch was ready
    db.commit()
    # the start itself is blocked by the readiness check, so force the row past it to test the worker's own guard
    batch(db, bid)
    from app.models.enums import SendState
    for p in payslips(db, bid):
        p.status = "queued"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    sender.process_queue(bid, db_factory, client, CFG)
    rows = {p.employee_id: p for p in payslips(db, bid)}
    neo = db.query(Employee).filter(Employee.first_name == "Neo").one()
    assert rows[neo.id].status == "failed" and rows[neo.id].error_code == "LOCAL_NO_PASSWORD"
    assert len(client.uploads) == 2 and all(s["name"] != "Neo Dube" for s in client.sends)
    assert statuses(db, bid).count("sent") == 2
    logged_in.get(f"/batches/{bid}")
    assert "no ID number, so the PDF password cannot be set" in logged_in.get(f"/batches/{bid}").text


def test_an_inactive_or_phoneless_employee_is_refused_at_send_time(logged_in, db, db_factory):
    client = ScriptedClient()
    bid = ready_batch(logged_in, db)
    from app.models.enums import SendState
    for p in payslips(db, bid):                                         # (payslips() expires the session: change things AFTER it)
        p.status = "queued"
    thabo, neo = db.query(Employee).filter(Employee.first_name.in_(["Thabo", "Neo"])).order_by(Employee.id).all()
    thabo.active, neo.phone = False, None
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    sender.process_queue(bid, db_factory, client, CFG)
    by_emp = {p.employee_id: p for p in payslips(db, bid)}
    assert by_emp[thabo.id].error_code == "LOCAL_NOT_READY" and by_emp[neo.id].error_code == "LOCAL_NO_PHONE" and len(client.sends) == 1


# ---- allowed recipients ------------------------------------------------------------------------------------------

def test_allowlist_refuses_others_tells_hr_why_and_sends_nothing_to_them(logged_in, db, app, db_factory):
    client = ScriptedClient(allowed={"+26771234567"})
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["sent", "failed", "failed"]
    assert [s["to"] for s in client.sends] == ["+26771234567"] and len(client.uploads) == 1       # nobody else was even uploaded
    html = page(logged_in, bid)
    assert html.count("not on the allowed recipients list (test mode)") == 2 and "WHATSAPP_ALLOWED_RECIPIENTS" in html


def test_live_client_allowlist_end_to_end_with_mocked_http(logged_in, db, app, db_factory, live_mode):
    live, seen = live_client(ok_handler, allowed={"+26772345678"})
    use_client(app, db_factory, live)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["failed", "sent", "failed"]
    recipients = [json.loads(r.content)["to"] for r in seen if r.url.path.endswith("/messages")]
    assert recipients == ["+26772345678"] and len(seen) == 2             # one upload + one message, nothing for the others


# ---- no double sending -------------------------------------------------------------------------------------------

def test_running_the_worker_again_never_sends_a_payslip_twice(logged_in, db, app, db_factory):
    client = ScriptedClient()
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    for _ in range(3):
        sender.process_queue(bid, db_factory, client, CFG)
    assert len(client.sends) == 3 and statuses(db, bid) == ["sent"] * 3


def test_a_row_can_only_be_claimed_once(logged_in, db, db_factory):
    bid = ready_batch(logged_in, db)
    from app.models.enums import SendState
    for p in payslips(db, bid):
        p.status = "queued"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    first = sender.claim_next(db_factory, bid, 10)
    second = sender.claim_next(db_factory, bid, 10)
    assert len(first) == 3 and second == [] and all(p.attempts == 1 for p in payslips(db, bid))


def test_sent_delivered_and_read_rows_can_never_be_queued_again(logged_in, db, app, db_factory):
    use_client(app, db_factory, ScriptedClient())
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    fire_events(app)
    ids = [p.id for p in payslips(db, bid)]
    with pytest.raises(SendError):
        sender.retry_failed(db, batch(db, bid), user_id=1, ip=None)
    with pytest.raises(SendError):
        sender.retry_uncertain(db, batch(db, bid), ids, True, user_id=1, ip=None)
    with pytest.raises(SendError):
        sender.resume_send(db, batch(db, bid), user_id=1, ip=None)
    assert statuses(db, bid) == ["sent"] * 3


def test_the_retry_buttons_do_nothing_for_rows_that_are_fine(logged_in, db):
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    token = csrf_from(logged_in.get("/").text)
    first = logged_in.post(f"/batches/{bid}/retry-failed", data={"csrf_token": token}, follow_redirects=True)
    second = logged_in.post(f"/batches/{bid}/retry-uncertain", data={"csrf_token": token, "understood": "on"}, follow_redirects=True)
    assert "There is nothing to retry." in first.text and "Select at least one uncertain payslip." in second.text
    assert [p.attempts for p in payslips(db, bid)] == [1, 1, 1]


# ---- uncertain: never retried automatically ----------------------------------------------------------------------

def test_a_timeout_makes_the_row_uncertain_and_it_is_never_retried_automatically(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(AMBIG, "LOCAL_TIMEOUT", message="timed out")])
    sleeps = use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["uncertain", "sent", "sent"] and len(client.sends) == 3 and sleeps == []      # no retry, no waiting
    for _ in range(2):
        sender.process_queue(bid, db_factory, client, CFG)
    assert len(client.sends) == 3 and payslips(db, bid)[0].attempts == 1
    html = page(logged_in, bid)
    assert "may or may not have been sent" in html and "Retry selected uncertain rows" in html and "may be delivered twice" in html
    assert batch(db, bid).status == "completed"


def test_retrying_an_uncertain_row_needs_the_warning_box_and_a_selection(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(AMBIG, "LOCAL_TIMEOUT")])
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    uncertain = payslips(db, bid)[0]
    post(logged_in, f"/batches/{bid}/retry-uncertain", payslip_ids=str(uncertain.id))               # box not ticked
    assert statuses(db, bid)[0] == "uncertain" and len(client.sends) == 3
    post(logged_in, f"/batches/{bid}/retry-uncertain", understood="on")                              # nothing selected
    assert statuses(db, bid)[0] == "uncertain" and len(client.sends) == 3
    post(logged_in, f"/batches/{bid}/retry-uncertain", payslip_ids=str(payslips(db, bid)[1].id), understood="on")   # not uncertain
    assert len(client.sends) == 3
    post(logged_in, f"/batches/{bid}/retry-uncertain", payslip_ids=str(uncertain.id), understood="on")
    assert statuses(db, bid) == ["sent"] * 3 and len(client.sends) == 4 and payslips(db, bid)[0].attempts == 2
    assert ("retry_clicked", "kind=uncertain count=1") in audit_actions(db)


def test_a_webhook_report_resolves_an_uncertain_row(logged_in, db, app, db_factory):
    from app.services.send_status import apply_status_event
    p = None
    client = ScriptedClient(send_errors=[err(AMBIG, "LOCAL_TIMEOUT")])
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    p = payslips(db, bid)[0]
    p.whatsapp_message_id = "wamid.LATE"                                 # (as if the id were known)
    db.commit()
    assert apply_status_event(db_factory, "wamid.LATE", "delivered") and payslips(db, bid)[0].status == "delivered"
    assert payslips(db, bid)[0].error is None and payslips(db, bid)[0].error_code is None


# ---- retries with backoff ----------------------------------------------------------------------------------------

def test_backoff_on_rate_limit_waits_1_4_16_seconds_and_then_succeeds(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(TRANS, "130429", 429), err(TRANS, "130429", 429), err(TRANS, "130429", 429)])
    sleeps = use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert sleeps == [1, 4, 16] and len(client.sends) == 4 and statuses(db, bid) == ["sent"]


def test_rate_limit_that_never_clears_fails_the_row_after_the_retries_and_explains(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(TRANS, "130429", 429)] * 10)
    sleeps = use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    p = payslips(db, bid)[0]
    assert sleeps == [1, 4, 16] and len(client.sends) == 4 and (p.status, p.error_code) == ("failed", "130429")
    assert "sending limit was reached" in page(logged_in, bid)


def test_retry_after_header_wins_over_the_default_backoff(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(TRANS, "130429", 429, retry_after=7)])
    sleeps = use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert sleeps == [7] and statuses(db, bid) == ["sent"]


def test_the_retry_count_is_the_setting(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(TRANS, "130429", 429)] * 10)
    sleeps = use_client(app, db_factory, client, SendConfig(concurrency=1, interval=0, max_retries=1))
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert sleeps == [1] and len(client.sends) == 2


def test_5xx_on_the_upload_is_retried_and_a_5xx_that_never_clears_on_the_message_is_uncertain(logged_in, db, app, db_factory):
    up = ScriptedClient(upload_errors=[err(TRANS, None, 503), None])
    sleeps = use_client(app, db_factory, up)
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert sleeps == [1] and len(up.uploads) == 2 and statuses(db, bid) == ["sent"]

    msg = ScriptedClient(send_errors=[err(TRANS, None, 502)] * 10)
    use_client(app, db_factory, msg)
    bid2 = ready_batch(logged_in, db, [("Zed", "Other", "+26775550000", "1111")], label="December 2026")
    send_now(logged_in, bid2, "December 2026")
    assert statuses(db, bid2) == ["uncertain"] and len(msg.sends) == 4        # the request may have been accepted: HR decides


def test_a_permanent_error_is_not_retried(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(PERM, "132001", 400, message="template missing")])
    sleeps = use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert sleeps == [] and len(client.sends) == 1 and payslips(db, bid)[0].error_code == "132001"
    assert "template was not found in this language, or it has not been approved" in page(logged_in, bid)


def test_live_http_429_then_success_end_to_end(logged_in, db, app, db_factory, live_mode):
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/messages"):
            calls["n"] += 1
            if calls["n"] <= 2:
                return error_response(429, 130429, "rate", headers={"Retry-After": "2"})
        return ok_handler(request)

    live, seen = live_client(handler)
    sleeps = use_client(app, db_factory, live)
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert sleeps == [2, 2] and statuses(db, bid) == ["sent"] and calls["n"] == 3
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == ["media", "messages", "messages", "messages"]    # one upload, three tries


def test_live_connection_error_before_sending_is_retried_but_a_read_timeout_is_uncertain(logged_in, db, app, db_factory, live_mode):
    attempts = {"n": 0}

    def flaky(request):
        if request.url.path.endswith("/messages"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("refused")
        return ok_handler(request)

    sleeps = use_client(app, db_factory, live_client(flaky)[0])
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["sent"] and sleeps == [1] and attempts["n"] == 2

    def slow(request):
        if request.url.path.endswith("/messages"):
            raise httpx.ReadTimeout("slow")
        return ok_handler(request)

    live, seen = live_client(slow)
    sleeps = use_client(app, db_factory, live)
    bid2 = ready_batch(logged_in, db, [("Zed", "Other", "+26775550000", "1111")], label="December 2026")
    send_now(logged_in, bid2, "December 2026")
    assert statuses(db, bid2) == ["uncertain"] and sleeps == [] and len([r for r in seen if r.url.path.endswith("/messages")]) == 1


# ---- expired token: stop everything ------------------------------------------------------------------------------

def test_an_invalid_access_token_halts_the_whole_batch_and_shows_a_banner(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(AUTH_, "190", 401, message="expired")])
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    b = batch(db, bid)
    assert statuses(db, bid) == ["failed", "pending", "pending"] and len(client.sends) == 1            # the others were never touched
    assert b.send_state == "halted" and "access token" in b.halted_reason and b.status == "sending"
    html = page(logged_in, bid)
    assert "Sending was stopped automatically" in html and "WhatsApp rejected the access token" in html and "Resume sending (2 waiting)" in html
    assert ("send_token_error", "code=190") in audit_actions(db)


def test_after_fixing_the_token_resume_and_retry_finish_the_batch(logged_in, db, app, db_factory):
    client = ScriptedClient(send_errors=[err(AUTH_, "190", 401)])
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    post(logged_in, f"/batches/{bid}/resume")
    assert statuses(db, bid) == ["failed", "sent", "sent"]
    post(logged_in, f"/batches/{bid}/retry-failed")
    assert statuses(db, bid) == ["sent"] * 3 and batch(db, bid).status == "completed" and batch(db, bid).halted_reason is None
    assert len(client.sends) == 4 and ("send_resumed", "files=2") in audit_actions(db)


def test_live_http_401_with_mocked_transport_halts(logged_in, db, app, db_factory, live_mode):
    def handler(request):
        if request.url.path.endswith("/media"):
            return error_response(401, 190, "Error validating access token: Session has expired")
        return ok_handler(request)

    live, seen = live_client(handler)
    use_client(app, db_factory, live)
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    assert batch(db, bid).send_state == "halted" and len(seen) == 1 and statuses(db, bid) == ["failed", "pending", "pending"]


# ---- stop and resume ---------------------------------------------------------------------------------------------

def test_stop_cancels_the_queued_rows_and_resume_sends_them(logged_in, db, app, db_factory):
    client = ScriptedClient()
    use_client(app, db_factory, client)
    bid = ready_batch(logged_in, db)

    def stop_after_first(n, to):
        if n == 1:
            with db_factory() as other:
                b = other.get(Batch, bid)
                sender.stop_send(other, b, user_id=1, ip=None)
                other.commit()

    client.on_send = stop_after_first
    send_now(logged_in, bid)
    assert statuses(db, bid) == ["sent", "pending", "pending"] and len(client.sends) == 1           # the first finished, the rest were cancelled
    b = batch(db, bid)
    assert b.send_state == "stopped" and b.status == "sending" and b.stopped_at is not None
    assert ("batch_stopped", "queued_cancelled=2") in audit_actions(db)
    html = page(logged_in, bid)
    assert "Sending is stopped" in html and "Resume sending (2 waiting)" in html
    client.on_send = None
    post(logged_in, f"/batches/{bid}/resume")
    assert statuses(db, bid) == ["sent"] * 3 and len(client.sends) == 3 and batch(db, bid).status == "completed"


def test_the_stop_button_works_through_the_page(logged_in, db, app, db_factory):
    from app.models.enums import SendState
    bid = ready_batch(logged_in, db)
    for p in payslips(db, bid):
        p.status = "queued"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    assert 'action="/batches/%d/stop"' % bid in page(logged_in, bid)
    post(logged_in, f"/batches/{bid}/stop")
    assert statuses(db, bid) == ["pending"] * 3 and batch(db, bid).send_state == "stopped"


# ---- restart recovery --------------------------------------------------------------------------------------------

def test_after_a_restart_sending_becomes_uncertain_and_queued_rows_resume(logged_in, db, db_factory):
    from app.models.enums import SendState
    bid = ready_batch(logged_in, db)
    rows = payslips(db, bid)
    rows[0].status, rows[1].status, rows[2].status = "sending", "queued", "queued"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    started = []

    class Launcher:
        def start(self, batch_id):
            started.append(batch_id)

    assert sender.recover_after_restart(db_factory, Launcher()) == {"uncertain": 1, "resumed": 1}
    rows = payslips(db, bid)
    assert [p.status for p in rows] == ["uncertain", "queued", "queued"] and rows[0].error_code == "LOCAL_RESTART" and started == [bid]
    assert "may or may not have been delivered" in sender.explain("LOCAL_RESTART")


def test_recovery_completes_a_batch_that_had_nothing_queued_and_ignores_idle_ones(logged_in, db, db_factory):
    from app.models.enums import SendState
    bid = ready_batch(logged_in, db)
    for p in payslips(db, bid):
        p.status = "sending"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    sender.recover_after_restart(db_factory, type("L", (), {"start": lambda self, i: None})())
    assert statuses(db, bid) == ["uncertain"] * 3 and batch(db, bid).status == "completed"
    again = sender.recover_after_restart(db_factory, type("L", (), {"start": lambda self, i: None})())
    assert again == {"uncertain": 0, "resumed": 0}


def test_a_restart_never_resends_what_was_in_flight(logged_in, db, db_factory):
    from app.models.enums import SendState
    client = ScriptedClient()
    bid = ready_batch(logged_in, db)
    for p in payslips(db, bid):
        p.status = "sending"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", SendState.running.value
    db.commit()
    sender.recover_after_restart(db_factory, type("L", (), {"start": lambda self, i: sender.process_queue(i, db_factory, client, CFG)})())
    assert client.sends == [] and client.uploads == []


# ---- mock scenarios ----------------------------------------------------------------------------------------------

SCENARIOS = [("Not", "OnWhatsapp", "+26770000000", "1111"), ("Rate", "Limited", "+26770000111", "2222"),
             ("Time", "Out", "+26770000222", "3333"), ("Fine", "Person", "+26770000333", "4444")]


def test_mock_numbers_000_111_222_simulate_each_path(logged_in, db, app):
    bid = ready_batch(logged_in, db, SCENARIOS)
    send_now(logged_in, bid)
    by_name = {db.get(Employee, p.employee_id).first_name: p for p in payslips(db, bid)}
    assert by_name["Rate"].status == "failed" and by_name["Rate"].error_code == "130429"             # after the automatic retries
    assert by_name["Time"].status == "uncertain" and by_name["Time"].error_code == "LOCAL_TIMEOUT"
    assert by_name["Not"].status == "sent" and by_name["Fine"].status == "sent"                    # accepted; the outcome comes by status
    fire_events(app)
    by_name = {db.get(Employee, p.employee_id).first_name: p for p in payslips(db, bid)}
    assert by_name["Not"].status == "failed" and by_name["Not"].error_code == "131026"
    assert by_name["Fine"].status == "read" and by_name["Rate"].status == "failed" and by_name["Time"].status == "uncertain"
    html = page(logged_in, bid)
    assert "not be on WhatsApp" in html and "sending limit was reached" in html and "may or may not have been sent" in html
    assert batch(db, bid).status == "completed"


def test_retry_failed_resends_only_failed_rows_and_the_rate_limit_number_fails_again_by_design(logged_in, db, app):
    bid = ready_batch(logged_in, db, SCENARIOS)
    send_now(logged_in, bid)
    fire_events(app)
    before = {p.id: p.attempts for p in payslips(db, bid)}
    post(logged_in, f"/batches/{bid}/retry-failed")
    fire_events(app)
    after = {p.id: p for p in payslips(db, bid)}
    retried = [pid for pid, p in after.items() if p.attempts == before[pid] + 1]
    names = {db.get(Employee, after[pid].employee_id).first_name for pid in retried}
    assert names == {"Not", "Rate"}                                      # only the two failed rows; the uncertain and read ones untouched
    assert ("retry_clicked", "kind=failed count=2") in audit_actions(db)


def test_retry_one_failed_row(logged_in, db, app):
    bid = ready_batch(logged_in, db, SCENARIOS)
    send_now(logged_in, bid)
    rate = next(p for p in payslips(db, bid) if p.error_code == "130429")
    post(logged_in, f"/batches/{bid}/payslips/{rate.id}/retry")
    assert payslips(db, bid)[[p.id for p in payslips(db, bid)].index(rate.id)].attempts == 2
    other = next(p for p in payslips(db, bid) if p.status == "sent")
    post(logged_in, f"/batches/{bid}/payslips/{other.id}/retry")        # not failed: refused
    assert next(p for p in payslips(db, bid) if p.id == other.id).attempts == 1


# ---- progress, JSON and report -----------------------------------------------------------------------------------

def test_progress_page_has_counters_rows_buttons_and_the_polling_hook(logged_in, db, app):
    bid = ready_batch(logged_in, db, SCENARIOS)
    send_now(logged_in, bid)
    html = page(logged_in, bid)
    assert 'data-status-url="/batches/%d/status.json"' % bid in html and "progress.js" in html and "Download send report" in html
    for key in ("pending", "queued", "sending", "sent", "delivered", "read", "failed", "uncertain"):
        assert f'data-count="{key}"' in html
    assert html.count("data-row-id=") == 4 and "Retry failed (1)" in html and "Retry selected uncertain rows" in html
    assert 'type="checkbox" name="payslip_ids"' in html and 'form="retry-uncertain-form"' in html
    assert "<script>" not in html and ' style="' not in html and not re.search(r"\son[a-z]+=", html)
    assert "***333" in html and "26770000333" not in html


def test_status_json_is_small_private_and_not_cached(logged_in, db, app):
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    r = logged_in.get(f"/batches/{bid}/status.json")
    data = r.json()
    assert r.status_code == 200 and r.headers["cache-control"].startswith("no-store") and r.headers["content-type"].startswith("application/json")
    assert set(data) == {"batch", "counts", "rows"} and data["counts"]["sent"] == 3 and data["batch"]["mode"] == "mock" and data["batch"]["active"] is False
    assert all(set(row) == {"id", "status", "label", "detail"} for row in data["rows"])
    text = r.text
    assert "Thabo" not in text and "26771234567" not in text and "6789" not in text           # no names, phones or IDs in the poll answer
    fire_events(app)
    assert logged_in.get(f"/batches/{bid}/status.json").json()["counts"]["read"] == 3
    assert logged_in.get("/batches/9999/status.json").status_code == 404


def test_progress_script_is_self_hosted_ascii_and_makes_one_request_to_the_page_supplied_url():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "app/static/progress.js").read_bytes()
    text = js.decode("ascii")
    for forbidden in ("XMLHttpRequest", "sendBeacon", "localStorage", "sessionStorage", "console.", "eval(", "innerHTML", "http://", "https://", "/batches"):
        assert forbidden not in text, forbidden
    assert text.count("fetch(") == 1 and 'getAttribute("data-status-url")' in text and "textContent" in text


def test_the_progress_script_is_only_loaded_while_a_batch_is_being_sent(logged_in, db):
    bid = ready_batch(logged_in, db)
    assert "progress.js" not in page(logged_in, bid) and "progress.js" not in logged_in.get("/batches").text
    send_now(logged_in, bid)
    assert "progress.js" in page(logged_in, bid)
    assert logged_in.get("/static/progress.js").status_code == 200


def test_report_csv_has_names_masked_phones_status_times_and_errors_but_no_ids_or_full_phones(logged_in, db, app):
    bid = ready_batch(logged_in, db, SCENARIOS)
    send_now(logged_in, bid)
    fire_events(app)
    r = logged_in.get(f"/batches/{bid}/report.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.headers["cache-control"].startswith("no-store")
    assert r.headers["content-disposition"] == f'attachment; filename="send-report-{bid}.csv"'
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["Employee", "Phone (last 3 digits)", "Status", "Sent at (UTC)", "Delivered at (UTC)", "Read at (UTC)", "Error code", "What happened"]
    by_name = {row[0]: row for row in rows[1:]}
    assert by_name["Fine Person"][1:3] == ["***333", "Read"] and by_name["Fine Person"][3] and by_name["Fine Person"][4] and by_name["Fine Person"][5]
    assert by_name["Rate Limited"][2] == "Failed" and by_name["Rate Limited"][6] == "130429" and "sending limit" in by_name["Rate Limited"][7]
    assert by_name["Time Out"][2] == "Uncertain" and by_name["Not OnWhatsapp"][6] == "131026"
    for secret in ("+26770000", "26770000333", "1111", "2222", "3333", "4444"):
        assert secret not in r.text
    assert ("report_downloaded", "rows=4") in audit_actions(db)


def test_report_is_not_available_before_sending_and_guards_against_spreadsheet_formulas(logged_in, db):
    bid = ready_batch(logged_in, db)
    r = logged_in.get(f"/batches/{bid}/report.csv", follow_redirects=False)
    assert r.status_code == 303 and "report_downloaded" not in [a for a, _ in audit_actions(db)]
    emp = db.query(Employee).filter(Employee.first_name == "Thabo").one()
    emp.first_name = "=HYPERLINK(1)"
    db.commit()
    send_now(logged_in, bid)
    text = logged_in.get(f"/batches/{bid}/report.csv").text
    assert "'=HYPERLINK(1)" in text and "\n=HYPERLINK" not in text


# ---- audit and privacy -------------------------------------------------------------------------------------------

def test_audit_entries_for_sending_hold_counts_only(logged_in, db, app):
    bid = ready_batch(logged_in, db, SCENARIOS)
    send_now(logged_in, bid)
    fire_events(app)
    post(logged_in, f"/batches/{bid}/retry-failed")
    logged_in.get(f"/batches/{bid}/report.csv")
    wanted = {"send_started", "retry_clicked", "report_downloaded"}
    rows = [(a, d) for a, d in audit_actions(db) if a in wanted]
    assert {a for a, _ in rows} == wanted
    for _, details in rows:
        assert re.fullmatch(r"[a-z_]+=[a-z0-9]+( [a-z_]+=[a-z0-9]+)*", details), details


def test_nothing_secret_or_personal_reaches_the_logs_in_a_live_flow(logged_in, db, app, db_factory, live_mode, caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        if request.url.path.endswith("/messages") and json.loads(request.content)["to"].endswith("345678"):
            return error_response(400, 131026, "bad number", details="Cannot reach 26772345678")
        return ok_handler(request)

    use_client(app, db_factory, live_client(handler)[0])
    bid = ready_batch(logged_in, db)
    send_now(logged_in, bid)
    page(logged_in, bid)
    logged_in.get(f"/batches/{bid}/status.json")
    logged_in.get(f"/batches/{bid}/report.csv")
    assert statuses(db, bid) == ["sent", "failed", "sent"]
    for needle in (TOKEN, "EAAB", "+26771234567", "26771234567", "71234567", "26772345678", "72345678", "Thabo", "Molefe", "Neo", "Dube",
                   "6789", "4321", "Payslip - November"):
        assert needle not in caplog.text, needle
    assert "26772345678" not in (payslips(db, bid)[1].error or "")


def test_the_access_token_is_never_stored_or_shown(logged_in, db, app, db_factory, live_mode):
    use_client(app, db_factory, live_client(lambda r: error_response(401, 190, f"bad token {TOKEN}"))[0])
    bid = ready_batch(logged_in, db, PEOPLE[:1])
    send_now(logged_in, bid)
    blobs = [page(logged_in, bid), logged_in.get(f"/batches/{bid}/status.json").text, logged_in.get(f"/batches/{bid}/report.csv").text,
             " ".join(str(v) for p in payslips(db, bid) for v in (p.error, p.error_code)), str(batch(db, bid).halted_reason), str(audit_actions(db))]
    assert not any(TOKEN in b for b in blobs)


# ---- concurrency and rate limiting -------------------------------------------------------------------------------

def test_rate_limiter_spaces_request_starts():
    clock = {"t": 0.0}
    waits = []

    def sleep(seconds):
        waits.append(round(seconds, 3))
        clock["t"] += seconds

    limiter = RateLimiter(0.5, clock=lambda: clock["t"], sleep=sleep)
    for _ in range(4):
        limiter.wait()
    assert waits == [0.5, 0.5, 0.5]                                     # 4 requests: the first is free, then 0.5 s apart
    assert RateLimiter(0, sleep=lambda s: 1 / 0).wait() is None         # interval 0 = no limiting


def test_the_worker_respects_the_concurrency_limit(tmp_path):
    """Several threads, each with its own database connection (a file database, not the shared in-memory test one)."""
    import threading
    import time

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.models import User
    from app.models.enums import SendState

    engine = create_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _wal(conn, _):
        conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as s:
        batch_row = Batch(pay_month_label="November 2026", status="sending", send_state=SendState.running.value)
        s.add(batch_row)
        s.flush()
        for i in range(8):
            e = Employee(first_name=f"P{i}", surname="Test", name_key=f"p{i} test", phone=f"+2677100{i:04d}", id_type="omang", id_last4="1234", active=True)
            s.add(e)
            s.flush()
            stored = batch_storage.new_stored_name()
            batch_storage.ensure_batch_dir(batch_row.id)
            (batch_storage.batch_dir(batch_row.id) / stored).write_bytes(named_pdf("P", "Test"))
            s.add(Payslip(batch_id=batch_row.id, employee_id=e.id, original_filename=f"p{i}.pdf", stored_filename=stored, status="queued",
                          match_status="exact", match_method="filename_exact", confirmed=True))
        s.commit()
        bid = batch_row.id

    class Slow(ScriptedClient):
        def __init__(self):
            super().__init__()
            self.lock, self.current, self.peak = threading.Lock(), 0, 0

        def send_template(self, *a, **k):
            with self.lock:
                self.current += 1
                self.peak = max(self.peak, self.current)
            time.sleep(0.08)
            try:
                return super().send_template(*a, **k)
            finally:
                with self.lock:
                    self.current -= 1

    client = Slow()
    sender.process_queue(bid, factory, client, SendConfig(concurrency=3, interval=0, max_retries=0))
    with factory() as s:
        done = [p.status for p in s.scalars(select(Payslip).where(Payslip.batch_id == bid))]
        assert s.get(Batch, bid).status == "completed"
    assert done == ["sent"] * 8 and 2 <= client.peak <= 3 and len(client.sends) == 8 and len({s["to"] for s in client.sends}) == 8
    engine.dispose()
