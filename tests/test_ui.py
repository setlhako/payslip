"""Layout, accessibility basics and the 'everything is self-hosted' rules."""
import re
from pathlib import Path

from app.templating import BADGE_ICONS
from tests.conftest import csrf_from, make_employee

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "app" / "templates").glob("*.html"))


def test_every_page_has_the_sidebar_with_user_logout_and_coming_soon_items(logged_in):
    for path in ("/", "/employees", "/employees/new", "/employees/import", "/batches", "/batches/new"):
        html = logged_in.get(path).text
        assert 'aria-label="Main"' in html and ">Dashboard</span>" in html and ">Employees</span>" in html
        assert "hr.admin" in html                                             # the signed-in username
        assert 'aria-label="Log out"' in html and 'action="/logout"' in html and 'name="csrf_token"' in html
        assert 'aria-disabled="true"' not in html and "Coming soon" not in html               # no placeholders are left
        assert 'href="/batches"' in html and ">Batches</span>" in html and 'href="/audit"' in html and ">Audit log</span>" in html   # (the admin sees the audit log)
        assert 'class="mark"' in html and 'class="skip-link"' in html


def test_current_page_is_marked_in_the_navigation(logged_in):
    assert re.search(r'href="/" aria-current="page"', logged_in.get("/").text)
    html = logged_in.get("/employees").text
    assert re.search(r'href="/employees" aria-current="page"', html) and 'href="/" aria-current' not in html
    assert re.search(r'href="/employees" aria-current="page"', logged_in.get("/employees/import").text)


def test_page_header_has_title_and_primary_action(logged_in):
    html = logged_in.get("/employees").text
    assert "<h1>Employees</h1>" in html and "btn btn-primary" in html and "Add employee" in html


def test_dashboard_overview_numbers(logged_in, db):
    make_employee(db, "Ready", "One", "+26771234567", "1234")
    make_employee(db, "No", "Phone", None, "1234")
    make_employee(db, "No", "Id", "+26771234568", None)
    make_employee(db, "Gone", "Away", active=False)
    html = logged_in.get("/").text
    counts = re.findall(r'<div class="ledger-count[^"]*">(\d+)</div>\s*<div class="ledger-label">([^<]+)<', html)
    assert counts == [("3", "Active employees"), ("1", "Inactive employees"), ("1", "Active, no phone number"), ("1", "Active, no ID on file")]
    assert "Your payslip run" in html and "Coming soon" not in html


def test_flash_messages_are_dismissible_and_announced(logged_in, db):
    make_employee(db, "Thabo", "Molefe")
    logged_in.post("/employees/1/deactivate", data={"csrf_token": csrf_from(logged_in.get("/").text)}, follow_redirects=False)
    html = logged_in.get("/employees").text
    assert 'class="flash flash-success" role="status"' in html
    assert "data-dismiss" in html and 'aria-label="Dismiss this message"' in html
    assert "flash" not in logged_in.get("/employees").text.split("<main")[1].split("page-head")[0]   # shown once


def test_error_messages_use_role_alert(logged_in):
    logged_in.post("/employees/import/confirm", data={"csrf_token": csrf_from(logged_in.get("/").text)}, follow_redirects=False)
    assert 'role="alert"' in logged_in.get("/employees/import").text


def test_login_page_design_and_labels(client):
    html = client.get("/login").text
    assert 'for="username"' in html and 'for="password"' in html and 'class="glass-card"' in html
    assert "login-art" not in html and "Use the HR admin account" not in html and "Payslips reach" not in html     # just the sign-in card, nothing else
    assert re.search(r"<h1[^>]*>Log in</h1>", html) and 'type="password"' in html and 'autocomplete="current-password"' in html


def test_error_page_is_helpful(logged_in):
    r = logged_in.get("/no-such-page")
    assert r.status_code == 404 and "Page not found" in r.text and 'href="/"' in r.text


# ---- self-hosted assets and strict CSP ---------------------------------------------------------------------------

def test_static_assets_are_served_with_the_right_types(client):
    css, js, svg = client.get("/static/style.css"), client.get("/static/app.js"), client.get("/static/icons.svg")
    assert css.status_code == js.status_code == svg.status_code == 200
    assert "text/css" in css.headers["content-type"] and "javascript" in js.headers["content-type"]
    assert "image/svg+xml" in svg.headers["content-type"]


def test_nothing_is_loaded_from_outside_the_site():
    for path in [*TEMPLATES, ROOT / "app/static/style.css", ROOT / "app/static/app.js", ROOT / "app/static/icons.svg"]:
        text = path.read_text(encoding="utf-8")
        # the only allowed absolute URL is the SVG namespace declaration
        urls = [u for u in re.findall(r"https?://[^\s\"')]+", text) if u != "http://www.w3.org/2000/svg"]
        assert urls == [], path.name
        assert "@import" not in text and "fonts.googleapis" not in text


def test_icon_sprite_has_no_inline_style_and_every_used_icon_exists():
    sprite = (ROOT / "app/static/icons.svg").read_text(encoding="utf-8")
    assert "<style" not in sprite and "style=" not in sprite       # the strict CSP would block them
    defined = set(re.findall(r'<symbol id="i-([\w-]+)"', sprite))
    used = set(BADGE_ICONS.values()) | {"eye-off"}                  # eye-off is swapped in by app.js
    for path in TEMPLATES:
        used |= set(re.findall(r"icon\(\s*'([\w-]+)'", path.read_text(encoding="utf-8")))
        used |= set(re.findall(r"'((?:[\w]+-)*[\w]+)' if s ==", path.read_text(encoding="utf-8")))
    used |= {"arrow-up", "arrow-down", "sort", "check-circle", "x-circle", "info"}
    assert used <= defined, f"missing icons: {sorted(used - defined)}"


def test_icon_only_buttons_have_accessible_names():
    for path in TEMPLATES:
        for button in re.findall(r"<(?:button|summary)[^>]*icon-btn[^>]*>", path.read_text(encoding="utf-8")):
            assert "aria-label=" in button, f"{path.name}: {button}"


def test_stylesheet_defines_its_colours_as_variables():
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    root_block = css.split(":root {", 1)[1].split("\n}", 1)[0]
    for name in ("--bg", "--surface", "--text", "--primary", "--line", "--ok-fg", "--warn-fg", "--danger-fg", "--r-md"):
        assert f"{name}:" in root_block, name
    assert "color-scheme: light" in css
    assert ":focus-visible" in css and "prefers-reduced-motion" in css and "@media (max-width: 900px)" in css


def test_script_is_external_and_csp_allows_only_self(logged_in):
    r = logged_in.get("/employees")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp
    assert re.search(r'<script src="/static/app\.js\?v=[0-9a-f]{10}" defer></script>', r.text) and "<script>" not in r.text


def test_app_js_makes_one_request_only_the_reveal_form_post():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    for forbidden in ("XMLHttpRequest", "sendBeacon", "localStorage", "sessionStorage", "console.", "eval(", "innerHTML"):
        assert forbidden not in js, forbidden
    assert js.count("fetch(") == 1 and 'form.getAttribute("action")' in js    # the URL comes from the server-made form
    assert "http://" not in js and "https://" not in js and "/employees" not in js
    assert "data-secret" in js and "data-toggle-ids" in js and "aria-pressed" in js


def test_app_js_is_plain_ascii_so_no_server_setting_can_garble_it():
    assert (ROOT / "app/static/app.js").read_bytes().isascii()


def test_static_urls_change_when_the_file_changes_so_browsers_never_keep_an_old_script(client, tmp_path, monkeypatch):
    from app import templating

    (tmp_path / "app.js").write_text("// version one", encoding="utf-8")
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templating, "_static_versions", {})
    first = templating.static_url("app.js")
    assert re.fullmatch(r"/static/app\.js\?v=[0-9a-f]{10}", first) and templating.static_url("app.js") == first
    (tmp_path / "app.js").write_text("// version two (changed)", encoding="utf-8")
    import os
    os.utime(tmp_path / "app.js", (1, 1_900_000_000))       # make sure the modified time moves even on a fast disk
    assert templating.static_url("app.js") != first
    assert templating.static_url("missing.js") == "/static/missing.js"   # never breaks a page


def test_static_files_must_be_rechecked_by_the_browser(client):
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/login").headers["cache-control"] == "no-store"
