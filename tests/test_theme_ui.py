"""Dark theme, contrast, strict-CSP hygiene, breadcrumbs, toasts, dialogs and accessibility hooks."""
import re
from pathlib import Path

import pytest

from tests.batch_helpers import page as batch_page
from tests.send_helpers import ready_batch, send_now
from tests.user_helpers import staff, token  # noqa: F401  (staff is a fixture)

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
TEMPLATES = sorted((ROOT / "app" / "templates").glob("*.html"))


# ---- reading the theme variables out of the stylesheet -------------------------------------------------------------

def _block(start_pattern: str) -> str:
    start = re.search(start_pattern, CSS, re.M)
    assert start, start_pattern
    depth, i = 0, CSS.index("{", start.start())
    for j in range(i, len(CSS)):
        depth += {"{": 1, "}": -1}.get(CSS[j], 0)
        if depth == 0:
            return CSS[i + 1:j]
    raise AssertionError("unbalanced braces")


def _vars(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


LIGHT = _vars(_block(r"^:root \{"))
DARK = _vars(_block(r':root\[data-theme="dark"\] \{'))
DARK_AUTO = _vars(_block(r':root:not\(\[data-theme="light"\]\) \{'))


def _lum(hex_color: str) -> float:
    h = hex_color.strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("--text", "--bg"), ("--text", "--surface"), ("--text", "--surface-sunken"), ("--text", "--sidebar-bg"), ("--text", "--hover-row"),
    ("--text", "--row-error-bg"), ("--text", "--row-attention-bg"),
    ("--text-muted", "--bg"), ("--text-muted", "--surface"), ("--text-muted", "--surface-sunken"), ("--text-muted", "--sidebar-bg"),
    ("--text-faint", "--surface"), ("--text-faint", "--surface-sunken"),
    ("--primary", "--surface"), ("--primary", "--bg"), ("--on-primary", "--primary"), ("--on-primary", "--primary-hover"),
    ("--primary-hover", "--primary-tint"), ("--primary", "--primary-tint"),
    ("--ok-fg", "--ok-bg"), ("--info-fg", "--info-bg"), ("--neutral-fg", "--neutral-bg"), ("--warn-fg", "--warn-bg"), ("--danger-fg", "--danger-bg"),
    ("--ok-fg", "--surface"), ("--info-fg", "--surface"), ("--warn-fg", "--surface"), ("--danger-fg", "--surface"), ("--neutral-fg", "--surface"),
    ("--danger-fg", "--row-error-bg"), ("--warn-fg", "--row-attention-bg"),
] + [(f"--av-{i}-fg", f"--av-{i}-bg") for i in range(6)]


@pytest.mark.parametrize("theme,values", [("light", LIGHT), ("dark", DARK)])
def test_every_text_colour_pair_meets_wcag_aa_in_both_themes(theme, values):
    merged = {**LIGHT, **values}                                      # the dark block overrides the light variables
    low = [(fg, bg, round(contrast(merged[fg], merged[bg]), 2)) for fg, bg in TEXT_PAIRS if contrast(merged[fg], merged[bg]) < 4.5]
    assert low == [], f"{theme} theme pairs below 4.5:1: {low}"


@pytest.mark.parametrize("theme,values", [("light", LIGHT), ("dark", DARK)])
def test_the_focus_ring_is_visible_against_every_surface(theme, values):
    merged = {**LIGHT, **values}
    for surface in ("--bg", "--surface", "--surface-sunken", "--sidebar-bg"):
        assert contrast(merged["--focus"], merged[surface]) >= 3, (theme, surface)


def test_the_two_dark_blocks_are_identical_so_the_toggle_and_the_system_setting_agree():
    assert DARK == DARK_AUTO and len(DARK) > 40


def test_dark_overrides_everything_the_light_theme_colours_except_a_few_shared_values():
    shared_ok = {"--font-ui", "--font-display", "--font-mono", "--r-sm", "--r-md", "--r-lg", "--r-pill", "--sidebar-w"}
    colour_vars = {k for k, v in LIGHT.items() if re.match(r"#|rgba?\(", v.strip())}
    missing = sorted(k for k in colour_vars - set(DARK) if k not in shared_ok)
    assert missing == [], f"light-only colours (they would stay light in the dark theme): {missing}"


def test_no_hard_coded_colours_outside_the_variable_blocks_except_the_flag_stripe_white():
    stripped = CSS
    for pattern in (r"^:root \{", r':root\[data-theme="dark"\] \{', r':root:not\(\[data-theme="light"\]\) \{'):
        start = re.search(pattern, stripped, re.M)
        i = stripped.index("{", start.start())
        depth = 0
        for j in range(i, len(stripped)):
            depth += {"{": 1, "}": -1}.get(stripped[j], 0)
            if depth == 0:
                stripped = stripped[:start.start()] + stripped[j + 1:]
                break
    colours = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", stripped)
    assert set(colours) <= {"#fff"} or all(c == "#fff" for c in colours if c.startswith("#")), sorted(set(colours))
    assert not [c for c in colours if c.startswith("rgb")], sorted(set(colours))


def test_the_system_dark_preference_is_followed_when_nothing_was_chosen():
    assert "@media (prefers-color-scheme: dark)" in CSS and ':root:not([data-theme="light"])' in CSS and "color-scheme: dark" in CSS


# ---- the theme script ----------------------------------------------------------------------------------------------

def test_theme_script_is_self_hosted_ascii_and_never_touches_the_network():
    js = (ROOT / "app/static/theme.js").read_bytes().decode("ascii")
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "innerHTML", "eval(", "console.", "http://", "https://", "document.cookie"):
        assert forbidden not in js, forbidden
    assert js.count("window.localStorage") == 2 and js.count("try {") >= 2 and "catch" in js                 # read and write, both guarded
    assert 'KEY = "payslip-theme"' in js and 'value === "dark" || value === "light"' in js            # only known values are trusted


def test_the_theme_is_applied_before_the_first_paint_and_the_toggle_is_accessible(staff):
    html = staff["admin"].get("/").text
    head = html.split("</head>")[0]
    script = re.search(r'<script src="/static/theme\.js\?v=[0-9a-f]{10}"></script>', head)
    assert script and "defer" not in script.group(0) and script.start() < head.index('<link rel="stylesheet"')      # first, blocking, before the CSS
    toggle = re.search(r"<button[^>]*data-theme-toggle[^>]*>", html).group(0)
    assert 'aria-pressed="false"' in toggle and 'aria-label="Dark theme"' in toggle and "needs-js" in toggle and 'type="button"' in toggle
    assert "icon-moon" in html and "icon-sun" in html


def test_the_login_page_also_loads_the_theme_and_has_no_toggle_requirement(app):
    from fastapi.testclient import TestClient
    html = TestClient(app).get("/login").text
    assert "theme.js" in html.split("</head>")[0] and "Log in" in html


# ---- strict CSP hygiene across every template ------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_scripts_styles_or_handlers_and_nothing_external(template):
    text = template.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), "inline <script>"
    assert not re.search(r"<style\b", text) and not re.search(r"\sstyle\s*=", text), "inline style"
    assert not re.search(r"\son[a-z]+\s*=", text), "inline event handler"
    assert "javascript:" not in text.lower()
    assert not re.search(r"""(?:src|href|action)=["']\s*(?:https?:)?//""", text), "external resource"


def test_the_stylesheet_has_no_external_fonts_imports_or_urls():
    assert "@import" not in CSS and not re.search(r"url\(\s*['\"]?(?:https?:)?//", CSS) and "fonts.googleapis" not in CSS
    assert "@font-face" not in CSS                                                                     # system fonts only


def test_the_csp_header_is_still_strict(staff):
    csp = staff["admin"].get("/").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "default-src 'self'" in csp and "unsafe" not in csp


# ---- breadcrumbs ----------------------------------------------------------------------------------------------------

def test_deep_pages_have_breadcrumbs_with_the_current_page_marked(staff, db):
    bid = ready_batch(staff["hr"], db)
    pages = {
        "/employees/new": ["Employees", "Add employee"], "/employees/import": ["Employees", "Import"],
        "/users": ["Users"], "/users/new": ["Users", "Add user"], "/audit": ["Audit log"], "/system": ["System status"],
        "/account": ["My account"], "/account/password": ["My account", "Change password"], "/batches/new": ["Batches", "New batch"],
        f"/batches/{bid}": ["Batches", "November 2026"], f"/batches/{bid}/confirm": ["Batches", "November 2026", "Confirm"],
        f"/batches/{bid}/send": ["Batches", "November 2026", "Send"],
    }
    for path, labels in pages.items():
        html = staff["admin"].get(path).text
        nav = re.search(r'<nav class="crumbs" aria-label="Breadcrumb">(.*?)</nav>', html, re.S)
        assert nav, path
        assert re.findall(r"<(?:a [^>]*|span aria-current=\"page\")>([^<]+)</", nav.group(1)) == labels, path
        assert nav.group(1).count('aria-current="page"') == 1 and nav.group(1).rstrip().endswith("</ol>")
    send_now(staff["hr"], bid)
    assert 'aria-label="Breadcrumb"' in staff["admin"].get(f"/batches/{bid}").text                 # the progress page too


def test_breadcrumb_links_lead_somewhere_real(staff, db):
    bid = ready_batch(staff["hr"], db)
    html = staff["admin"].get(f"/batches/{bid}/confirm").text
    for href in re.findall(r'<nav class="crumbs".*?</nav>', html, re.S)[0].split('href="')[1:]:
        assert staff["admin"].get(href.split('"')[0]).status_code == 200


# ---- toasts and dialogs ---------------------------------------------------------------------------------------------

def test_messages_are_toasts_in_a_live_region_outside_the_page_content(staff, db):
    from app.models import User
    from sqlalchemy import select
    viewer = db.scalar(select(User).where(User.username == "the.viewer"))
    staff["admin"].post(f"/users/{viewer.id}/disable", data={"csrf_token": token(staff["admin"])}, follow_redirects=False)
    html = staff["admin"].get("/users").text
    region = re.search(r'<div class="toasts" role="region" aria-label="Notifications">(.*?)</div>\s*<dialog', html, re.S)
    assert region and 'class="flash flash-success" role="status"' in region.group(1) and 'aria-label="Dismiss this message"' in region.group(1)
    assert "flash" not in html.split("<main")[1].split("</main>")[0]                                # not a banner at the top of the page


def test_error_toasts_use_role_alert_and_stay_until_dismissed(staff, db, admin):
    staff["admin"].post(f"/users/{admin.id}/disable", data={"csrf_token": token(staff["admin"])}, follow_redirects=False)
    html = staff["admin"].get("/users").text
    assert 'class="flash flash-error" role="alert"' in html
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert 'classList.contains("flash-error")' in js and "7000" in js                                # errors are skipped by the auto-dismiss timer


def test_there_is_one_accessible_confirm_dialog_and_risky_buttons_use_it(staff, db):
    html = staff["admin"].get("/users").text
    dialog = re.search(r'<dialog class="dialog" id="confirm-dialog" role="alertdialog" aria-labelledby="confirm-title" aria-describedby="confirm-text">(.*?)</dialog>', html, re.S)
    assert dialog and 'id="confirm-title"' in dialog.group(1) and 'method="dialog"' in dialog.group(1) and 'value="cancel"' in dialog.group(1) and "data-confirm-ok" in dialog.group(1)
    assert html.count("data-confirm=") >= 3 and 'data-confirm-title="Reset password?"' in html and 'data-confirm-title="Disable this user?"' in html
    bid = ready_batch(staff["hr"], db)
    assert 'data-confirm-title="Delete this batch?"' in staff["admin"].get(f"/batches/{bid}").text
    from tests.conftest import make_employee
    make_employee(db, "Some", "Body", "+26770001122", "1111")
    assert 'data-confirm-title="Deactivate Some Body?"' in staff["admin"].get("/employees").text


def test_without_scripts_the_risky_buttons_still_submit_normally(staff, db):
    """data-confirm is only an attribute: the button is a normal submit button inside its form (no JS needed to work)."""
    html = staff["admin"].get("/users").text
    for button in re.findall(r"<button[^>]*data-confirm=[^>]*>", html):
        assert 'type="submit"' in button
    assert "needs-js" not in "".join(re.findall(r"<button[^>]*data-confirm=[^>]*>", html))


def test_app_js_has_keyboard_handling_for_menus_dialogs_and_reduced_motion():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert "ArrowDown" in js and "ArrowUp" in js and 'key === "Escape"' in js and "showModal" in js and "prefers-reduced-motion" in js
    assert "requestSubmit" in js and "focus()" in js
    assert js.count("fetch(") == 1 and js.isascii()


# ---- accessibility and mobile hooks -----------------------------------------------------------------------------------

@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_scrollable_table_can_be_focused_and_is_named(template):
    for tag in re.findall(r'<div class="[^"]*scroll-sm[^"]*"[^>]*>', template.read_text(encoding="utf-8")):
        assert 'role="region"' in tag and "aria-label=" in tag and 'tabindex="0"' in tag, tag


def test_css_supports_reduced_motion_visible_focus_and_sticky_first_columns():
    assert "@media (prefers-reduced-motion: reduce)" in CSS and "animation-duration: .001ms" in CSS and "transition-duration: .001ms" in CSS
    assert ":focus-visible" in CSS and ".scroll-sm:focus-visible" in CSS
    mobile = CSS[CSS.index("max-width: 900px", CSS.index("Users, audit, status")):]
    assert "table.data td:first-child" in mobile and "position: sticky" in mobile and "left: 0" in mobile


def test_tables_have_captions_and_column_scopes_on_the_new_pages(staff, db):
    for path in ("/users", "/audit", "/account"):
        html = staff["admin"].get(path).text
        if "<table" in html:
            assert '<caption class="visually-hidden">' in html and 'scope="col"' in html, path


def test_the_mode_pill_is_in_the_header_on_every_page_and_names_the_mode(staff, monkeypatch):
    from app.config import get_settings
    for path in ("/", "/employees", "/batches", "/users", "/audit", "/system", "/account"):
        assert re.search(r'class="mode-pill mode-pill-mock" role="status" aria-label="Mock mode: nothing is really sent">MOCK</span>', staff["admin"].get(path).text), path
    monkeypatch.setattr(get_settings(), "whatsapp_mode", "live")
    html = staff["admin"].get("/").text
    assert 'class="mode-pill mode-pill-live"' in html and 'aria-label="Live mode: real WhatsApp messages are sent"' in html and ">LIVE</span>" in html


def test_the_delivery_ribbon_has_a_text_alternative_and_a_legend_with_words(staff, db, app):
    from tests.send_helpers import fire_events
    bid = ready_batch(staff["hr"], db)
    send_now(staff["hr"], bid)
    fire_events(app)
    html = staff["admin"].get(f"/batches/{bid}").text
    assert re.search(r'<svg class="ribbon"[^>]*role="img" aria-label="3 of 3 delivered or read"', html) and "data-seg=\"read\"" in html
    listing = staff["admin"].get("/batches").text
    assert "3 of 3 delivered or read" in listing and "delivery-cell" in listing


def test_the_finished_batch_summary_says_what_to_do_next(staff, db, app):
    from tests.conftest import make_employee
    from tests.send_helpers import PEOPLE
    rows = PEOPLE[:1] + [("Rate", "Limited", "+26770000111", "2222"), ("Time", "Out", "+26770000222", "3333")]
    bid = ready_batch(staff["hr"], db, rows)
    send_now(staff["hr"], bid)
    html = staff["admin"].get(f"/batches/{bid}").text
    assert "Sending has finished, but some payslips need a look." in html
    assert "1 failed." in html and "press <em>Retry failed</em>" in html and "1 uncertain." in html and "Ask the employee first" in html and "retrying can deliver it twice" in html
    assert 'data-active="0"' in html and "Finished." in html


def test_progress_page_marks_itself_active_while_sending_so_the_script_can_reload_when_done(staff, db):
    from app.models import Batch
    from tests.send_helpers import payslips
    bid = ready_batch(staff["hr"], db)
    for p in payslips(db, bid):
        p.status = "queued"
    b = db.get(Batch, bid)
    b.status, b.send_state = "sending", "running"
    db.commit()
    html = staff["admin"].get(f"/batches/{bid}").text
    assert 'data-active="1"' in html and "live-dot" in html and "Updating..." in html and 'action="/batches/%d/stop"' % bid in html
    js = (ROOT / "app/static/progress.js").read_text(encoding="utf-8")
    assert 'data-active' in js and "wasActive" in js and "updateRibbon" in js and 'setAttribute("width"' in js
