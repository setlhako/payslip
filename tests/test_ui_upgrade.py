"""The UI upgrade: glass login, sidebar layout, send preview, dashboard attention card + admin banners, employee list tools
(bulk deactivate, export, filters, last payslip) and the inactivity warning."""
import csv
import io
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import AuditLog, Batch, Employee, Payslip, SystemState
from app.models.mixins import utcnow
from app.security import sessions
from tests.conftest import csrf_from, make_employee
from tests.send_helpers import ready_batch, send_now
from tests.test_theme_ui import CSS, DARK, LIGHT, contrast
from tests.user_helpers import login_as, make_user, post, staff, token  # noqa: F401  (staff is a fixture)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
TEMPLATES = sorted((ROOT / "app" / "templates").glob("*.html"))


# =====================================================================================================================
# B. login page
# =====================================================================================================================

def test_login_is_a_glass_card_with_a_round_logo_badge_and_pill_inputs(client):
    html = client.get("/login").text
    assert 'class="glass-card"' in html and 'class="login-badge"' in html
    assert "<svg" in html.split('class="login-badge"')[1].split("</div>")[0]
    assert html.count('class="login-blob') == 4 and 'class="login-bg" aria-hidden="true"' in html
    assert 'class="input pill"' in html and 'class="input pill has-trail"' in html and "btn-pill" in html
    assert 'for="username"' in html and 'for="password"' in html and "Payslip Sender" in html
    assert "<img" not in html and ".jpg" not in html and ".png" not in html                # drawn in CSS, no pictures


def test_login_has_no_remember_me_no_social_login_and_a_forgot_password_note(client):
    html = client.get("/login").text.lower()
    assert "remember" not in html and "google" not in html and "facebook" not in html and "sign in with" not in html
    note = re.search(r'<details class="forgot">\s*<summary>forgot password\?</summary>\s*<p[^>]*>(.*?)</p>', html, re.S)
    assert note and "reset by an administrator" in note.group(1)


def test_show_hide_password_button_is_accessible_and_script_only(client):
    html = client.get("/login").text
    button = re.search(r"<button[^>]*data-toggle-password[^>]*>", html).group(0)
    assert 'type="button"' in button and 'aria-label="Show password"' in button and 'aria-pressed="false"' in button
    assert "needs-js" in button
    assert 'type="password"' in html                                                          # hidden until the button is used
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "data-toggle-password" in js and '"Hide password"' in js and js.isascii()


def test_login_keeps_its_generic_errors_and_security_behaviour(client, admin):
    token_ = csrf_from(client.get("/login").text)
    r = client.post("/login", data={"username": "nobody", "password": "wrong-password-1", "csrf_token": token_})
    assert r.status_code == 401 and "Invalid username or password." in r.text and 'role="alert"' in r.text
    assert "login-alert-error" in r.text and "login-alert-lock" not in r.text
    assert 'aria-describedby="login-error"' in r.text and 'id="login-error"' in r.text


def test_the_lockout_message_is_shown_in_its_own_style(client, admin):
    for _ in range(get_settings().login_max_attempts):
        r = client.post("/login", data={"username": admin.username, "password": "wrong-password-1",
                                        "csrf_token": csrf_from(client.get("/login").text)})
    assert r.status_code == 429 and "login-alert-lock" in r.text
    assert "Account temporarily locked" in r.text and "Too many failed attempts" in r.text


def _rgba(value: str):
    m = re.match(r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\s*\)", value.strip())
    return int(m[1]), int(m[2]), int(m[3]), float(m[4] or 1)


def _composite(fg: str, bg_hex: str) -> str:
    r, g, b, a = _rgba(fg)
    h = bg_hex.lstrip("#")
    base = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(a * c + (1 - a) * back):02x}" for c, back in zip((r, g, b), base))


@pytest.mark.parametrize("theme,values", [("light", LIGHT), ("dark", DARK)])
def test_text_on_the_glass_card_stays_readable_over_every_background_shape(theme, values):
    v = {**LIGHT, **values}
    for blob in ("--blob-1", "--blob-2", "--blob-3", "--blob-4", "--login-base"):
        card = _composite(v["--glass-bg"], v[blob])
        for text in ("--text", "--text-muted", "--primary"):
            assert contrast(v[text], card) >= 4.5, (theme, blob, text, round(contrast(v[text], card), 2))
    assert contrast(v["--login-badge-ink"], v["--login-badge-bg"]) >= 4.5


def test_login_css_has_frosted_glass_a_fallback_reduced_motion_and_no_images():
    login = CSS[CSS.index("/* ---- login:"):CSS.index(".plain {")]
    assert "backdrop-filter: blur(22px)" in login and "-webkit-backdrop-filter" in login and "@supports not" in login
    assert "prefers-reduced-motion: no-preference" in login                                 # drift only for people who allow motion
    assert "url(" not in login and "border-radius: 28px" in login and "radial-gradient" in login and "filter: blur(60px)" in login


# =====================================================================================================================
# C. sidebar layout
# =====================================================================================================================

def _side(html: str) -> str:
    return html.split('<aside class="sidebar"')[1].split("</aside>")[0]


def test_sidebar_has_logo_search_nav_theme_switch_user_and_logout(staff):
    html = staff["admin"].get("/").text
    side = _side(html)
    assert 'class="mark"' in side and "Payslip Sender" in side
    assert re.search(r'<form class="side-search" role="search" action="/employees" method="get">', side)
    assert 'placeholder="Search employees..."' in side and 'name="q"' in side
    assert 'aria-keyshortcuts="/"' in side and 'class="kbd"' in side
    for label in ("Dashboard", "Employees", "Batches", "Users", "Audit log", "System status"):
        assert f'<span class="nav-label">{label}</span>' in side
        assert f'<span class="nav-tip" aria-hidden="true">{label}</span>' in side
    assert re.search(r"<button[^>]*data-theme-toggle[^>]*>", side) and "switch-track" in side
    assert "hr.admin" in side and ">Admin<" in side                                          # the signed-in name and role
    assert 'aria-label="Log out"' in side and 'action="/logout"' in side
    assert re.search(r'<button[^>]*data-sidebar-toggle[^>]*aria-expanded="true"[^>]*aria-label="Collapse sidebar"', side)
    assert "data-drawer-open" in html and 'class="drawer-backdrop" data-drawer-close hidden' in html


@pytest.mark.parametrize("role,extra", [("admin", ["Users", "Audit log", "System status"]), ("hr", []), ("viewer", [])])
def test_sidebar_items_follow_the_role(staff, role, extra):
    side = _side(staff[role].get("/").text)
    for label in ["Dashboard", "Employees", "Batches", *extra]:
        assert f'<span class="nav-label">{label}</span>' in side, label
    for label in {"Users", "Audit log", "System status"} - set(extra):
        assert f'<span class="nav-label">{label}</span>' not in side, label
    assert ("nav-heading" in side) == bool(extra)


def test_server_still_refuses_what_the_sidebar_hides(staff):
    assert staff["hr"].get("/users").status_code == 403 and staff["viewer"].get("/audit").status_code == 403


def test_the_sidebar_search_finds_active_and_inactive_employees(staff, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567")
    make_employee(db, "Gone", "Away", "+26771234568", active=False)
    html = staff["viewer"].get("/employees", params={"q": "away", "status": "all"}).text
    assert "Gone Away" in html and "Thabo Molefe" not in html
    assert 'name="status" value="all"' in _side(staff["viewer"].get("/").text)


def test_the_employee_page_search_box_takes_the_slash_key_itself(staff, db):
    make_employee(db, "Thabo", "Molefe")
    assert "data-page-search" in staff["admin"].get("/employees").text


def test_sidebar_script_is_self_hosted_and_has_the_keyboard_rules():
    js = (STATIC / "sidebar.js").read_bytes().decode("ascii")
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "innerHTML", "eval(", "console.", "http://", "https://", "document.cookie"):
        assert forbidden not in js, forbidden
    assert 'KEY = "payslip-sidebar"' in js and js.count("window.localStorage") == 2 and "catch" in js
    assert 'event.key === "Escape"' in js and 'event.key !== "Tab"' in js and 'event.key !== "/"' in js
    assert 'setAttribute("inert"' in js and 'setAttribute("aria-modal", "true")' in js and "tagName" in js     # trap, modal, not while typing


def test_sidebar_script_loads_in_the_head_without_defer_so_the_choice_applies_before_paint(staff):
    head = staff["admin"].get("/").text.split("</head>")[0]
    tag = re.search(r'<script src="/static/sidebar\.js\?v=[0-9a-f]{10}"></script>', head)
    assert tag and "defer" not in tag.group(0) and tag.start() < head.index('<link rel="stylesheet"')


def test_sidebar_css_has_a_collapsed_mode_a_drawer_and_visible_tooltips():
    assert 'html[data-sidebar="collapsed"]' in CSS and "--sidebar-w: 76px" in CSS
    assert 'html[data-drawer="open"] .sidebar' in CSS and "translateX(-102%)" in CSS and "visibility: hidden" in CSS
    assert ":is(.nav-link, .account-link, .logout-btn, .side-search-btn):is(:hover, :focus-visible) > .nav-tip" in CSS
    collapsed = CSS[CSS.index("collapsed: icons only"):CSS.index(".content { min-width: 0")]
    assert "@media (min-width: 901px)" in collapsed and "clip: rect(0 0 0 0)" in collapsed       # words stay for screen readers
    assert "html:not(.js) .sidebar" in CSS                                                          # without scripts it stays a plain block


# =====================================================================================================================
# D1. send confirmation preview
# =====================================================================================================================

def _preview(html: str) -> str:
    return html.split('aria-labelledby="preview-title"')[1].split("</section>")[0]


def test_send_page_previews_the_message_with_a_sample_name_the_month_the_file_and_the_password_rule(staff, db):
    bid = ready_batch(staff["hr"], db)
    html = staff["hr"].get(f"/batches/{bid}/send").text
    section = _preview(html)
    assert "What each employee will receive" in html
    assert 'class="wa-var">Thabo Molefe</mark>' in section and 'class="wa-var">November 2026</mark>' in section
    assert "Payslip - November 2026.pdf" in section
    assert "last 4 characters" in section and "4567" in section and "CD34" in section and "capital letters" in section
    assert "Template message" in section and get_settings().whatsapp_template_name in section
    assert "Payslips to send" in html and "MOCK MODE" in html and 'name="confirm_label"' in html   # existing summary and confirm step stay


def test_the_preview_never_shows_a_real_employees_name(staff, db):
    bid = ready_batch(staff["hr"], db, [("Zanele", "Uniquename", "+26771111111", "1111")])
    section = _preview(staff["hr"].get(f"/batches/{bid}/send").text)
    assert "Zanele" not in section and "Uniquename" not in section


def test_the_preview_uses_the_configured_wording_and_escapes_it(staff, db, monkeypatch):
    bid = ready_batch(staff["hr"], db)
    monkeypatch.setattr(get_settings(), "whatsapp_template_preview_text", "Dear {{1}} <b>x</b>, pay for {{2}}: see PDF.")
    section = _preview(staff["hr"].get(f"/batches/{bid}/send").text)
    assert "Dear <mark" in section and "&lt;b&gt;x&lt;/b&gt;" in section and "<b>x</b>" not in section and "pay for <mark" in section


def test_in_document_mode_the_preview_shows_the_caption(staff, db, monkeypatch):
    bid = ready_batch(staff["hr"], db)
    monkeypatch.setattr(get_settings(), "whatsapp_send_kind", "document")
    section = _preview(staff["hr"].get(f"/batches/{bid}/send").text)
    assert "plain document" in section and "24-hour" in section and "Template message" not in section


# =====================================================================================================================
# D2. dashboard: needs attention + admin banners
# =====================================================================================================================

def _card(html: str) -> str:
    return html.split('aria-labelledby="attention-title"')[1].split("</section>")[0]


def test_needs_attention_counts_missing_phone_missing_id_and_shared_names(staff, db):
    make_employee(db, "No", "Phone", None, "1111")
    make_employee(db, "No", "Id", "+26771111111", None)
    make_employee(db, "Same", "Name", "+26772222222", "2222")
    make_employee(db, "Same", "Name", "+26773333333", "3333")
    make_employee(db, "Gone", "Inactive", None, None, active=False)                     # inactive people are not counted
    card = _card(staff["viewer"].get("/").text)
    assert "3 things to look at" in card
    assert re.search(r"<strong>1</strong> active employee without a phone", card) and 'href="/employees?show=no_phone"' in card
    assert re.search(r"<strong>1</strong> active employee without an ID", card) and 'href="/employees?show=no_id"' in card
    assert re.search(r"<strong>2</strong> active employees share 1 name", card) and 'href="/employees?show=dup_name"' in card


def test_needs_attention_says_all_clear_when_nothing_is_wrong(staff, db):
    make_employee(db, "Fine", "Person", "+26771111111", "1111")
    card = _card(staff["hr"].get("/").text)
    assert "All clear" in card and "things to look at" not in card


def test_the_needs_attention_links_filter_the_employee_list(staff, db):
    make_employee(db, "No", "Phone", None, "1111")
    make_employee(db, "Has", "Phone", "+26771111111", "2222")
    html = staff["viewer"].get("/employees", params={"show": "no_phone"}).text
    assert "No Phone" in html and "Has Phone" not in html and 'aria-current="true"' in html


def _audit(db, action, when=None):
    db.add(AuditLog(action=action, created_at=when or utcnow(), details="code=190"))
    db.commit()


def test_admin_sees_a_banner_when_whatsapp_rejected_the_token_and_other_roles_do_not(staff, db):
    _audit(db, "send_token_error", utcnow() - timedelta(hours=2))
    admin_html = staff["admin"].get("/").text
    assert 'class="alert-banner alert-token"' in admin_html and "WhatsApp rejected the access token" in admin_html
    assert "WHATSAPP_ACCESS_TOKEN" in admin_html
    assert "alert-banner" not in staff["hr"].get("/").text and "alert-banner" not in staff["viewer"].get("/").text


def test_the_token_banner_goes_away_after_a_later_successful_send(staff, db):
    bid = ready_batch(staff["hr"], db)
    _audit(db, "send_token_error", utcnow() - timedelta(hours=2))
    assert "alert-token" in staff["admin"].get("/").text
    send_now(staff["hr"], bid)                                                            # mock sends succeed: sent_at is now
    assert "alert-token" not in staff["admin"].get("/").text


def test_the_token_banner_links_to_the_stopped_batch(staff, db):
    bid = ready_batch(staff["hr"], db)
    batch = db.get(Batch, bid)
    batch.status, batch.send_state = "sending", "halted"
    db.commit()
    _audit(db, "send_token_error")
    banner = staff["admin"].get("/").text.split("alert-token")[1].split("</a>")[0]
    assert f'href="/batches/{bid}"' in banner


def _live_sent_payslip(db, staff, minutes_ago):
    bid = ready_batch(staff["hr"], db)
    db.get(Batch, bid).send_mode = "live"
    pay = db.scalars(select(Payslip).where(Payslip.batch_id == bid)).first()
    pay.status, pay.sent_at = "sent", utcnow() - timedelta(minutes=minutes_ago)
    db.commit()
    return pay


def test_admin_sees_a_banner_when_no_delivery_reports_arrive_in_live_mode(staff, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "live")
    _live_sent_payslip(db, staff, minutes_ago=30)
    html = staff["admin"].get("/").text
    assert 'class="alert-banner alert-webhook"' in html and "No delivery reports are arriving" in html
    assert "WHATSAPP_VERIFY_TOKEN" in html and "alert-banner" not in staff["hr"].get("/").text


def test_no_webhook_banner_when_a_report_arrived_after_the_send_or_it_is_too_soon_or_mock_mode(staff, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "live")
    pay = _live_sent_payslip(db, staff, minutes_ago=30)
    db.add(SystemState(key="last_webhook_at", value="valid", updated_at=utcnow() - timedelta(minutes=5)))   # a report came in after the send
    db.commit()
    assert "alert-webhook" not in staff["admin"].get("/").text
    db.query(SystemState).delete()
    pay.sent_at = utcnow() - timedelta(minutes=2)                                           # too soon to worry
    db.commit()
    assert "alert-webhook" not in staff["admin"].get("/").text
    pay.sent_at = utcnow() - timedelta(minutes=30)
    db.commit()
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "mock")
    assert "alert-webhook" not in staff["admin"].get("/").text


# =====================================================================================================================
# D4. employees table
# =====================================================================================================================

def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.text.lstrip("﻿"))))


def _row(html: str, name: str) -> str:
    return re.search(name + r".*?</tr>", html, re.S).group(0)


def test_the_list_shows_id_type_tags_and_the_last_payslip_month_and_status(staff, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567", id_number="123456789")
    make_employee(db, "Chipo", "Fake", "+26773456789", id_number="AB1234567", id_type="passport")
    make_employee(db, "New", "Person", "+26770000001")
    bid = ready_batch(staff["hr"], db, [("Neo", "Dube", "+26772345678", "4321")])
    send_now(staff["hr"], bid)
    html = staff["admin"].get("/employees").text
    assert 'class="tag tag-omang">Omang<' in html and 'class="tag tag-passport">Passport<' in html and "Last payslip" in html
    assert "November 2026" in _row(html, "Neo Dube") and "badge-sent" in _row(html, "Neo Dube")
    assert "None yet" in _row(html, "New Person")


def test_last_payslip_ignores_payslips_that_were_never_sent(staff, db):
    ready_batch(staff["hr"], db, [("Neo", "Dube", "+26772345678", "4321")])                 # ready, not sent: status pending
    row = _row(staff["admin"].get("/employees").text, "Neo Dube")
    assert "None yet" in row and "November 2026" not in row


def test_filter_chips_count_and_filter(staff, db):
    make_employee(db, "O", "Mang", "+26771111111", id_number="123456789")
    make_employee(db, "P", "Port", "+26772222222", id_number="AB1234567", id_type="passport")
    make_employee(db, "N", "Oid", "+26773333333")
    html = staff["admin"].get("/employees").text
    assert re.search(r'Everyone <span class="count">3</span>', html) and re.search(r'Omang <span class="count">1</span>', html)
    assert re.search(r'No ID <span class="count">1</span>', html)
    only = staff["admin"].get("/employees", params={"show": "passport"}).text
    assert "P Port" in only and "O Mang" not in only and "N Oid" not in only
    assert "N Oid" in staff["admin"].get("/employees", params={"show": "no_id"}).text
    assert "P Port" in staff["admin"].get("/employees", params={"show": "nonsense"}).text      # unknown filter = everyone


def test_bulk_deactivate_deactivates_only_the_ticked_active_people_and_audits_each(staff, db):
    a, b, c = (make_employee(db, n, "Test", f"+2677{i}000001") for i, n in enumerate(("A", "B", "C"), start=1))
    gone = make_employee(db, "G", "Test", "+26774000001", active=False)
    html = staff["hr"].get("/employees", params={"status": "all"}).text
    assert "data-select-all" in html and html.count("data-select-row") == 3 and 'form="bulk-form"' in html      # inactive rows: no checkbox
    r = post(staff["hr"], "/employees/bulk-deactivate", employee_ids=[str(a.id), str(b.id), str(gone.id)], q="",
             status="active", sort="name", dir="asc", per_page="25", page="1", show="")
    assert r.status_code == 303 and r.headers["location"] == "/employees"
    db.expire_all()
    assert [e.active for e in (a, b, c, gone)] == [False, False, True, False]
    rows = list(db.scalars(select(AuditLog).where(AuditLog.action == "employee_deactivated")))
    assert sorted(x.entity_id for x in rows) == sorted([a.id, b.id]) and all(x.details == "bulk" for x in rows)
    assert "2 employees deactivated" in staff["hr"].get("/employees").text


def test_bulk_deactivate_keeps_the_list_settings_and_needs_a_selection(staff, db):
    make_employee(db, "A", "Test", "+26771000001")
    r = post(staff["hr"], "/employees/bulk-deactivate", q="te", status="all", sort="status", dir="desc", per_page="50", page="2", show="no_id")
    location = r.headers["location"]
    assert "q=te" in location and "status=all" in location and "show=no_id" in location
    assert "Tick at least one employee first" in staff["hr"].get("/employees").text


def test_bulk_deactivate_is_hr_and_admin_only_capped_and_needs_csrf(staff, db):
    e = make_employee(db, "A", "Test", "+26771000001")
    assert post(staff["viewer"], "/employees/bulk-deactivate", employee_ids=[str(e.id)]).status_code == 403
    db.expire_all()
    assert db.get(Employee, e.id).active is True
    assert staff["hr"].post("/employees/bulk-deactivate", data={"employee_ids": [str(e.id)]}).status_code == 403     # no CSRF token
    post(staff["hr"], "/employees/bulk-deactivate", employee_ids=[str(i) for i in range(1, 300)])
    assert "at most 200" in staff["hr"].get("/employees").text


def test_viewers_do_not_get_checkboxes_or_the_bulk_bar(staff, db):
    make_employee(db, "A", "Test", "+26771000001")
    html = staff["viewer"].get("/employees").text
    assert "data-select-row" not in html and "bulk-form" not in html


def test_export_has_names_phones_and_status_only_and_never_any_id(staff, db):
    e = make_employee(db, "Thabo", "Molefe", "+26771234567", id_number="987654321")
    make_employee(db, "Gone", "Away", "+26772345678", id_number="ZX9988776", id_type="passport", active=False)
    make_employee(db, "=cmd", "Evil", None)
    r = staff["hr"].get("/employees/export.csv", params={"status": "all"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "no-store" in r.headers["cache-control"]
    assert 'attachment; filename="employees.csv"' in r.headers["content-disposition"]
    rows = _csv_rows(r)
    assert rows[0] == ["First name", "Surname", "Phone", "Status"]
    assert ["Thabo", "Molefe", "+26771234567", "Active"] in rows and ["Gone", "Away", "+26772345678", "Inactive"] in rows
    assert ["'=cmd", "Evil", "", "Active"] in rows                                          # spreadsheet formula guard
    db.expire_all()
    stored = db.get(Employee, e.id)
    for secret in ("987654321", "ZX9988776", "4321", "8776", stored.id_number_encrypted, stored.id_number_hash):
        assert secret not in r.text, secret


def test_export_follows_the_current_search_status_and_filter_and_is_audited(staff, db):
    make_employee(db, "Thabo", "Molefe", "+26771234567")
    make_employee(db, "Neo", "Dube", None)
    make_employee(db, "Gone", "Away", "+26772345678", active=False)
    assert [r[0] for r in _csv_rows(staff["viewer"].get("/employees/export.csv"))[1:]] == ["Neo", "Thabo"]          # default = active only
    assert [r[0] for r in _csv_rows(staff["viewer"].get("/employees/export.csv", params={"q": "thabo"}))[1:]] == ["Thabo"]
    assert [r[0] for r in _csv_rows(staff["viewer"].get("/employees/export.csv", params={"show": "no_phone"}))[1:]] == ["Neo"]
    entries = list(db.scalars(select(AuditLog).where(AuditLog.action == "employees_exported").order_by(AuditLog.id)))
    assert [e.details for e in entries] == ["rows=2", "rows=1", "rows=1"]
    assert "Export list (no ID numbers)" in staff["viewer"].get("/employees").text


def test_export_needs_a_login(client):
    assert client.get("/employees/export.csv", follow_redirects=False).status_code == 303


# =====================================================================================================================
# D5. inactivity warning
# =====================================================================================================================

IDLE = lambda: get_settings().session_idle_minutes * 60   # noqa: E731


def test_status_reports_the_time_left_without_counting_as_activity(staff, monkeypatch):
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sessions, "now", lambda: clock["t"])
    client = staff["hr"]
    client.get("/")                                                                          # a page load: the idle clock starts here
    clock["t"] += 600
    first = client.get("/session/status").json()["remaining"]
    clock["t"] += 600
    second = client.get("/session/status").json()["remaining"]
    assert first == IDLE() - 600 and second == first - 600                                   # asking did NOT reset the clock


def test_keepalive_resets_the_idle_clock_and_needs_the_csrf_token(staff, monkeypatch):
    clock = {"t": 2_000_000.0}
    monkeypatch.setattr(sessions, "now", lambda: clock["t"])
    client = staff["hr"]
    client.get("/")
    clock["t"] += 1500
    assert client.post("/session/keepalive").status_code == 403                              # no token
    r = client.post("/session/keepalive", headers={"X-CSRF-Token": token(client)})
    assert r.status_code == 200 and r.json()["remaining"] == IDLE()
    clock["t"] += 1700
    assert client.get("/session/status").json()["remaining"] == IDLE() - 1700


def test_status_never_goes_past_the_absolute_limit(app, staff, monkeypatch):
    clock = {"t": 3_000_000.0}
    monkeypatch.setattr(sessions, "now", lambda: clock["t"])
    client = login_as(app, "the.hr")                                                         # a session that starts on the fake clock
    started = clock["t"]
    absolute = get_settings().session_absolute_hours * 3600
    while absolute - (clock["t"] - started) > 2100:
        clock["t"] += 1500
        client.post("/session/keepalive", headers={"X-CSRF-Token": token(client)})
    clock["t"] = started + absolute - 600                                                    # 10 minutes before the 8-hour limit
    client.post("/session/keepalive", headers={"X-CSRF-Token": token(client)})
    assert client.get("/session/status").json()["remaining"] == 600 < IDLE()                 # the shorter limit wins


def test_an_expired_session_is_sent_to_the_login_page_by_the_status_call(staff, monkeypatch):
    clock = {"t": 4_000_000.0}
    monkeypatch.setattr(sessions, "now", lambda: clock["t"])
    client = staff["hr"]
    client.get("/")
    clock["t"] += IDLE() + 5
    r = client.get("/session/status", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_session_endpoints_need_a_login_and_work_on_a_temporary_password(app, db, admin):
    from fastapi.testclient import TestClient
    assert TestClient(app).get("/session/status", follow_redirects=False).status_code == 303
    make_user(db, "temp.user", "hr", must_change=True)
    client = login_as(app, "temp.user")
    assert client.get("/session/status", follow_redirects=False).status_code == 200


def test_pages_carry_the_warning_dialog_and_the_script_but_the_login_page_does_not(staff, client):
    html = staff["viewer"].get("/").text
    dialog = re.search(r'<dialog class="dialog" id="idle-dialog" role="alertdialog" aria-labelledby="idle-title" '
                       r'aria-describedby="idle-text" data-idle-seconds="(\d+)">(.*?)</dialog>', html, re.S)
    assert dialog and int(dialog.group(1)) == IDLE()
    assert "Stay signed in" in dialog.group(2) and "Log out now" in dialog.group(2) and "You will be signed out in" in dialog.group(2)
    assert re.search(r'<script src="/static/session\.js\?v=[0-9a-f]{10}" defer></script>', html)
    assert "idle-dialog" not in client.get("/login").text


def test_session_script_is_self_hosted_ascii_and_only_talks_to_the_two_session_endpoints():
    js = (STATIC / "session.js").read_bytes().decode("ascii")
    assert sorted(set(re.findall(r'fetch\("([^"]+)"', js))) == ["/session/keepalive", "/session/status"]
    for forbidden in ("XMLHttpRequest", "sendBeacon", "innerHTML", "eval(", "console.", "http://", "https://", "document.cookie", "localStorage"):
        assert forbidden not in js, forbidden
    assert "X-CSRF-Token" in js and 'redirect: "manual"' in js and '"cancel"' in js and "visibilitychange" in js


# =====================================================================================================================
# D3. in-page modals only
# =====================================================================================================================

def test_no_browser_confirm_alert_or_prompt_anywhere():
    for path in [*STATIC.glob("*.js"), *TEMPLATES]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\s*\(", text), path.name


def test_every_dialog_is_a_native_modal_with_a_name_and_a_description(staff):
    for path in ("/", "/employees", "/users"):
        dialogs = re.findall(r"<dialog [^>]*>", staff["admin"].get(path).text)
        assert len(dialogs) >= 2
        for tag in dialogs:
            assert 'role="alertdialog"' in tag and "aria-labelledby=" in tag and "aria-describedby=" in tag, tag
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "showModal()" in js and "requestSubmit" in js
