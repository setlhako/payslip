"""Roles and permissions: every role against every route, enforced on the SERVER, plus what each role is shown."""
import re

import pytest

from app.security.permissions import PERMISSIONS, ROLES, can, require
from tests.batch_helpers import create
from tests.conftest import csrf_from, make_employee
from tests.pdfs import named_pdf
from tests.send_helpers import ready_batch, send_now
from tests.test_auth import _api_routes
from tests.user_helpers import login_as, make_user, staff, token  # noqa: F401  (staff is a fixture)

PUBLIC = {("/health", "GET"), ("/login", "GET"), ("/login", "POST"), ("/webhook/whatsapp", "GET"), ("/webhook/whatsapp", "POST")}


def permission_of(dependant) -> str | None:
    for dep in dependant.dependencies:
        permission = getattr(dep.call, "permission", None)
        if permission:
            return permission
        found = permission_of(dep)
        if found:
            return found
    return None


def route_table(app):
    table = []
    for route in _api_routes(app.routes):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if (route.path, method) not in PUBLIC:
                table.append((route.path, method, permission_of(route.dependant)))
    return table


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "999999", path)


# ---- the permission table itself ---------------------------------------------------------------------------------

def test_every_permission_names_only_real_roles_and_admin_has_all():
    for permission, roles in PERMISSIONS.items():
        assert roles <= set(ROLES) and "admin" in roles, permission


def test_can_is_false_for_nobody_unknown_permissions_and_unknown_roles():
    from types import SimpleNamespace as U
    assert can(None, "dashboard.view") is False
    assert can(U(role="admin"), "does.not.exist") is False
    assert can(U(role="superuser"), "dashboard.view") is False
    assert can(U(role="viewer"), "employees.edit") is False and can(U(role="hr"), "employees.edit") is True


def test_require_refuses_unknown_permissions_at_startup():
    with pytest.raises(ValueError):
        require("not.a.permission")


def test_the_documented_split_between_roles():
    expected = {
        "admin": set(PERMISSIONS),
        "hr": {"dashboard.view", "account.self", "employees.view", "employees.edit", "ids.reveal", "batches.view", "batches.manage", "batches.send"},
        "viewer": {"dashboard.view", "account.self", "employees.view", "batches.view"},
    }
    for role, permissions in expected.items():
        assert {p for p, roles in PERMISSIONS.items() if role in roles} == permissions, role


# ---- every route declares a permission (a new route without one fails here) -----------------------------------------

def test_every_protected_route_declares_a_permission(app):
    table = route_table(app)
    assert len(table) > 50
    missing = [(m, p) for p, m, perm in table if perm is None]
    assert missing == [], f"routes without a permission: {missing}"
    assert {perm for _, _, perm in table} <= set(PERMISSIONS)
    assert {perm for _, _, perm in table} == set(PERMISSIONS)                 # and every permission is used by some route


# ---- every role against every route --------------------------------------------------------------------------------

SKIP = {("/logout", "POST")}                          # would end the session the matrix needs (covered by test_logout_is_open_to_every_role)


def test_every_role_against_every_route(app, staff):
    """Denied roles get 403. Allowed roles get anything else (200, a redirect, 404 for the made-up id, ...)."""
    wrong = []
    checked = 0
    for path, method, permission in route_table(app):
        if (path, method) in SKIP:
            continue
        for role in ROLES:
            client = staff[role]
            url = concrete(path)
            if method == "GET":
                response = client.get(url, follow_redirects=False)
            else:
                response = client.post(url, data={"csrf_token": token(client)}, follow_redirects=False)
            allowed = role in PERMISSIONS[permission]
            checked += 1
            if allowed and response.status_code == 403:
                wrong.append((role, method, path, "should be ALLOWED but got 403"))
            if not allowed and response.status_code != 403:
                wrong.append((role, method, path, f"should be 403 but got {response.status_code}"))
    assert checked > 150
    assert wrong == [], wrong


def test_logout_is_open_to_every_role(app, staff):
    for role in ROLES:
        client = login_as(app, {"admin": "hr.admin", "hr": "the.hr", "viewer": "the.viewer"}[role])
        assert client.post("/logout", data={"csrf_token": token(client)}, follow_redirects=False).headers["location"] == "/login"


def test_a_logged_out_visitor_gets_a_login_redirect_not_a_403_everywhere(app):
    from fastapi.testclient import TestClient
    anonymous = TestClient(app)
    for path, method, _ in route_table(app):
        url = concrete(path)
        r = anonymous.get(url, follow_redirects=False) if method == "GET" else anonymous.post(url, data={"csrf_token": "x"}, follow_redirects=False)
        assert r.status_code in (303, 403) and (r.status_code == 403 or r.headers["location"] == "/login"), (method, path, r.status_code)


def test_a_denied_request_is_audited_without_any_personal_data(app, staff, db):
    from tests.user_helpers import audit
    staff["viewer"].get("/users")
    rows = audit(db, "permission_denied")
    assert rows and rows[-1].details == "permission=users.manage method=GET" and rows[-1].user_id is not None


def test_the_403_page_is_plain_and_does_not_reveal_anything(staff):
    r = staff["viewer"].get("/audit")
    assert r.status_code == 403 and "Your role does not allow you to do that." in r.text and "<td" not in r.text         # no audit rows leak into the error page


# ---- what each role is SHOWN ---------------------------------------------------------------------------------------

def employee_with_id(db):
    return make_employee(db, "Thabo", "Molefe", "+26771234567", "6789", "E1", id_number="987654321")


def test_viewer_employee_list_has_no_id_digits_no_password_column_no_reveal_and_no_edit(staff, db):
    e = employee_with_id(db)
    make_employee(db, "Old", "Record", "+26770001122", "4321")                       # only a last 4 saved
    html = staff["viewer"].get("/employees?status=all").text
    assert "Thabo Molefe" in html and "Old Record" in html
    for forbidden in ("data-id-slot", "data-secret", "data-toggle-ids", "reveal-ids", "data-reveal-form", "Show IDs", "Password (last 4)",
                      "6789", "4321", "987654321", "full ID not imported", "/edit", "Import", "Add employee", "Deactivate"):
        assert forbidden not in html, forbidden
    assert "ID numbers are always hidden for your role" in html and html.count("•" * 9) >= 2


def test_hr_and_admin_employee_list_has_the_reveal_controls(staff, db):
    employee_with_id(db)
    for role in ("hr", "admin"):
        html = staff[role].get("/employees").text
        assert "data-toggle-ids" in html and "data-reveal-form" in html and "Password (last 4)" in html and "Add employee" in html and "/edit" in html


def test_viewer_cannot_reveal_ids_by_any_route_and_nothing_is_audited_as_revealed(staff, db):
    from tests.user_helpers import audit
    e = employee_with_id(db)
    for data in ({"employee_id": str(e.id)}, {"status": "all"}, {}):
        r = staff["viewer"].post("/employees/reveal-ids", data={"csrf_token": token(staff["viewer"]), **data})
        assert r.status_code == 403 and "987654321" not in r.text
    assert audit(db, "ids_revealed") == []
    assert staff["hr"].post("/employees/reveal-ids", data={"csrf_token": token(staff["hr"]), "employee_id": str(e.id)}).json() == {"ids": {str(e.id): "987654321"}}


def test_viewer_cannot_open_or_post_the_employee_forms_or_import(staff, db):
    e = employee_with_id(db)
    v = staff["viewer"]
    assert v.get(f"/employees/{e.id}/edit").status_code == 403 and v.get("/employees/new").status_code == 403 and v.get("/employees/import").status_code == 403
    for path in (f"/employees/{e.id}/deactivate", f"/employees/{e.id}/edit", "/employees/new", "/employees/import/confirm"):
        assert v.post(path, data={"csrf_token": token(v)}).status_code == 403
    db.expire_all()
    assert db.get(type(e), e.id).active is True


def test_viewer_sees_batches_and_progress_but_none_of_the_buttons(staff, db):
    bid = ready_batch(staff["hr"], db)
    v = staff["viewer"]
    html = v.get(f"/batches/{bid}").text
    for forbidden in ("Change match", "Add more files", "Cancel and delete", "/remove", "Send payslips", "action=\"/batches/%d/ready\"" % bid):
        assert forbidden not in html, forbidden
    assert "Review and confirm" in html and "Thabo_Molefe.pdf" in html
    confirm = v.get(f"/batches/{bid}/confirm").text
    assert "Mark as ready to send" not in confirm and "I have checked the matches" not in confirm and "Reopen" not in confirm
    assert v.get("/batches").status_code == 200 and "New batch" not in v.get("/batches").text
    send_now(staff["hr"], bid)
    progress = v.get(f"/batches/{bid}").text
    assert "data-status-url" in progress and "Download send report" in progress
    for control in ("/stop", "/resume", "/retry", 'name="payslip_ids"', "retry-uncertain-form"):
        assert control not in progress, control
    assert "Only HR and admins can stop, resume or retry" in progress
    assert v.get(f"/batches/{bid}/status.json").status_code == 200 and v.get(f"/batches/{bid}/report.csv").status_code == 200


def test_viewer_cannot_change_or_send_anything_in_a_batch(staff, db):
    from tests.batch_helpers import by_name, payslips
    bid = ready_batch(staff["hr"], db)
    pid = payslips(db, bid)[0].id
    v = staff["viewer"]
    for path in (f"/batches/{bid}/ready", f"/batches/{bid}/reopen", f"/batches/{bid}/delete", f"/batches/{bid}/rematch", f"/batches/{bid}/upload",
                 f"/batches/{bid}/payslips/{pid}/remove", f"/batches/{bid}/payslips/{pid}/match", f"/batches/{bid}/payslips/{pid}/confirm",
                 f"/batches/{bid}/send", f"/batches/{bid}/stop", f"/batches/{bid}/resume", f"/batches/{bid}/retry-failed", "/batches/new"):
        assert v.post(path, data={"csrf_token": token(v), "confirm_label": "November 2026"}).status_code == 403, path
    assert v.get(f"/batches/{bid}/send").status_code == 403
    db.expire_all()
    from app.models import Batch
    assert db.get(Batch, bid).status == "ready" and len(payslips(db, bid)) == 3


def test_hr_can_do_the_whole_flow_but_not_admin_pages(staff, db):
    bid = ready_batch(staff["hr"], db)
    assert send_now(staff["hr"], bid).status_code == 303
    from tests.send_helpers import batch
    assert batch(db, bid).status == "completed"
    for path in ("/users", "/audit", "/system", "/audit/export.csv", "/users/new"):
        assert staff["hr"].get(path).status_code == 403


def test_the_dashboard_adapts_to_the_role(staff, db):
    admin_html, hr_html, viewer_html = (staff[r].get("/").text for r in ("admin", "hr", "viewer"))
    assert "Recent activity" in admin_html and "Add user" in admin_html and "Audit log" in admin_html
    assert "Recent activity" not in hr_html and "Add user" not in hr_html and "New batch" in hr_html and "Import employees" in hr_html
    assert "New batch" not in viewer_html and "Import employees" not in viewer_html and "read-only access" in viewer_html


def test_the_sidebar_only_lists_what_the_role_can_open(staff):
    def nav(role):
        html = staff[role].get("/").text
        return [name for name, href in (("Users", 'href="/users"'), ("Audit log", 'href="/audit"'), ("System status", 'href="/system"')) if href in html]

    assert nav("admin") == ["Users", "Audit log", "System status"] and nav("hr") == [] and nav("viewer") == []


def test_the_header_always_shows_the_mode_for_every_role(staff):
    for role in ROLES:
        html = staff[role].get("/").text
        assert 'class="mode-pill mode-pill-mock"' in html and ">MOCK</span>" in html
