"""The live-config preflight (python -m app.scripts.check_live_config): mocked HTTP only, never sends a message."""
import inspect
import logging

import httpx
import pytest

from app.config import Settings, get_settings
from app.scripts import check_live_config as check
from app.scripts.check_live_config import FAIL, INFO, PASS, SKIP, WARN

TOKEN = "EAAB-preflight-secret-token-999"
APP_SECRET = "app-secret-preflight-xyz"
VERIFY = "verify-token-preflight-abc"
PHONE_ID = "109876543210987"
WABA = "209876543210987"
FULL_PHONE_DIGITS = "26771234567"


def good_env(**override):
    env = {
        "WHATSAPP_ACCESS_TOKEN": TOKEN, "WHATSAPP_PHONE_NUMBER_ID": PHONE_ID, "WHATSAPP_BUSINESS_ACCOUNT_ID": WABA,
        "WHATSAPP_APP_SECRET": APP_SECRET, "WHATSAPP_VERIFY_TOKEN": VERIFY, "GRAPH_API_VERSION": "v23.0",
        "WHATSAPP_TEMPLATE_NAME": "payslip_", "WHATSAPP_TEMPLATE_LANGUAGE": "en", "WHATSAPP_MODE": "mock",
        "WHATSAPP_ALLOWED_RECIPIENTS": "+26771234567",
    }
    env.update(override)
    return env


def tpl(name="payslip_", language="en", status="APPROVED", header=True, variables=2, named=False):
    components = []
    if header:
        components.append({"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["4::abc"]}})
    body = "Hello " + " and ".join("{{%d}}" % i for i in range(1, variables + 1)) if variables else "Hello"
    components.append({"type": "BODY", "text": body})
    return {"id": "1", "name": name, "language": language, "status": status, "category": "UTILITY",
            "parameter_format": "NAMED" if named else "POSITIONAL", "components": components}


def phone_ok(**fields):
    return {"id": PHONE_ID, "display_phone_number": "+267 71 234 567", "verified_name": "Acme Payroll", "quality_rating": "GREEN",
            "name_status": "APPROVED", "code_verification_status": "VERIFIED", **fields}


def meta_error(status, code, message="problem"):
    return httpx.Response(status, json={"error": {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "x"}})


class FakeMeta:
    """A pretend Graph API. `pages` is a list of template pages; page n+1 is reached with the `after` cursor of page n."""

    def __init__(self, phone=None, pages=None, phone_response=None, templates_response=None):
        self.requests: list[httpx.Request] = []
        self.phone = phone_ok() if phone is None else phone
        self.pages = [[tpl()]] if pages is None else pages
        self.phone_response, self.templates_response = phone_response, templates_response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/v23.0/{PHONE_ID}":
            return self.phone_response(request) if self.phone_response else httpx.Response(200, json=self.phone)
        if path == f"/v23.0/{WABA}/message_templates":
            if self.templates_response:
                return self.templates_response(request)
            index = int(request.url.params.get("after", "0").replace("cursor", "") or 0) if "after" in request.url.params else 0
            body = {"data": self.pages[index]}
            if index + 1 < len(self.pages):
                body["paging"] = {"cursors": {"before": "x", "after": f"cursor{index + 1}"},
                                  "next": f"https://graph.facebook.com{path}?access_token={TOKEN}&after=cursor{index + 1}"}
            else:
                body["paging"] = {"cursors": {"before": "x", "after": "end"}}
            return httpx.Response(200, json=body)
        return meta_error(404, 803, "not found")


def run(env=None, meta=None, capsys=None):
    meta = meta or FakeMeta()
    client = httpx.Client(transport=httpx.MockTransport(meta))
    code = check.main([], client=client, env=good_env() if env is None else env)
    return code, meta, (capsys.readouterr().out if capsys else "")


def statuses(env=None, meta=None):
    meta = meta or FakeMeta()
    report = check.run_checks(good_env() if env is None else env, httpx.Client(transport=httpx.MockTransport(meta)))
    return report, meta


def find(report, text, status=None):
    return [c for c in report.checks if text in c.title and (status is None or c.status == status)]


# ---- 1. settings -------------------------------------------------------------------------------------------------

def test_missing_settings_are_named_and_no_request_is_made(capsys):
    code, meta, out = run(env={}, capsys=capsys)
    assert code == 1 and meta.requests == []
    for name in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_BUSINESS_ACCOUNT_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"):
        assert name in out.split("Settings: some are missing")[1].split("[")[0]
    assert "RESULT: FAIL" in out and "What to do next" in out and "[SKIP] Token and phone number: not checked" in out


def test_each_required_setting_is_reported_when_missing_and_defaults_cover_the_three_that_have_one():
    for name in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_BUSINESS_ACCOUNT_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"):
        env = good_env()
        del env[name]
        report, _ = statuses(env)
        assert name in find(report, "some are missing", FAIL)[0].detail[0], name
    env = good_env()
    for name in ("GRAPH_API_VERSION", "WHATSAPP_TEMPLATE_NAME", "WHATSAPP_TEMPLATE_LANGUAGE"):
        del env[name]
    report, _ = statuses(env)
    ok = find(report, "all 8 live settings are present", PASS)
    assert ok and "GRAPH_API_VERSION, WHATSAPP_TEMPLATE_NAME, WHATSAPP_TEMPLATE_LANGUAGE not set: the app's default is used" in ok[0].title


def test_env_example_placeholders_and_blank_values_count_as_missing():
    report, meta = statuses(good_env(WHATSAPP_ACCESS_TOKEN="your-access-token", WHATSAPP_BUSINESS_ACCOUNT_ID="  ", WHATSAPP_VERIFY_TOKEN="your-webhook-verify-token"))
    detail = find(report, "some are missing", FAIL)[0].detail[0]
    assert "WHATSAPP_ACCESS_TOKEN" in detail and "WHATSAPP_BUSINESS_ACCOUNT_ID" in detail and "WHATSAPP_VERIFY_TOKEN" in detail and "WHATSAPP_APP_SECRET" not in detail
    assert meta.requests == []                                                         # no token = nothing to try


def test_malformed_ids_and_version_are_flagged_without_printing_them(capsys):
    code, meta, out = run(env=good_env(WHATSAPP_PHONE_NUMBER_ID="12 34-ab", WHATSAPP_BUSINESS_ACCOUNT_ID="abc", GRAPH_API_VERSION="23"), capsys=capsys)
    assert code == 1 and meta.requests == []
    assert "WHATSAPP_PHONE_NUMBER_ID (should be only digits)" in out and "WHATSAPP_BUSINESS_ACCOUNT_ID (should be only digits)" in out
    assert "GRAPH_API_VERSION (should look like v23.0)" in out
    assert "12 34-ab" not in out and "12 34" not in out and "=abc" not in out


def test_the_template_name_and_language_in_use_are_shown_but_nothing_secret(capsys):
    code, _, out = run(capsys=capsys)
    assert "Template to check: name 'payslip_', language 'en'" in out and "WHATSAPP_MODE is currently 'mock'" in out and "switch to live only after" in out


def test_works_even_when_the_app_itself_would_refuse_to_start_in_live_mode(capsys):
    """Settings() refuses live mode with missing values: the preflight must still run and say what is missing."""
    env = {"WHATSAPP_MODE": "live"}
    with pytest.raises(Exception, match="WHATSAPP_MODE=live needs"):
        Settings(_env_file=None, session_secret="s" * 40, database_url="sqlite://", id_encryption_key=get_settings().id_encryption_key.get_secret_value(),
                 whatsapp_mode="live", whatsapp_access_token="", whatsapp_phone_number_id="", whatsapp_app_secret="", whatsapp_verify_token="")
    code, _, out = run(env=env, capsys=capsys)
    assert code == 1 and "WHATSAPP_MODE is currently 'live'" in out and "WHATSAPP_ACCESS_TOKEN" in out


def test_the_business_account_id_is_a_known_optional_setting_for_the_app():
    s = get_settings()
    assert hasattr(s, "whatsapp_business_account_id")
    base = dict(_env_file=None, session_secret="s" * 40, database_url="sqlite://", id_encryption_key=s.id_encryption_key.get_secret_value())
    live = dict(whatsapp_mode="live", whatsapp_access_token="t", whatsapp_phone_number_id="1", whatsapp_app_secret="s", whatsapp_verify_token="v", graph_api_version="v23.0")
    assert Settings(**base, **live).whatsapp_mode == "live"                            # sending itself does not need the account id
    assert Settings(**base, whatsapp_business_account_id="123").whatsapp_business_account_id == "123"


# ---- 2. token and phone number -----------------------------------------------------------------------------------

def test_happy_path_passes_and_shows_the_masked_number_quality_and_name_status(capsys):
    code, meta, out = run(capsys=capsys)
    assert code == 0 and "RESULT: PASS" in out and "RESULT: FAIL" not in out
    assert "[PASS] Token works and the phone number ID is valid" in out
    assert "WhatsApp number: ***567 (last 3 digits), display name 'Acme Payroll'" in out
    assert "Quality rating: GREEN (good)" in out and "Name status: APPROVED" in out
    assert "[PASS] Template 'payslip_' language 'en' exists and is APPROVED" in out
    assert "[PASS] The template has a DOCUMENT header and 2 body variables" in out
    assert "WHATSAPP_ALLOWED_RECIPIENTS is set: 1 number." in out and "Set WHATSAPP_MODE=live" in out


def test_only_two_read_only_requests_with_the_documented_urls_and_the_bearer_token():
    report, meta = statuses()
    assert [r.method for r in meta.requests] == ["GET", "GET"]
    phone, templates = meta.requests
    assert phone.url.path == f"/v23.0/{PHONE_ID}" and phone.url.params["fields"] == "id,display_phone_number,verified_name,quality_rating,code_verification_status,name_status"
    assert templates.url.path == f"/v23.0/{WABA}/message_templates" and templates.url.params["limit"] == "100"
    assert set(templates.url.params["fields"].split(",")) >= {"name", "language", "status", "components"}
    for r in meta.requests:
        assert r.headers["authorization"] == f"Bearer {TOKEN}" and r.url.host == "graph.facebook.com" and "access_token" not in r.url.params
        assert r.content == b""


def test_it_can_never_send_a_message():
    source = inspect.getsource(check)
    assert "/messages" not in source.replace("message_templates", "") and ".post(" not in source and "send_template" not in source and "WhatsAppClient" not in source


@pytest.mark.parametrize("response,expect", [
    (lambda r: meta_error(401, 190, "Error validating access token: Session has expired"), "rejected the access token"),
    (lambda r: meta_error(400, 190, "bad"), "rejected the access token"),
    (lambda r: meta_error(403, 200, "no permission"), "not allowed to do this"),
    (lambda r: meta_error(400, 10, "no permission"), "not allowed to do this"),
    (lambda r: meta_error(404, 803, "gone"), "cannot find that ID"),
    (lambda r: meta_error(400, 100, "Unsupported get request"), "ID or a value is probably wrong"),
    (lambda r: meta_error(429, 4, "slow down"), "too many requests"),
    (lambda r: httpx.Response(503, text="oops"), "temporary problem"),
    (lambda r: httpx.Response(418, text="teapot"), "unexpected answer (HTTP 418"),
])
def test_phone_number_errors_are_explained_in_plain_language(response, expect, capsys):
    code, meta, out = run(meta=FakeMeta(phone_response=response), capsys=capsys)
    assert code == 1 and "[FAIL] Token and phone number: Meta said no" in out and expect in out and "RESULT: FAIL" in out


def test_a_rejected_token_skips_the_template_check_but_a_wrong_phone_id_does_not():
    report, meta = statuses(meta=FakeMeta(phone_response=lambda r: meta_error(401, 190)))
    assert find(report, "Templates: not checked", SKIP) and len(meta.requests) == 1
    report, meta = statuses(meta=FakeMeta(phone_response=lambda r: meta_error(404, 803)))
    assert find(report, "Template 'payslip_' exists", PASS) and len(meta.requests) == 2


@pytest.mark.parametrize("exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), httpx.ConnectTimeout("x")])
def test_network_problems_fail_clearly(exc, capsys):
    def boom(request):
        raise exc

    code, meta, out = run(meta=FakeMeta(phone_response=boom, templates_response=boom), capsys=capsys)
    assert code == 1 and "internet connection" in out
    assert [line for line in out.splitlines() if line.startswith("[FAIL]")] == [
        "[FAIL] Token and phone number: could not reach Meta", "[FAIL] Templates: could not reach Meta"]


@pytest.mark.parametrize("fields,status,fragment", [
    ({"quality_rating": "YELLOW"}, WARN, "YELLOW (medium"), ({"quality_rating": "RED"}, WARN, "RED (poor"), ({"quality_rating": "NA"}, INFO, "not rated yet"),
    ({"name_status": "PENDING_REVIEW"}, WARN, "PENDING_REVIEW"), ({"name_status": "DECLINED"}, WARN, "DECLINED"), ({"name_status": "EXPIRED"}, WARN, "EXPIRED"),
    ({"name_status": "AVAILABLE_WITHOUT_REVIEW"}, PASS, "AVAILABLE_WITHOUT_REVIEW"), ({"name_status": "NONE"}, WARN, "NONE"),
    ({"code_verification_status": "UNVERIFIED"}, WARN, "not verified yet"),
])
def test_quality_name_status_and_verification_are_reported(fields, status, fragment):
    report, _ = statuses(meta=FakeMeta(phone=phone_ok(**fields)))
    assert [c for c in report.checks if fragment in c.title and c.status == status], [c.title for c in report.checks]


def test_warnings_do_not_fail_the_check(capsys):
    code, _, out = run(meta=FakeMeta(phone=phone_ok(quality_rating="RED", name_status="PENDING_REVIEW")), capsys=capsys)
    assert code == 0 and "RESULT: PASS (with 2 warnings)" in out and "Warnings do not block going live" in out


# ---- 3. templates ------------------------------------------------------------------------------------------------

def test_a_missing_template_fails_and_lists_the_names_that_do_exist(capsys):
    code, _, out = run(meta=FakeMeta(pages=[[tpl("welcome_msg"), tpl("order_update"), tpl("welcome_msg", "fr")]]), capsys=capsys)
    assert code == 1 and "[FAIL] Template 'payslip_' was not found" in out and "3 template(s) in total" in out
    assert "Template names on this account: order_update, welcome_msg" in out and "set WHATSAPP_TEMPLATE_NAME to one of the names above" in out


def test_an_account_with_no_templates_says_so(capsys):
    code, _, out = run(meta=FakeMeta(pages=[[]]), capsys=capsys)
    assert code == 1 and "no templates on this account at all" in out


def test_the_name_must_match_exactly():
    report, _ = statuses(meta=FakeMeta(pages=[[tpl("payslip"), tpl("Payslip_"), tpl("payslip__")]]))
    assert find(report, "was not found", FAIL)


@pytest.mark.parametrize("status,help_text", [
    ("PENDING", "still reviewing"), ("REJECTED", "rejected it"), ("PAUSED", "paused it"), ("DISABLED", "disabled it"),
    ("IN_APPEAL", "appeal"), ("LIMIT_EXCEEDED", "template limit"), ("ARCHIVED", "archived"), ("DELETED", "deleted"), ("PENDING_DELETION", "being deleted"),
])
def test_a_template_that_is_not_approved_fails_with_what_to_do(status, help_text, capsys):
    code, _, out = run(meta=FakeMeta(pages=[[tpl(status=status)]]), capsys=capsys)
    assert code == 1 and f"is {status}, not APPROVED" in out and help_text in out


def test_a_language_mismatch_lists_the_languages_and_names_the_setting_to_change(capsys):
    code, _, out = run(env=good_env(WHATSAPP_TEMPLATE_LANGUAGE="en"), meta=FakeMeta(pages=[[tpl(language="en_US"), tpl(language="fr", status="PENDING")]]), capsys=capsys)
    assert code == 1 and "language 'en' does not exist" in out and "Available languages: en_US (APPROVED), fr (PENDING)" in out
    assert "Only the spelling differs" not in out and "Change WHATSAPP_TEMPLATE_LANGUAGE to en_US (the only approved language)" in out


def test_a_language_that_differs_only_in_spelling_says_the_exact_code(capsys):
    code, _, out = run(env=good_env(WHATSAPP_TEMPLATE_LANGUAGE="en-us"), meta=FakeMeta(pages=[[tpl(language="en_US")]]), capsys=capsys)
    assert code == 1 and "Only the spelling differs. Change WHATSAPP_TEMPLATE_LANGUAGE to en_US (it must match exactly)" in out


def test_several_approved_languages_are_all_offered(capsys):
    code, _, out = run(env=good_env(WHATSAPP_TEMPLATE_LANGUAGE="pt"), meta=FakeMeta(pages=[[tpl(language="en"), tpl(language="en_GB"), tpl(language="fr", status="REJECTED")]]), capsys=capsys)
    assert "one of the approved languages: en, en_GB" in out and "or add a 'pt' version" in out


def test_no_approved_language_at_all(capsys):
    code, _, out = run(env=good_env(WHATSAPP_TEMPLATE_LANGUAGE="pt"), meta=FakeMeta(pages=[[tpl(language="en", status="PENDING")]]), capsys=capsys)
    assert "None of its languages is approved yet" in out


def test_right_language_not_approved_but_another_language_is(capsys):
    code, _, out = run(meta=FakeMeta(pages=[[tpl(language="en", status="PENDING"), tpl(language="en_US")]]), capsys=capsys)
    assert code == 1 and "(en) is PENDING, not APPROVED" in out and "Another language is approved (en_US)" in out


def test_all_languages_are_listed_when_there_are_several():
    report, _ = statuses(meta=FakeMeta(pages=[[tpl(language="en"), tpl(language="fr")]]))
    assert find(report, "All languages of this template: en (APPROVED), fr (APPROVED)", INFO)


def test_templates_are_found_on_a_later_page_using_the_cursor_and_the_next_link_is_never_followed():
    meta = FakeMeta(pages=[[tpl("a_one"), tpl("a_two")], [tpl("b_three")], [tpl("payslip_")]])
    report, _ = statuses(meta=meta)
    assert find(report, "language 'en' exists and is APPROVED", PASS)
    template_calls = [r for r in meta.requests if r.url.path.endswith("message_templates")]
    assert [r.url.params.get("after") for r in template_calls] == [None, "cursor1", "cursor2"]
    assert all("access_token" not in str(r.url) and r.headers["authorization"] == f"Bearer {TOKEN}" for r in template_calls)


def test_paging_is_capped_so_it_cannot_loop_forever(monkeypatch):
    monkeypatch.setattr(check, "MAX_PAGES", 3)
    meta = FakeMeta(templates_response=lambda r: httpx.Response(200, json={"data": [tpl("other")], "paging": {"cursors": {"after": "c"}, "next": "https://x"}}))
    report, _ = statuses(meta=meta)
    assert len([r for r in meta.requests if r.url.path.endswith("message_templates")]) == 3 and find(report, "was not found", FAIL)


@pytest.mark.parametrize("response,expect", [
    (lambda r: meta_error(400, 100, "Invalid parameter"), "ID or a value is probably wrong"),
    (lambda r: meta_error(404, 803, "WhatsApp Business Account not found"), "cannot find that ID"),
    (lambda r: meta_error(403, 200, "no permission"), "not allowed to do this"),
    (lambda r: meta_error(401, 190, "expired"), "rejected the access token"),
])
def test_template_list_errors_are_explained_and_hint_at_the_two_kinds_of_id(response, expect, capsys):
    code, _, out = run(meta=FakeMeta(templates_response=response), capsys=capsys)
    assert code == 1 and "[FAIL] Templates: Meta said no" in out and expect in out
    if "ID or a value" in expect or "cannot find" in expect:
        assert "must be the Business ACCOUNT id, not the phone number id" in out


def test_a_missing_business_account_id_fails_the_template_check_with_a_pointer(capsys):
    env = good_env()
    del env["WHATSAPP_BUSINESS_ACCOUNT_ID"]
    code, meta, out = run(env=env, capsys=capsys)
    assert code == 1 and "[SKIP] Templates: not checked" in out and "different from the phone number id" in out
    assert [r.url.path for r in meta.requests] == [f"/v23.0/{PHONE_ID}"]               # the phone check still ran


def test_template_shape_problems_are_warnings_not_failures(capsys):
    for entry, fragment in [(tpl(header=False), "no DOCUMENT header"), (tpl(variables=1), "1 variable(s), the app sends 2"), (tpl(variables=3), "3 variable(s), the app sends 2"),
                            (tpl(named=True), "NAMED parameters")]:
        code, _, out = run(meta=FakeMeta(pages=[[entry]]), capsys=capsys)
        assert code == 0 and "[WARN] The template" in out and fragment in out and "RESULT: PASS (with" in out


def test_when_meta_does_not_list_the_template_parts_it_is_only_noted():
    entry = tpl()
    del entry["components"]
    report, _ = statuses(meta=FakeMeta(pages=[[entry]]))
    assert find(report, "did not list the template's parts", INFO) and not report.failed


# ---- 4. allowed recipients ---------------------------------------------------------------------------------------

def test_allowed_recipients_not_set_is_a_warning_with_advice(capsys):
    code, _, out = run(env=good_env(WHATSAPP_ALLOWED_RECIPIENTS=""), capsys=capsys)
    assert code == 0 and "[WARN] WHATSAPP_ALLOWED_RECIPIENTS is NOT set" in out and "every employee in a batch" in out and "your own number" in out


@pytest.mark.parametrize("value,count", [("+26771234567", 1), (" +26771234567 , +27821234567 ,+26772345678", 3)])
def test_allowed_recipients_reports_how_many_numbers_without_showing_them(value, count, capsys):
    code, _, out = run(env=good_env(WHATSAPP_ALLOWED_RECIPIENTS=value), capsys=capsys)
    assert f"WHATSAPP_ALLOWED_RECIPIENTS is set: {count} number" in out
    for digits in ("26771234567", "27821234567", "26772345678", "71234567"):
        assert digits not in out


def test_invalid_allowed_recipients_fail_because_the_app_would_refuse_to_start(capsys):
    code, _, out = run(env=good_env(WHATSAPP_ALLOWED_RECIPIENTS="+26771234567,71234567,abc"), capsys=capsys)
    assert code == 1 and "2 entries that are not valid phone numbers" in out and "refuses to start" in out and "71234567" not in out


# ---- 5. summary and secrecy --------------------------------------------------------------------------------------

def test_summary_lines_and_exit_codes(capsys):
    ok, _, out = run(capsys=capsys)
    bad, _, out2 = run(env={}, capsys=capsys)
    assert (ok, bad) == (0, 1) and "RESULT: PASS" in out and "RESULT: FAIL (" in out2 and "to fix before going live" in out2


def test_nothing_secret_is_ever_printed_or_logged_even_when_meta_echoes_it(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    echo = lambda r: meta_error(400, 100, f"Invalid token {TOKEN} for secret {APP_SECRET} phone 26771234567 verify {VERIFY}")  # noqa: E731
    teapot = lambda r: meta_error(418, 999, f"{TOKEN} 26771234567")  # noqa: E731
    scenarios = [
        (good_env(), FakeMeta()),
        (good_env(), FakeMeta(phone_response=echo, templates_response=echo)),
        (good_env(), FakeMeta(phone_response=teapot, templates_response=teapot)),
        (good_env(), FakeMeta(pages=[[tpl("other")]])),
        (good_env(WHATSAPP_ALLOWED_RECIPIENTS="+26771234567,+26772345678"), FakeMeta()),
        ({}, FakeMeta()),
    ]
    printed = []
    for env, meta in scenarios:
        _, _, out = run(env=env, meta=meta, capsys=capsys)
        printed.append(out)
    everything = chr(10).join(printed) + caplog.text
    for secret in (TOKEN, APP_SECRET, VERIFY, PHONE_ID, WABA, FULL_PHONE_DIGITS, "26772345678", "71234567", "EAAB"):
        assert secret not in everything, secret
    assert "[hidden]" in everything                                                      # an echoed secret was replaced, not shown


def test_the_env_file_is_read_without_validation_and_real_environment_variables_win(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WHATSAPP_ACCESS_TOKEN=from-file\nWHATSAPP_PHONE_NUMBER_ID=111  # a comment\nGRAPH_API_VERSION=v24.0\nOTHER_THING=ignored\n", encoding="utf-8")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "from-environment")
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)       # (the test setup defines one: remove it so the file value shows)
    monkeypatch.delenv("GRAPH_API_VERSION", raising=False)
    values = check.load_env(env_file)
    assert values["WHATSAPP_ACCESS_TOKEN"] == "from-environment" and values["WHATSAPP_PHONE_NUMBER_ID"] == "111" and values["GRAPH_API_VERSION"] == "v24.0"
    assert "OTHER_THING" not in values
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert "WHATSAPP_PHONE_NUMBER_ID" not in check.load_env(tmp_path / "missing.env")


def test_the_command_cannot_run_with_a_real_client_inside_tests():
    with pytest.raises(RuntimeError, match="mocked HTTP client"):
        check.main([], env=good_env())


def test_custom_base_url_and_version_are_used():
    meta = FakeMeta()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "1", "display_phone_number": "+267 71 234 567", "quality_rating": "GREEN", "name_status": "APPROVED"}) if "message_templates" not in request.url.path \
            else httpx.Response(200, json={"data": [tpl()], "paging": {}})

    env = good_env(WHATSAPP_BASE_URL="https://graph.example.test/", GRAPH_API_VERSION="v99.1")
    check.run_checks(env, httpx.Client(transport=httpx.MockTransport(handler)))
    assert [str(r.url).split("?")[0] for r in seen] == [f"https://graph.example.test/v99.1/{PHONE_ID}", f"https://graph.example.test/v99.1/{WABA}/message_templates"]
