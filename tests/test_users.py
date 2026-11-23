"""User management: safeguards, temporary passwords, forced change, sessions, lockouts, audit."""
import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, LoginAttempt, User
from app.security.passwords import hash_password, verify_password
from app.services import one_time, users as svc
from app.services.one_time import temp_passwords
from tests.conftest import PASSWORD, csrf_from, do_login
from tests.user_helpers import audit, login_as, make_user, post, staff, temp_password_from, token  # noqa: F401


def create_via_page(admin_client, username="new.hr", full_name="New Person", role="hr", follow=True):
    return admin_client.post("/users/new", data={"csrf_token": token(admin_client), "username": username, "full_name": full_name, "role": role},
                             follow_redirects=follow)


def get_user(db, username):
    db.expire_all()
    return db.scalar(select(User).where(User.username == username))


# ---- adding a user and the temporary password --------------------------------------------------------------------

def test_admin_adds_a_user_and_the_temporary_password_is_shown_once(staff, db):
    r = create_via_page(staff["admin"], follow=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/users/temp-password?t=")
    page = staff["admin"].get(r.headers["location"])
    temp = temp_password_from(page.text)
    assert page.headers["cache-control"].startswith("no-store") and "shown only once" in page.text and "new.hr" in page.text
    assert re.fullmatch(r"[A-Za-z2-9]{4}(-[A-Za-z2-9]{4}){3}", temp) and not set(temp) & set("0O1lI")
    # a refresh (or opening the link again) finds nothing: the password cannot be shown a second time
    again = staff["admin"].get(r.headers["location"], follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/users"
    assert "already shown or has expired" in staff["admin"].get("/users").text
    user = get_user(db, "new.hr")
    assert (user.role, user.full_name, user.is_active, user.must_change_password) == ("hr", "New Person", True, True)


def test_the_temporary_password_is_never_stored_in_plain_text_or_logged(staff, db, caplog):
    caplog.set_level(logging.DEBUG)
    r = create_via_page(staff["admin"], follow=False)
    temp = temp_password_from(staff["admin"].get(r.headers["location"]).text)
    user = get_user(db, "new.hr")
    assert user.password_hash.startswith("$argon2id$") and temp not in user.password_hash and verify_password(user.password_hash, temp)
    audit_text = " ".join(f"{a.action} {a.details} {a.entity_type} {a.ip_address}" for a in audit(db))
    assert temp not in audit_text and temp.replace("-", "") not in audit_text
    assert temp not in caplog.text and temp.replace("-", "") not in caplog.text
    cookie = staff["admin"].cookies.get("payslip_session") or ""
    assert temp not in cookie                                              # not in the (visible, signed) session cookie either
    assert temp_passwords._items == {}                                     # and gone from memory once shown
    for row in db.execute(select(User.username, User.full_name, User.password_hash)):
        assert temp not in " ".join(map(str, row))                            # nowhere in the users table as readable text


def test_only_the_admin_it_was_made_for_can_open_the_link_and_it_expires(app, staff, db, monkeypatch):
    make_user(db, "other.admin", "admin")
    other_admin = login_as(app, "other.admin")
    r = create_via_page(staff["admin"], follow=False)
    link = r.headers["location"]
    assert staff["hr"].get(link).status_code == 403                      # not an admin at all
    stolen = other_admin.get(link, follow_redirects=False)               # an admin, but not the one it was made for
    assert stolen.status_code == 303 and stolen.headers["location"] == "/users" and "<code" not in stolen.text
    assert staff["admin"].get(link).status_code == 200                   # the owner can still open it (once)
    r2 = create_via_page(staff["admin"], "second.user", "Second", "viewer", follow=False)
    now = one_time._now()
    monkeypatch.setattr(one_time, "_now", lambda: now + one_time.TTL_SECONDS + 1)
    gone = staff["admin"].get(r2.headers["location"], follow_redirects=False)
    assert gone.status_code == 303 and gone.headers["location"] == "/users"        # expired: nothing to show


def test_the_one_time_store_hands_a_secret_out_once_and_only_to_its_owner():
    store = one_time.OneTimeStore()
    token_ = store.put(7, {"password": "x"})
    assert store.pop(token_, 8) is None and store.pop("nonsense", 7) is None and store.pop(None, 7) is None
    assert store.pop(token_, 7) == {"password": "x"} and store.pop(token_, 7) is None


def test_new_user_rules_unique_case_insensitive_valid_name_and_role(staff, db):
    create_via_page(staff["admin"])
    for username, full, role, message in [
        ("NEW.HR", "Someone", "hr", "already taken"), ("new.hr", "Someone", "hr", "already taken"),
        ("ab", "Short", "hr", "3-150 characters"), ("bad name!", "Bad", "hr", "3-150 characters"),
        ("fine.name", "", "hr", "full name"), ("fine.name", "Ok", "boss", "Choose a role"),
    ]:
        r = create_via_page(staff["admin"], username, full, role, follow=False)
        assert r.status_code == 422 and message in r.text, (username, role)
    assert db.query(User).filter(User.username.in_(["fine.name", "ab"])).count() == 0


def test_usernames_are_stored_lower_case(staff, db):
    create_via_page(staff["admin"], "  Mixed.Case ", "Mixed Case", "viewer")
    assert get_user(db, "mixed.case") is not None and get_user(db, "Mixed.Case") is None


# ---- forced password change --------------------------------------------------------------------------------------

def first_login(app, db, username="new.hr"):
    admin_client = login_as(app, "hr.admin")
    r = create_via_page(admin_client, username, "New Person", "hr", follow=False)
    temp = temp_password_from(admin_client.get(r.headers["location"]).text)
    fresh = TestClient(app)
    response = do_login(fresh, username, temp)
    return fresh, temp, response


def test_a_new_user_must_change_the_temporary_password_before_anything_else(app, admin, db):
    client, temp, response = first_login(app, db)
    assert response.status_code == 303 and response.headers["location"] == "/account/password"
    for path in ("/", "/employees", "/batches", "/account"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/account/password", path
    posted = client.post("/employees/new", data={"csrf_token": csrf_from(client.get("/account/password").text), "first_name": "A", "surname": "B"}, follow_redirects=False)
    assert posted.status_code == 303 and posted.headers["location"] == "/account/password"
    page = client.get("/account/password").text
    assert "Choose your own password" in page and "Until you choose a new password" in page and "Temporary password" in page
    assert client.get("/static/style.css").status_code == 200                      # the page itself still has its styles


def change(client, current, new, confirm=None):
    return client.post("/account/password", data={"csrf_token": csrf_from(client.get("/account/password").text), "current_password": current,
                                                  "new_password": new, "confirm_password": new if confirm is None else confirm}, follow_redirects=False)


def test_the_change_page_enforces_the_same_rules_as_every_password(app, admin, db):
    client, temp, _ = first_login(app, db)
    cases = [
        (temp, "short-pw", None, "at least 12 characters"),
        (temp, "a-new-password-1", "a-different-one-2", "do not match"),
        (temp, temp, None, "different from your current password"),
        ("not-the-temp-password", "a-new-password-1", None, "current password is not correct"),
        (temp, "new.hr-new.hr-new", None, "must not contain the username"),
        (temp, "aaaaaaaaaaaaaaaa", None, "too repetitive"),
    ]
    for current, new, confirm, message in cases:
        r = change(client, current, new, confirm)
        assert r.status_code == 422 and message in r.text, (new, message)
        assert get_user(db, "new.hr").must_change_password is True


def test_changing_the_password_clears_the_flag_ends_other_sessions_and_keeps_this_one(app, admin, db):
    client, temp, _ = first_login(app, db)
    other_browser = TestClient(app)
    do_login(other_browser, "new.hr", temp)                                          # a second session with the temporary password
    r = change(client, temp, "my-own-long-password-9")
    assert r.status_code == 303 and r.headers["location"] == "/"
    user = get_user(db, "new.hr")
    assert user.must_change_password is False and user.password_changed_at is not None and verify_password(user.password_hash, "my-own-long-password-9")
    assert client.get("/", follow_redirects=False).status_code == 200                # this browser carries on
    assert other_browser.get("/", follow_redirects=False).status_code == 303          # the other session ended
    assert TestClient(app).post("/login", data={"csrf_token": csrf_from(TestClient(app).get("/login").text), "username": "new.hr", "password": temp}).status_code in (401, 403)
    assert [a.action for a in audit(db, "password_changed")] == ["password_changed"]


def test_after_the_change_a_normal_login_goes_straight_to_the_dashboard(app, admin, db):
    client, temp, _ = first_login(app, db)
    change(client, temp, "my-own-long-password-9")
    again = TestClient(app)
    assert do_login(again, "new.hr", "my-own-long-password-9").headers["location"] == "/"


def test_wrong_current_password_attempts_are_rate_limited(app, admin, db):
    client, temp, _ = first_login(app, db)
    for _ in range(5):
        change(client, "wrong-wrong-wrong-1", "a-new-password-1")
    r = change(client, temp, "a-new-password-1")                                     # even the right one is refused while locked
    assert r.status_code == 422 and "Too many wrong attempts" in r.text and get_user(db, "new.hr").must_change_password is True


def test_any_user_can_change_their_own_password_from_my_account(app, staff, db):
    hr = staff["hr"]
    page = hr.get("/account").text
    assert "Change password" in page and "Recent logins" in page and "The Hr" in page
    r = change(hr, PASSWORD, "another-long-password-5")
    assert r.status_code == 303 and verify_password(get_user(db, "the.hr").password_hash, "another-long-password-5")


def test_my_account_shows_only_my_own_recent_logins_with_masked_addresses(app, staff, db):
    login_as(app, "the.hr")
    login_as(app, "the.hr")
    page = staff["hr"].get("/account").text
    rows = re.findall(r'<td class="num nowrap">(\d\d \w{3} \d{4} \d\d:\d\d)', page)
    assert len(rows) >= 3 and "(this session)" in page and ("unknown" in page or ".x" in page)
    assert "hr.admin" not in page and "the.viewer" not in page


# ---- the safeguards ----------------------------------------------------------------------------------------------

def test_an_admin_cannot_disable_or_demote_themselves(staff, db, admin):
    me = get_user(db, "hr.admin")
    other_admin = make_user(db, "second.admin", "admin")                             # so the "last admin" rule is NOT what stops this
    r = post(staff["admin"], f"/users/{me.id}/disable")
    assert get_user(db, "hr.admin").is_active is True and "cannot disable your own account" in staff["admin"].get("/users").text
    r = staff["admin"].post(f"/users/{me.id}/edit", data={"csrf_token": token(staff["admin"]), "full_name": "Me", "role": "viewer"})
    assert r.status_code == 422 and "cannot change your own role" in r.text and get_user(db, "hr.admin").role == "admin"
    # but they CAN change their own name
    ok = staff["admin"].post(f"/users/{me.id}/edit", data={"csrf_token": token(staff["admin"]), "full_name": "Renamed Admin", "role": "admin"}, follow_redirects=False)
    assert ok.status_code == 303 and get_user(db, "hr.admin").full_name == "Renamed Admin"


def test_the_last_active_admin_cannot_be_disabled_or_demoted_whoever_asks(db, admin):
    hr = make_user(db, "the.hr", "hr")                                               # (a non-admin actor: this tests the rule, not the route)
    with pytest.raises(svc.UserError, match="last active admin"):
        svc.set_active(db, hr, admin, False)
    with pytest.raises(svc.UserError, match="last active admin"):
        svc.update_user(db, hr, admin, admin.full_name or "Admin", "hr")
    db.expire_all()
    assert admin.is_active is True and admin.role == "admin"


def test_with_two_admins_one_may_demote_or_disable_the_other_but_not_the_last(staff, db, admin):
    second = make_user(db, "second.admin", "admin")
    me = staff["admin"]
    assert post(me, f"/users/{second.id}/disable").status_code == 303 and get_user(db, "second.admin").is_active is False
    # now only one active admin is left: it cannot be disabled by anyone; re-enable the second one
    assert post(me, f"/users/{second.id}/enable").status_code == 303 and get_user(db, "second.admin").is_active is True
    r = me.post(f"/users/{second.id}/edit", data={"csrf_token": token(me), "full_name": "Second", "role": "hr"}, follow_redirects=False)
    assert r.status_code == 303 and get_user(db, "second.admin").role == "hr"
    assert svc.active_admin_count(db) == 1


def test_disabled_admins_do_not_count_as_active_admins(db, admin):
    ghost = make_user(db, "ghost.admin", "admin", active=False)
    assert svc.active_admin_count(db) == 1 and svc.active_admin_count(db, excluding_id=admin.id) == 0
    hr = make_user(db, "the.hr", "hr")
    with pytest.raises(svc.UserError):
        svc.set_active(db, hr, admin, False)


def test_a_disabled_user_cannot_log_in_and_an_open_session_ends(app, staff, db):
    hr_client = staff["hr"]
    hr = get_user(db, "the.hr")
    post(staff["admin"], f"/users/{hr.id}/disable")
    assert hr_client.get("/", follow_redirects=False).status_code == 303 and hr_client.get("/", follow_redirects=False).headers["location"] == "/login"
    fresh = TestClient(app)
    assert do_login(fresh, "the.hr").status_code == 401
    post(staff["admin"], f"/users/{hr.id}/enable")
    assert do_login(TestClient(app), "the.hr").status_code == 303


# ---- sign out everywhere / role changes / password reset -------------------------------------------------------

def test_sign_out_everywhere_ends_every_session_of_that_user_only(app, staff, db):
    hr = get_user(db, "the.hr")
    second_browser = login_as(app, "the.hr")
    r = post(staff["admin"], f"/users/{hr.id}/signout")
    assert r.status_code == 303
    for client in (staff["hr"], second_browser):
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert staff["viewer"].get("/", follow_redirects=False).status_code == 200 and staff["admin"].get("/", follow_redirects=False).status_code == 200
    assert do_login(TestClient(app), "the.hr").status_code == 303                    # logging in again works: it is a sign-out, not a lock
    assert [a.entity_id for a in audit(db, "user_sessions_revoked")] == [hr.id]


def test_changing_a_role_signs_that_user_out_so_the_new_role_applies_at_once(app, staff, db):
    hr = get_user(db, "the.hr")
    r = staff["admin"].post(f"/users/{hr.id}/edit", data={"csrf_token": token(staff["admin"]), "full_name": "The Hr", "role": "viewer"}, follow_redirects=False)
    assert r.status_code == 303 and get_user(db, "the.hr").role == "viewer"
    assert staff["hr"].get("/", follow_redirects=False).headers["location"] == "/login"
    assert login_as(app, "the.hr").get("/batches/new").status_code == 403           # now a viewer


def test_reset_password_shows_a_new_temporary_password_once_and_ends_the_old_one(app, staff, db):
    hr = get_user(db, "the.hr")
    r = post(staff["admin"], f"/users/{hr.id}/reset-password")
    assert r.headers["location"].startswith("/users/temp-password?t=")
    temp = temp_password_from(staff["admin"].get(r.headers["location"]).text)
    assert staff["admin"].get(r.headers["location"], follow_redirects=False).headers["location"] == "/users"
    assert staff["hr"].get("/", follow_redirects=False).headers["location"] == "/login"           # their session ended
    assert do_login(TestClient(app), "the.hr", PASSWORD).status_code == 401                        # the old password is dead
    client = TestClient(app)
    assert do_login(client, "the.hr", temp).headers["location"] == "/account/password"             # the new one forces a change
    user = get_user(db, "the.hr")
    assert user.must_change_password is True and temp not in user.password_hash
    assert audit(db, "user_password_reset")[-1].entity_id == hr.id


def test_unlock_clears_a_lockout_and_the_users_page_shows_it(app, staff, db):
    anonymous = TestClient(app)
    for _ in range(5):
        do_login(anonymous, "the.viewer", "wrong-password-1")
    assert do_login(TestClient(app), "the.viewer", PASSWORD).status_code == 429          # right password, but the account is locked
    page = staff["admin"].get("/users").text
    assert page.count("Locked out") == 1
    viewer = get_user(db, "the.viewer")
    assert f'action="/users/{viewer.id}/unlock"' in page
    post(staff["admin"], f"/users/{viewer.id}/unlock")
    assert "Locked out" not in staff["admin"].get("/users").text
    assert do_login(TestClient(app), "the.viewer", PASSWORD).status_code == 303
    assert audit(db, "user_unlocked")[-1].entity_id == viewer.id


# ---- users list ---------------------------------------------------------------------------------------------------

def test_users_page_lists_everyone_with_role_status_last_login_and_search(app, staff, db):
    make_user(db, "gone.user", "viewer", active=False, full_name="Gone Person")
    page = staff["admin"].get("/users").text
    for text in ("hr.admin", "the.hr", "the.viewer", "gone.user", "Disabled", "(you)", "Admin", "HR", "Viewer"):
        assert text in page, text
    assert re.search(r"\d\d \w{3} \d{4} \d\d:\d\d", page)                                # somebody has a last login time
    found = staff["admin"].get("/users", params={"q": "gone"}).text
    assert "gone.user" in found and "the.hr" not in found
    assert "No users match" in staff["admin"].get("/users", params={"q": "zzzz"}).text
    assert "the.hr" in staff["admin"].get("/users", params={"q": "HR"}).text            # searches the role too


def test_a_user_flagged_for_a_new_password_is_labelled(staff, db):
    create_via_page(staff["admin"])
    assert "Temporary password" in staff["admin"].get("/users").text


def test_the_users_page_never_shows_a_password_or_hash(staff, db):
    page = staff["admin"].get("/users").text
    assert "argon2" not in page and PASSWORD not in page


# ---- audit of every user action ---------------------------------------------------------------------------------

def test_every_user_action_is_audited_with_actor_and_target_and_no_password(app, staff, db, admin):
    me = staff["admin"]
    r = create_via_page(me, "audited.user", "Audited User", "viewer", follow=False)
    temp = temp_password_from(me.get(r.headers["location"]).text)
    target = get_user(db, "audited.user")
    me.post(f"/users/{target.id}/edit", data={"csrf_token": token(me), "full_name": "Audited Renamed", "role": "hr"})
    post(me, f"/users/{target.id}/reset-password")
    post(me, f"/users/{target.id}/unlock")
    post(me, f"/users/{target.id}/signout")
    post(me, f"/users/{target.id}/disable")
    post(me, f"/users/{target.id}/enable")
    rows = [a for a in audit(db) if a.entity_type == "user" and a.entity_id == target.id]
    assert [a.action for a in rows] == ["user_created", "user_updated", "user_password_reset", "user_unlocked", "user_sessions_revoked", "user_disabled", "user_enabled"]
    assert all(a.user_id == admin.id for a in rows)                                  # the ACTING user
    blob = " ".join(f"{a.details}" for a in rows)
    assert temp not in blob and "argon2" not in blob and "role=viewer" in blob and "role viewer->hr" in blob and "name" in blob


def test_failed_actions_are_not_audited_as_done(staff, db, admin):
    me = staff["admin"]
    before = len(audit(db))
    post(me, f"/users/{admin.id}/disable")                                           # refused: it is yourself
    assert [a.action for a in audit(db)][before:] == []


# ---- the command-line emergency path -----------------------------------------------------------------------------

def test_create_admin_cli_function_makes_an_admin_by_default_and_other_roles_on_request(db):
    from app.services.admins import AdminError, create_admin
    a = create_admin(db, "cli.admin", "a-good-password-123")
    h = create_admin(db, "cli.hr", "a-good-password-123", role="hr", full_name="Cli  Hr", must_change=True)
    assert (a.role, a.must_change_password) == ("admin", False) and (h.role, h.full_name, h.must_change_password) == ("hr", "Cli Hr", True)
    with pytest.raises(AdminError, match="Role"):
        create_admin(db, "cli.bad", "a-good-password-123", role="root")
    with pytest.raises(AdminError, match="already exists"):
        create_admin(db, "CLI.ADMIN", "a-good-password-123")
    details = [x.details for x in audit(db, "admin_created")]
    assert "admin 'cli.admin' created via command line" in details and "hr 'cli.hr' created via command line" in details


def test_reset_password_cli_function_can_enable_force_a_change_and_ends_sessions(db, admin):
    from app.services.admins import reset_password
    admin.is_active = False
    version = admin.session_version
    db.commit()
    user = reset_password(db, "HR.ADMIN", "a-brand-new-password-77", must_change=True, enable=True)
    assert user.is_active is True and user.must_change_password is True and user.session_version == version + 1
    assert verify_password(user.password_hash, "a-brand-new-password-77")
    plain = reset_password(db, "hr.admin", "another-new-password-88")
    assert plain.must_change_password is False and plain.password_changed_at is not None and plain.is_active is True


def test_the_cli_scripts_parse_their_options_and_use_getpass(monkeypatch, db_factory, capsys):
    from app.scripts import create_admin as create_script, reset_admin_password as reset_script
    monkeypatch.setattr(create_script, "SessionLocal", db_factory)
    monkeypatch.setattr(reset_script, "SessionLocal", db_factory)
    answers = iter(["cli.user"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-good-password-123")
    assert create_script.main(["--role", "viewer", "--name", "Cli User", "--must-change"]) == 0
    assert "Viewer 'cli.user' created" in capsys.readouterr().out
    answers = iter(["cli.user"])
    assert reset_script.main(["--enable"]) == 0
    assert "enabled" in capsys.readouterr().out
    with db_factory() as s:
        u = s.scalar(select(User).where(User.username == "cli.user"))
        assert u.role == "viewer" and u.full_name == "Cli User"


# ---- the migration keeps users ------------------------------------------------------------------------------------

def test_the_roles_migration_keeps_every_existing_user_as_an_admin(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from app.config import PROJECT_ROOT, get_settings

    url = f"sqlite:///{(tmp_path / 'migration5.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "e2a6d5b81f39")
        with engine.begin() as c:
            c.execute(text("INSERT INTO users (username, password_hash, is_active, created_at, updated_at) VALUES ('old.admin', 'hash1', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
            c.execute(text("INSERT INTO users (username, password_hash, is_active, created_at, updated_at) VALUES ('old.second', 'hash2', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
        command.upgrade(cfg, "f7c3a91d2e58")
        cols = {c["name"] for c in inspect(engine).get_columns("users")}
        assert {"full_name", "role", "must_change_password", "password_changed_at", "session_version"} <= cols and "system_state" in inspect(engine).get_table_names()
        with engine.connect() as c:
            rows = c.execute(text("SELECT username, role, must_change_password, session_version, full_name, is_active, password_hash FROM users ORDER BY id")).all()
        assert rows == [("old.admin", "admin", 0, 0, "", 1, "hash1"), ("old.second", "admin", 0, 0, "", 0, "hash2")]     # kept, and admins
        command.downgrade(cfg, "e2a6d5b81f39")
        with engine.connect() as c:
            assert c.execute(text("SELECT username FROM users ORDER BY id")).all() == [("old.admin",), ("old.second",)]
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
        monkeypatch.undo()
        get_settings.cache_clear()
