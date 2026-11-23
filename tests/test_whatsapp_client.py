"""PDF protection, the live WhatsApp client (mocked HTTP only), the mock client, error handling and settings."""
import io
import json
import logging

import httpx
import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from app.config import Settings, get_settings
from app.services.pdf_protect import ProtectError, protect_pdf, verify_protected
from app.services.whatsapp import (
    AMBIGUOUS, AUTH, PERMANENT, TRANSIENT, LiveWhatsAppClient, MockWhatsAppClient, WhatsAppConfigError, WhatsAppError,
    build_client, clean_param, error_from_response, payslip_filename,
)
from app.services.whatsapp_errors import clean_error_text, explain
from tests.pdfs import locked_pdf, named_pdf
from tests.send_helpers import PHONE_ID, TOKEN, error_response, live_client, ok_handler


# ---- PDF protection ----------------------------------------------------------------------------------------------

@pytest.fixture
def source(tmp_path):
    path = tmp_path / "in.pdf"
    path.write_bytes(named_pdf("Thabo", "Molefe"))
    return path


def test_protected_copy_is_aes256_opens_only_with_the_exact_password(source):
    data = protect_pdf(source, "AB12")
    reader = PdfReader(io.BytesIO(data))
    assert reader.is_encrypted and reader.trailer["/Encrypt"]["/V"] == 5 and reader.trailer["/Encrypt"]["/R"] == 6   # AES-256
    for wrong in ("", "ab12", "AB1", "AB123", "6789", " AB12"):
        r = PdfReader(io.BytesIO(data))
        assert int(r.decrypt(wrong)) == 0, wrong
        with pytest.raises(Exception):
            len(r.pages)
    right = PdfReader(io.BytesIO(data))
    assert int(right.decrypt("AB12")) != 0 and "Thabo Molefe" in right.pages[0].extract_text()


def test_the_password_is_case_sensitive_for_passports(source):
    data = protect_pdf(source, "CD34")
    assert int(PdfReader(io.BytesIO(data)).decrypt("cd34")) == 0
    assert int(PdfReader(io.BytesIO(data)).decrypt("CD34")) != 0


def test_the_owner_password_is_random_each_time_and_is_not_the_user_password(source):
    a, b = protect_pdf(source, "1234"), protect_pdf(source, "1234")
    assert a != b                                                       # random salts/keys: nothing is predictable
    for data in (a, b):
        assert int(PdfReader(io.BytesIO(data)).decrypt("1234")) == 1    # 1 = the USER password


def test_nothing_is_written_to_disk(source, tmp_path):
    before = sorted(p.name for p in tmp_path.rglob("*"))
    protect_pdf(source, "AB12")
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("password", [None, "", "   "])
def test_no_password_refuses_with_a_clear_reason(source, password):
    with pytest.raises(ProtectError, match="no ID number"):
        protect_pdf(source, password)


def test_an_already_protected_or_broken_pdf_is_refused(tmp_path):
    for data in (locked_pdf("x"), b"not a pdf"):
        path = tmp_path / "bad.pdf"
        path.write_bytes(data)
        with pytest.raises(ProtectError):
            protect_pdf(path, "AB12")


def test_verification_rejects_an_unprotected_or_weak_copy(source):
    plain = source.read_bytes()
    with pytest.raises(ProtectError, match="safety check"):
        verify_protected(plain, "AB12", 1)                              # not encrypted at all
    protected = protect_pdf(source, "AB12")
    with pytest.raises(ProtectError):
        verify_protected(protected, "WRONG", 1)                         # does not open with the password we claim
    with pytest.raises(ProtectError):
        verify_protected(protected, "AB12", 5)                          # page count changed


def test_a_failed_safety_check_never_falls_back_to_the_original(source, monkeypatch):
    from app.services import pdf_protect

    monkeypatch.setattr(pdf_protect, "verify_protected", lambda *a, **k: (_ for _ in ()).throw(ProtectError("failed")))
    with pytest.raises(ProtectError):
        protect_pdf(source, "AB12")


def test_error_messages_never_contain_the_password(source):
    for password in ("AB12", None):
        try:
            protect_pdf(source, password)
            verify_protected(b"x", "SECRETPW", 1)
        except ProtectError as exc:
            assert "SECRETPW" not in str(exc) and "AB12" not in str(exc)


# ---- live client: the exact requests -----------------------------------------------------------------------------

def test_media_upload_request_is_exactly_what_meta_documents():
    client, seen = live_client(ok_handler)
    pdf = b"%PDF-1.4 fake pdf bytes"
    assert client.upload_media(pdf, "Payslip - November 2026.pdf") == "MEDIA123"
    (request,) = seen
    assert request.method == "POST" and str(request.url) == f"https://graph.facebook.com/v23.0/{PHONE_ID}/media"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="messaging_product"' in body and b"\r\n\r\nwhatsapp\r\n" in body
    assert b'name="type"' in body and b"\r\n\r\napplication/pdf\r\n" in body
    assert b'name="file"; filename="Payslip - November 2026.pdf"' in body and b"Content-Type: application/pdf" in body
    assert pdf in body


def test_template_message_request_body_is_exact():
    client, seen = live_client(ok_handler)
    message_id = client.send_template("+26771234567", "MEDIA123", "Payslip - November 2026.pdf", "payslip_", "en",
                                      "Thabo Molefe", "November 2026")
    (request,) = seen
    assert message_id == "wamid.LIVE567"
    assert request.method == "POST" and str(request.url) == f"https://graph.facebook.com/v23.0/{PHONE_ID}/messages"
    assert request.headers["authorization"] == f"Bearer {TOKEN}" and request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": "+26771234567", "type": "template",
        "template": {
            "name": "payslip_", "language": {"code": "en"},
            "components": [
                {"type": "header", "parameters": [{"type": "document", "document": {"id": "MEDIA123", "filename": "Payslip - November 2026.pdf"}}]},
                {"type": "body", "parameters": [{"type": "text", "text": "Thabo Molefe"}, {"type": "text", "text": "November 2026"}]},
            ],
        },
    }


def test_template_parameters_are_cleaned_for_whatsapp():
    client, seen = live_client(ok_handler)
    client.send_template("+26771234567", "M", "f.pdf", "payslip_", "en", "Thabo\n  Molefe\t Jr", "Nov\r\n2026")
    params = json.loads(seen[0].content)["template"]["components"][1]["parameters"]
    assert [p["text"] for p in params] == ["Thabo Molefe Jr", "Nov 2026"]
    assert clean_param("   ") == "-" and clean_param("a" * 2000) == "a" * 1000


def test_plain_document_message_request_body_is_exact():
    client, seen = live_client(ok_handler)
    client.send_document("+26771234567", "MEDIA123", "Payslip - November 2026.pdf", "November 2026")
    assert json.loads(seen[0].content) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": "+26771234567", "type": "document",
        "document": {"id": "MEDIA123", "filename": "Payslip - November 2026.pdf", "caption": "November 2026"},
    }


def test_graph_version_comes_from_the_setting_not_the_code():
    client, seen = live_client(ok_handler, version="v99.1")
    client.upload_media(b"%PDF-", "a.pdf")
    assert "/v99.1/" in str(seen[0].url) and "v23.0" not in str(seen[0].url)


@pytest.mark.parametrize("label,expected", [
    ("November 2026", "Payslip - November 2026.pdf"), ("Nov  2026 (bonus)", "Payslip - Nov 2026 (bonus).pdf"),
    ("Déc 2026", "Payslip - Dc 2026.pdf"), ("../../x", "Payslip - ....x.pdf"), ("", "Payslip - payslip.pdf"),
])
def test_payslip_filename(label, expected):
    assert payslip_filename(label) == expected


# ---- live client: allowed recipients (safety switch) -------------------------------------------------------------

def test_allowlist_blocks_numbers_not_on_it_and_sends_nothing():
    client, seen = live_client(ok_handler, allowed={"+26771234567"})
    with pytest.raises(WhatsAppError) as caught:
        client.send_template("+26779999999", "M", "f.pdf", "payslip_", "en", "A B", "November 2026")
    assert caught.value.kind == PERMANENT and caught.value.code == "LOCAL_NOT_ALLOWED"
    assert seen == []                                                   # no request left the building
    with pytest.raises(WhatsAppError):
        client.check_recipient("+26779999999")
    client.check_recipient("+26771234567")
    assert client.send_document("+26771234567", "M", "f.pdf") == "wamid.LIVE567"


def test_no_allowlist_means_no_restriction():
    client, seen = live_client(ok_handler)
    client.check_recipient("+26779999999")
    client.send_document("+26779999999", "M", "f.pdf")
    assert len(seen) == 1


# ---- live client: errors are classified safely -------------------------------------------------------------------

@pytest.mark.parametrize("status,code,kind", [
    (429, 130429, TRANSIENT), (400, 130429, TRANSIENT), (400, 131056, TRANSIENT), (429, None, TRANSIENT), (503, None, TRANSIENT),
    (500, 131016, TRANSIENT), (400, 80007, TRANSIENT),
    (401, 190, AUTH), (400, 190, AUTH), (401, None, AUTH), (403, 10, AUTH), (400, 0, AUTH),
    (400, 131026, PERMANENT), (400, 131047, PERMANENT), (400, 132001, PERMANENT), (400, 132000, PERMANENT),
    (400, 132012, PERMANENT), (400, 131030, PERMANENT), (400, 999999, PERMANENT), (404, None, PERMANENT),
])
def test_error_responses_are_classified(status, code, kind):
    response = error_response(status, code) if code is not None else httpx.Response(status, text="oops")
    assert error_from_response(response).kind == kind


def test_error_keeps_meta_code_and_message_and_masks_long_digit_runs():
    error = error_from_response(error_response(400, 131026, "Undeliverable", details="Cannot reach 26771234567 today"))
    assert (error.code, error.message) == ("131026", "Cannot reach ••• today") and str(error) == "permanent:131026"


def test_retry_after_header_is_read_and_capped():
    assert error_from_response(error_response(429, 130429, headers={"Retry-After": "7"})).retry_after == 7
    assert error_from_response(error_response(429, 130429, headers={"Retry-After": "9999"})).retry_after == 60
    assert error_from_response(error_response(429, 130429, headers={"Retry-After": "soon"})).retry_after is None


def _client_raising(exc):
    def handler(request):
        raise exc
    return live_client(handler)[0]


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x")])
def test_failures_before_the_request_is_sent_are_clear_transient_errors(exc):
    with pytest.raises(WhatsAppError) as caught:
        _client_raising(exc).send_document("+26771234567", "M", "f.pdf")
    assert caught.value.kind == TRANSIENT


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("x"), httpx.WriteTimeout("x"), httpx.RemoteProtocolError("x"), httpx.ReadError("x")])
def test_a_timeout_after_sending_is_ambiguous_never_transient(exc):
    with pytest.raises(WhatsAppError) as caught:
        _client_raising(exc).send_template("+26771234567", "M", "f.pdf", "payslip_", "en", "A B", "Nov 2026")
    assert caught.value.kind == AMBIGUOUS and caught.value.code == "LOCAL_TIMEOUT"


def test_an_unreadable_success_answer_is_ambiguous():
    client = live_client(lambda r: httpx.Response(200, text="<html>not json</html>"))[0]
    with pytest.raises(WhatsAppError) as caught:
        client.send_document("+26771234567", "M", "f.pdf")
    assert caught.value.kind == AMBIGUOUS
    client = live_client(lambda r: httpx.Response(200, json={"messages": []}))[0]
    with pytest.raises(WhatsAppError) as caught:
        client.send_document("+26771234567", "M", "f.pdf")
    assert caught.value.kind == AMBIGUOUS


def test_upload_failures_are_always_safe_to_retry():
    for exc in (httpx.ReadTimeout("x"), httpx.ConnectError("x")):
        with pytest.raises(WhatsAppError) as caught:
            _client_raising(exc).upload_media(b"%PDF-", "f.pdf")
        assert caught.value.kind == TRANSIENT
    client = live_client(lambda r: httpx.Response(200, json={}))[0]
    with pytest.raises(WhatsAppError) as caught:
        client.upload_media(b"%PDF-", "f.pdf")
    assert caught.value.kind == TRANSIENT


# ---- explanations ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code,fragment", [
    ("131026", "not be on WhatsApp"), ("LOCAL_NOT_ALLOWED", "allowed recipients list"), ("131030", "allowed list"),
    ("132001", "not found in this language"), ("132000", "number of parameters"), ("132012", "wrong format"),
    ("130429", "sending limit"), ("131056", "same number"), ("190", "access token"), ("131047", "24 hours"),
    ("LOCAL_TIMEOUT", "may or may not have been sent"), ("LOCAL_NO_PASSWORD", "no ID number"),
])
def test_common_errors_have_plain_language_explanations(code, fragment):
    assert fragment in explain(code, "raw message")


def test_unknown_codes_show_the_raw_code_and_message():
    assert explain("999123", "Something odd happened") == "WhatsApp returned error 999123: Something odd happened"
    assert explain(None, "weird") == "WhatsApp returned error unknown: weird"
    assert explain(None, None) == ""
    assert "••••" not in clean_error_text("call 26771234567 now") and "26771234567" not in explain("999", "call 26771234567 now")


# ---- mock client -------------------------------------------------------------------------------------------------

def mock_client(events):
    return MockWhatsAppClient(lambda *a: events.append(a), schedule=lambda delay, fn: (events.append(("scheduled", delay)), fn()))


def test_mock_normal_number_goes_sent_delivered_read_with_increasing_delays():
    events = []
    message_id = mock_client(events).send_template("+26771234567", "m", "f.pdf", "t", "en", "A B", "Nov")
    names = [e[1] for e in events if e[0] != "scheduled"]
    delays = [e[1] for e in events if e[0] == "scheduled"]
    assert message_id.startswith("wamid.MOCK") and names == ["sent", "delivered", "read"]
    assert delays == sorted(delays) and all(0 < d < 15 for d in delays)


def test_mock_000_is_accepted_then_fails_as_not_on_whatsapp():
    events = []
    mock_client(events).send_template("+26770000000", "m", "f.pdf", "t", "en", "A B", "Nov")
    real = [e for e in events if e[0] != "scheduled"]
    assert [e[1] for e in real] == ["sent", "failed"] and real[1][3][0]["code"] == 131026


def test_mock_111_is_a_rate_limit_and_222_is_a_timeout():
    client = mock_client([])
    with pytest.raises(WhatsAppError) as caught:
        client.send_template("+26770000111", "m", "f.pdf", "t", "en", "A B", "Nov")
    assert (caught.value.kind, caught.value.code, caught.value.http_status) == (TRANSIENT, "130429", 429)
    with pytest.raises(WhatsAppError) as caught:
        client.send_document("+26770000222", "m", "f.pdf")
    assert caught.value.kind == AMBIGUOUS
    assert client.upload_media(b"%PDF-", "f.pdf").startswith("mock-media-")


# ---- choosing the client / settings ------------------------------------------------------------------------------

def test_mock_is_the_default_and_needs_no_network():
    assert get_settings().whatsapp_mode == "mock"
    assert build_client(get_settings(), status_sink=lambda *a: None).mode == "mock"


def test_live_mode_refuses_to_start_when_a_required_setting_is_missing():
    base = dict(_env_file=None, session_secret="s" * 40, database_url="sqlite://", id_encryption_key=get_settings().id_encryption_key.get_secret_value(),
                whatsapp_mode="live")
    full = dict(whatsapp_access_token="tok", whatsapp_phone_number_id="123", whatsapp_app_secret="sec", whatsapp_verify_token="ver",
                graph_api_version="v23.0")
    assert Settings(**base, **full).whatsapp_mode == "live"
    for missing, env_name in [("whatsapp_access_token", "WHATSAPP_ACCESS_TOKEN"), ("whatsapp_phone_number_id", "WHATSAPP_PHONE_NUMBER_ID"),
                              ("whatsapp_app_secret", "WHATSAPP_APP_SECRET"), ("whatsapp_verify_token", "WHATSAPP_VERIFY_TOKEN"),
                              ("graph_api_version", "GRAPH_API_VERSION")]:
        with pytest.raises(ValidationError) as caught:
            Settings(**base, **{**full, missing: ""})
        assert env_name in str(caught.value) and "tok" not in str(caught.value).replace("token", "")
    with pytest.raises(ValidationError):
        Settings(**base, **{**full, "whatsapp_access_token": "your-access-token"})        # the .env.example placeholder
    with pytest.raises(ValidationError):
        Settings(**base, **{**full, "graph_api_version": "23"})


def test_build_client_refuses_live_with_missing_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_mode", "live")
    monkeypatch.setattr(s, "whatsapp_phone_number_id", "")
    with pytest.raises(WhatsAppConfigError, match="WHATSAPP_PHONE_NUMBER_ID"):
        build_client(s)


def test_the_live_client_cannot_be_created_without_a_mocked_transport_in_tests():
    with pytest.raises(RuntimeError, match="mocked transport"):
        LiveWhatsAppClient(base_url="https://graph.facebook.com", version="v23.0", phone_number_id="1", token="t")


def test_a_real_network_request_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="REAL network"):
        httpx.Client().get("https://graph.facebook.com/")


def test_allowed_recipients_setting_is_parsed_and_validated():
    base = dict(_env_file=None, session_secret="s" * 40, database_url="sqlite://", id_encryption_key=get_settings().id_encryption_key.get_secret_value())
    assert Settings(**base, whatsapp_allowed_recipients=" +26771234567 , +27821234567 ").allowed_recipients == {"+26771234567", "+27821234567"}
    assert Settings(**base).allowed_recipients == set()
    for bad in ("71234567", "+26771234567,abc", "+0123456789"):
        with pytest.raises(ValidationError) as caught:
            Settings(**base, whatsapp_allowed_recipients=bad)
        assert "71234567" not in str(caught.value).split("input_value")[0].replace("+26771234567", "")


# ---- nothing secret in logs --------------------------------------------------------------------------------------

def test_the_client_never_logs_tokens_or_phone_numbers(caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = live_client(ok_handler)
    client.upload_media(b"%PDF-", "f.pdf")
    client.send_template("+26771234567", "M", "f.pdf", "payslip_", "en", "Thabo Molefe", "November 2026")
    for handler in (lambda r: error_response(401, 190, "expired"), lambda r: error_response(400, 131026, "bad", "to 26771234567")):
        failing, _ = live_client(handler)
        with pytest.raises(WhatsAppError) as caught:
            failing.send_document("+26771234567", "M", "f.pdf")
        assert TOKEN not in str(caught.value) and "26771234567" not in str(caught.value) and "26771234567" not in (caught.value.message or "")
    assert TOKEN not in caplog.text and "26771234567" not in caplog.text and "71234567" not in caplog.text
