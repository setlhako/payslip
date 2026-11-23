"""Shared helpers for the Batches route tests."""
import re

import pytest
from sqlalchemy import select

from app.models import AuditLog, Employee, Payslip
from tests.conftest import csrf_from, make_employee
from tests.pdfs import named_pdf


def token(client):
    return csrf_from(client.get("/").text)


def create(client, files=(), label="November 2026", mode="pdfs", zip_bytes=None):
    multipart = [("files", (name, data, "application/pdf")) for name, data in files]
    if zip_bytes is not None:
        multipart = [("zip_file", ("payslips.zip", zip_bytes, "application/zip"))]
    return client.post("/batches/new", data={"csrf_token": token(client), "pay_month_label": label, "mode": mode},
                       files=multipart or None, follow_redirects=False)


def batch_id_of(response) -> int:
    assert response.status_code == 303, response.text[:300]
    return int(re.search(r"/batches/(\d+)", response.headers["location"]).group(1))


def page(client, batch_id, **params):
    r = client.get(f"/batches/{batch_id}", params=params)
    assert r.status_code == 200
    return r.text


def confirm_page(client, batch_id):
    r = client.get(f"/batches/{batch_id}/confirm")
    assert r.status_code == 200
    return r.text


def post(client, path, **data):
    return client.post(path, data={"csrf_token": token(client), **data}, follow_redirects=False)


def add_more(client, batch_id, files, mode="pdfs"):
    return client.post(f"/batches/{batch_id}/upload", data={"csrf_token": token(client), "mode": mode},
                       files=[("files", (n, d, "application/pdf")) for n, d in files], follow_redirects=False)


def payslips(db, batch_id):
    db.expire_all()
    return list(db.scalars(select(Payslip).where(Payslip.batch_id == batch_id).order_by(Payslip.id)))


def by_name(db, batch_id):
    return {p.original_filename: p for p in payslips(db, batch_id)}


def audit_rows(db, prefix="batch_"):
    db.expire_all()
    return [a for a in db.scalars(select(AuditLog).order_by(AuditLog.id)) if a.action.startswith(prefix)]


def good_batch(client):
    return batch_id_of(create(client, [
        ("Thabo_Molefe.pdf", named_pdf("Thabo", "Molefe")),
        ("dube_neo.pdf", named_pdf("Neo", "Dube")),
        ("Chipo_Fakename.pdf", named_pdf("Chipo", "Fakename")),
    ]))


@pytest.fixture
def people(db):
    return {
        "thabo": make_employee(db, "Thabo", "Molefe", "+26771234567", "6789", "E1"),
        "neo": make_employee(db, "Neo", "Dube", "+26772345678", "4321"),
        "chipo": make_employee(db, "Chipo", "Fakename", "+26773456789", "AB12", id_type="passport"),
        "kea": make_employee(db, "Kea", "Quiet", "+26774567890", "1111"),
    }
