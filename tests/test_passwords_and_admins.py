import pytest
from sqlalchemy import select

from app.models import AuditLog, User
from app.security.passwords import hash_password, password_problem, verify_password
from app.services.admins import AdminError, create_admin


def test_hash_is_argon2_and_verifies():
    h = hash_password("a-long-enough-password")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a-long-enough-password")
    assert not verify_password(h, "wrong")
    assert not verify_password(None, "anything")  # unknown user


@pytest.mark.parametrize("pw", ["short", "elevenchars", "aaaaaaaaaaaaaaaa", "password1234", "has-hr.admin-inside"])
def test_weak_passwords_refused(pw):
    assert password_problem(pw, "hr.admin")


def test_12_char_password_accepted():
    assert password_problem("twelve-chars", "hr.admin") is None


def test_create_admin_stores_hash_and_audits(db):
    user = create_admin(db, "  Second.Admin ", "a-good-password-123")
    assert user.username == "second.admin"
    assert user.password_hash.startswith("$argon2id$") and "a-good-password-123" not in user.password_hash
    log = db.scalars(select(AuditLog)).one()
    assert log.action == "admin_created" and "a-good-password-123" not in (log.details or "")


def test_create_admin_refuses_duplicates_and_weak(db):
    create_admin(db, "boss", "a-good-password-123")
    with pytest.raises(AdminError):
        create_admin(db, "BOSS", "another-good-password-1")
    with pytest.raises(AdminError):
        create_admin(db, "other", "short")
    assert db.query(User).count() == 1
