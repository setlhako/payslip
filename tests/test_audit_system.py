"""Audit log page and CSV export, system status page, dashboard."""
import csv
import hashlib
import hmac
import io
import json
import re
from datetime import datetime

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog, User
from app.services import audit_view
from tests.send_helpers import ready_batch, send_now
from tests.user_helpers import audit, login_as, make_user, staff, token  # noqa: F401


def add(db, action, user=None, when=None, details=None, entity=None, ip="10.1.2.x"):
    row = AuditLog(action=action, user_id=user.id if user else None, details=details, ip_address=ip,
                   entity_type=entity[0] if entity else None, entity_id=entity[1] if entity else None,
                   created_at=when or datetime(2026, 11, 20, 12, 0, 0))
    db.add(row)
    db.commit()
    return row


def seed(db):
    hr, viewer = (db.scalar(select(User).where(User.username == n)) for n in ("the.hr", "the.viewer"))
    add(db, "batch_created", hr, datetime(2026, 10, 1, 9, 0), "files=3", ("batch", 1))
    add(db, "send_started", hr, datetime(2026, 10, 15, 9, 0), "files=3 mode=mock", ("batch", 1))
    add(db, "login_success", viewer, datetime(2026, 11, 1, 8, 0), "username 'the.viewer'")
    add(db, "employee_created", hr, datetime(2026, 11, 10, 8, 0), None, ("employee", 7))
    add(db, "webhook_rejected", None, datetime(2026, 11, 12, 8, 0), "reason=bad_signature")
    return hr, viewer


def page_rows(html):
    return re.findall(r"<strong>([^<]+)</strong><div class=\"file-meta\">([a-z_]+)</div>", html)


def actions_on(html):
    return [code for _, code in page_rows(html)]


def csv_rows(response):
    return list(csv.reader(io.StringIO(response.text)))


# ---- audit page --------------------------------------------------------------------------------------------------

def test_audit_page_lists_entries_newest_first_with_plain_labels_and_target(staff, db):
    seed(db)
    html = staff["admin"].get("/audit", params={"action": ""}).text
    codes = actions_on(html)
    assert codes.index("webhook_rejected") < codes.index("employee_created") < codes.index("send_started") < codes.index("batch_created")
    assert "Webhook rejected (bad signature)" in html and "Sending started" in html and "Employee added" in html
    assert "employee #7" in html and "batch #1" in html and "system" in html and "files=3 mode=mock" in html and "the.hr" in html


def test_filter_by_date_range_is_inclusive_of_both_days(staff, db):
    seed(db)
    html = staff["admin"].get("/audit", params={"date_from": "2026-10-15", "date_to": "2026-11-01"}).text
    got = set(actions_on(html))
    assert {"send_started", "login_success"} <= got and not ({"batch_created", "employee_created", "webhook_rejected"} & got)
    assert "match your filters" in html or "matches your filters" in html


def test_filter_by_user_system_and_action(staff, db):
    hr, viewer = seed(db)
    only_hr = set(actions_on(staff["admin"].get("/audit", params={"user": str(hr.id)}).text))
    assert {"batch_created", "send_started", "employee_created"} <= only_hr and "webhook_rejected" not in only_hr       # (plus the real login of the fixture)
    assert str(viewer.id) not in only_hr and "the.viewer" not in staff["admin"].get("/audit", params={"user": str(hr.id)}).text.split("<tbody>")[1]
    system_only = set(actions_on(staff["admin"].get("/audit", params={"user": "system"}).text))
    assert "webhook_rejected" in system_only and not ({"batch_created", "send_started", "employee_created"} & system_only)
    only_action = actions_on(staff["admin"].get("/audit", params={"action": "login_success"}).text)
    assert only_action and set(only_action) == {"login_success"}


def test_search_looks_in_action_details_target_and_username(staff, db):
    seed(db)
    def found(q):
        return set(actions_on(staff["admin"].get("/audit", params={"q": q}).text))
    assert found("bad_signature") == {"webhook_rejected"}
    assert found("mode=mock") == {"send_started"}
    assert found("the.viewer") >= {"login_success"} and "batch_created" not in found("the.viewer")
    assert found("employee") == {"employee_created"}
    assert found("100%") == set()                                        # the % is searched literally, not as a wildcard


def test_filters_combine_and_the_options_lists_are_offered(staff, db):
    hr, _ = seed(db)
    html = staff["admin"].get("/audit", params={"user": str(hr.id), "action": "send_started", "date_from": "2026-10-01", "date_to": "2026-10-31", "q": "mock"}).text
    assert set(actions_on(html)) == {"send_started"}
    assert '<option value="login_success"' in html and '<option value="%d"' % hr.id in html and 'type="date"' in html


def test_bad_filter_values_are_ignored_with_a_message_and_never_crash(staff, db):
    seed(db)
    html = staff["admin"].get("/audit", params={"date_from": "not-a-date", "date_to": "2026-13-45", "user": "abc", "page": "-4"}).text
    assert "was not understood" in html and len(actions_on(html)) >= 4
    swapped = staff["admin"].get("/audit", params={"date_from": "2026-11-30", "date_to": "2026-10-01"}).text
    assert "wrong way round" in swapped and "batch_created" in swapped


def test_paging_keeps_the_filters_and_clamps_the_page(staff, db):
    hr, _ = seed(db)
    for i in range(70):
        add(db, "employee_edited", hr, datetime(2026, 11, 21, 10, i % 60), None, ("employee", i))
    first = staff["admin"].get("/audit", params={"action": "employee_edited"}).text
    assert len(actions_on(first)) == audit_view.PER_PAGE and "Page 1 of 2" in first and "70 entries match your filters" in first
    assert 'href="/audit?action=employee_edited&amp;page=2"' in first
    second = staff["admin"].get("/audit", params={"action": "employee_edited", "page": "2"}).text
    assert len(actions_on(second)) == 20 and "Page 2 of 2" in second
    assert "Page 2 of 2" in staff["admin"].get("/audit", params={"action": "employee_edited", "page": "99"}).text


def test_empty_states(staff, db):
    assert "No entries match" in staff["admin"].get("/audit", params={"q": "zzzzzz-nothing"}).text
    assert "Clear filters" in staff["admin"].get("/audit", params={"q": "zzzzzz-nothing"}).text


# ---- nothing sensitive is shown or exported ----------------------------------------------------------------------

SENSITIVE = "ID 9876543210 token EAABsecrettoken1234567890ABCDEF phone +26771234567 omang 123456789 hash sha256=0123456789abcdef0123456789abcdef"


def test_the_page_and_the_csv_scrub_digits_and_tokens_even_if_an_entry_contains_them(staff, db):
    add(db, "employee_edited", None, None, SENSITIVE, ("employee", 3))
    page = staff["admin"].get("/audit").text
    export = staff["admin"].get("/audit/export.csv").text
    for text in (page, export):
        for secret in ("9876543210", "EAABsecrettoken", "26771234567", "71234567", "123456789", "0123456789abcdef"):
            assert secret not in text, secret
    assert "ID *** token [hidden] phone +*** omang *** hash [hidden]" in page or "ID *** token [hidden]" in page


def test_scrub_masks_long_digit_runs_and_token_like_strings_but_keeps_ordinary_numbers():
    assert audit_view.scrub("files=12 mode=mock batch=3") == "files=12 mode=mock batch=3"
    assert audit_view.scrub("0711234567 and 1234567") == "*** and ***" and audit_view.scrub("123456") == "123456"
    assert audit_view.scrub("Bearer EAABxxxxxxxxxxxxxxxxxxxx") == "Bearer [hidden]" and audit_view.scrub(None) == ""


def test_real_audit_rows_made_by_the_app_hold_no_id_numbers_phones_or_tokens(staff, db):
    from app.models import Employee
    from app.services.id_crypto import encrypt_id
    bid = ready_batch(staff["hr"], db)
    e = db.query(Employee).filter(Employee.first_name == "Thabo").one()
    e.id_number_encrypted = encrypt_id("987654321")                                   # a stored full ID, to be revealed and audited
    db.commit()
    assert staff["hr"].post("/employees/reveal-ids", data={"csrf_token": token(staff["hr"]), "employee_id": str(e.id)}).json()["ids"]
    send_now(staff["hr"], bid)
    text = staff["admin"].get("/audit").text + staff["admin"].get("/audit/export.csv").text
    for secret in ("987654321", "26771234567", "71234567", "6789", get_settings().whatsapp_access_token.get_secret_value()):
        assert secret not in text, secret


# ---- CSV export ----------------------------------------------------------------------------------------------------

def test_csv_export_has_the_filtered_view_only(staff, db):
    hr, _ = seed(db)
    response = staff["admin"].get("/audit/export.csv", params={"user": str(hr.id), "date_from": "2026-10-01", "date_to": "2026-10-31"})
    rows = csv_rows(response)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv") and response.headers["cache-control"].startswith("no-store")
    assert response.headers["content-disposition"] == 'attachment; filename="audit-log.csv"'
    assert rows[0] == ["When (UTC)", "User", "What happened", "Action code", "About", "Details", "Address (masked)"]
    assert [r[3] for r in rows[1:]] == ["send_started", "batch_created"] and rows[1][1] == "the.hr" and rows[1][0] == "2026-10-15 09:00:00"


def test_the_page_download_link_carries_the_filters(staff, db):
    seed(db)
    html = staff["admin"].get("/audit", params={"action": "send_started"}).text
    assert 'href="/audit/export.csv?action=send_started"' in html and "Download CSV of this view" in html
    assert 'href="/audit/export.csv"' in staff["admin"].get("/audit").text


def test_exporting_is_itself_audited_with_a_row_count_only(staff, db):
    seed(db)
    staff["admin"].get("/audit/export.csv", params={"action": "send_started"})
    entry = audit(db, "audit_exported")[-1]
    assert entry.details == "rows=1" and entry.user_id is not None


def test_csv_cells_cannot_run_as_spreadsheet_formulas(staff, db):
    add(db, "employee_edited", None, None, "=HYPERLINK(\"http://x\")", ("employee", 1))
    user = make_user(db, "=cmd|' /C calc'!A0", "viewer", full_name="x")
    add(db, "login_success", user, None, "+SUM(1,1)")
    text = staff["admin"].get("/audit/export.csv").text
    assert "'=HYPERLINK" in text and "'+SUM" in text and "'=cmd" in text
    assert not re.search(r"(^|,)[=+@-]", text.split("\n", 1)[1].replace("\r", ""), re.M)


def test_export_is_capped_and_only_for_admins(staff, db, monkeypatch):
    seed(db)
    monkeypatch.setattr(audit_view, "EXPORT_LIMIT", 2)
    assert len(csv_rows(staff["admin"].get("/audit/export.csv"))) == 3          # header + 2
    for role in ("hr", "viewer"):
        assert staff[role].get("/audit").status_code == 403 and staff[role].get("/audit/export.csv").status_code == 403


# ---- system status -------------------------------------------------------------------------------------------------

def test_system_status_shows_the_mode_template_allowlist_and_settings_without_secrets(staff, db, monkeypatch):
    s = get_settings()
    html = staff["admin"].get("/system").text
    assert 'class="big-mode mock"' in html and "MOCK" in html and "Nothing is really sent" in html
    assert "payslip_" in html and "language en" in html and "Not set" in html and "Never" in html and "Working" in html
    for secret in (s.whatsapp_access_token.get_secret_value(), s.whatsapp_app_secret.get_secret_value(), s.whatsapp_verify_token.get_secret_value(),
                   s.session_secret.get_secret_value(), s.id_encryption_key.get_secret_value(), s.whatsapp_phone_number_id):
        assert secret not in html
    assert html.count('<span class="yes">Yes</span>') >= 5 and "Live settings complete" in html
    monkeypatch.setattr(s, "whatsapp_allowed_recipients", "+26771234567,+26772345678")
    assert "Active</span>: only 2 numbers can receive messages" in staff["admin"].get("/system").text


def test_system_status_in_live_mode_is_very_visible_and_warns_without_an_allowlist(staff, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "live")
    html = staff["admin"].get("/system").text
    assert 'class="big-mode live"' in html and "LIVE" in html and "every employee in a batch would really receive a message" in html
    assert 'mode-pill mode-pill-live' in html and ">LIVE</span>" in html


def test_settings_present_are_yes_or_no_only(staff, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_access_token", s.whatsapp_access_token.__class__("your-access-token"))
    html = staff["admin"].get("/system").text
    row = re.search(r"Access token \(WHATSAPP_ACCESS_TOKEN\)</dt><dd><span class=\"(\w+)\">(\w+)</span>", html)
    assert row and row.groups() == ("no", "No") and "your-access-token" not in html
    assert "Live settings complete</dt><dd><span class=\"\">No</span>" in html or "No</span> <span class=\"muted\">(only needed before switching to live" in html


def test_last_webhook_event_time_appears_after_a_signed_webhook_arrives(app, staff, db):
    from fastapi.testclient import TestClient
    assert "Never" in staff["admin"].get("/system").text
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
    sig = "sha256=" + hmac.new(get_settings().whatsapp_app_secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
    bad = TestClient(app).post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert bad.status_code == 403 and "Never" in staff["admin"].get("/system").text              # a REJECTED event does not count
    assert TestClient(app).post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
    html = staff["admin"].get("/system").text
    assert "Never" not in html.split("Last webhook event received")[1].split("</dd>")[0] and re.search(r"\d\d \w{3} \d{4} \d\d:\d\d:\d\d \(UTC\)", html)


def test_batch_counts_on_the_status_page(staff, db):
    from datetime import timedelta
    from app.models import Batch
    from app.models.mixins import utcnow
    ready_batch(staff["hr"], db)                                                      # 1 prepared batch
    old = Batch(pay_month_label="January 2026", status="draft", created_at=utcnow() - timedelta(days=90))
    db.add(old)
    db.commit()
    html = staff["admin"].get("/system").text
    assert re.search(r"Batches being prepared \(draft or ready\)</dt><dd class=\"num\">2</dd>", html)
    assert re.search(r"Old batches still holding files</dt><dd class=\"num\">1 ", html) and "purge_old_batches" in html


def test_the_page_is_read_only_there_is_nothing_to_post(app, staff):
    html = staff["admin"].get("/system").text
    assert "<form" not in html.split("</aside>")[1].split("<dialog")[0].replace("<form method=\"dialog\"", "") or "action=\"/system" not in html
    assert staff["admin"].post("/system", data={"csrf_token": token(staff["admin"])}).status_code == 405


# ---- dashboard -----------------------------------------------------------------------------------------------------

def test_dashboard_shows_the_latest_batch_ribbon_and_delivery_rate_after_sending(staff, db, app):
    from tests.send_helpers import fire_events
    bid = ready_batch(staff["hr"], db)
    html = staff["admin"].get("/").text
    assert "Latest batch" in html and "November 2026" in html and "Ready to send" in html and "3 matched" in html and "nothing needs attention" in html
    send_now(staff["hr"], bid)
    fire_events(app)
    html = staff["admin"].get("/").text
    assert 'data-ribbon' in html and "<strong>100%</strong> delivered or read (3 of 3 sent)" in html and 'aria-label="3 of 3 delivered or read"' in html
    assert "Last sent batch" not in html                                               # it IS the latest, so not repeated


def test_dashboard_shows_the_last_sent_batch_when_a_newer_draft_exists(staff, db, app):
    from tests.send_helpers import fire_events
    first = ready_batch(staff["hr"], db)
    send_now(staff["hr"], first)
    fire_events(app)
    from tests.batch_helpers import create
    from tests.pdfs import named_pdf
    create(staff["hr"], [("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe"))], label="December 2026")
    html = staff["admin"].get("/").text
    assert "December 2026" in html.split("Last sent batch")[0] and "Last sent batch: November 2026" in html and "<strong>100%</strong>" in html


def test_dashboard_recent_activity_feed_is_admin_only_and_has_no_details(staff, db):
    ready_batch(staff["hr"], db)
    html = staff["admin"].get("/").text
    feed = html.split("Recent activity")[1]
    assert "Batch created" in feed and "the.hr" in feed and "files=" not in feed and "username '" not in feed
    assert "Recent activity" not in staff["hr"].get("/").text and "Recent activity" not in staff["viewer"].get("/").text


def test_dashboard_empty_state_and_employee_counts(staff, db):
    html = staff["admin"].get("/").text
    assert "No batches yet" in html and 'href="/batches/new"' in html
    assert re.findall(r'<div class="ledger-count[^"]*">(\d+)</div>\s*<div class="ledger-label">([^<]+)<', html)[:2] == [("0", "Active employees"), ("0", "Inactive employees")]
