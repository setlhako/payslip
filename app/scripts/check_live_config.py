"""Preflight for live WhatsApp sending. Run: python -m app.scripts.check_live_config

It NEVER sends a message: it only makes two kinds of read-only (GET) requests to Meta's Graph API:
    GET /{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}?fields=...      is the token valid, is the phone number id right
    GET /{GRAPH_API_VERSION}/{WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates?fields=...&limit=100
                                                                         does your template exist, is it APPROVED, which language
(Endpoints and fields checked against Meta's reference pages, Nov 2026. Those pages do not document name/language filters on the
template list, so ALL templates are listed page by page and filtered here. The "next page" link Meta returns contains the access
token in its address, so it is never followed or printed: the cursor is used with our own address instead.)

It prints PASS / FAIL / WARN lines and a summary with next steps. It never prints a token, secret, verify token, ID value or full
phone number (the display phone number is masked to its last 3 digits), and it also works when WHATSAPP_MODE is still mock, or when
the settings are incomplete (the app itself refuses to start in live mode with missing settings; this command tells you what is missing).
"""
import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import dotenv_values

from app.config import PROJECT_ROOT
from app.services.phones import mask_phone
from app.services.whatsapp_errors import clean_error_text

ENV_FILE = PROJECT_ROOT / ".env"
TIMEOUT = 15.0
PAGE_LIMIT = 100
MAX_PAGES = 20

PASS, FAIL, WARN, INFO, SKIP = "PASS", "FAIL", "WARN", "INFO", "SKIP"

# (setting name, what it is, must be only digits?, default used when the setting is absent)
REQUIRED = [
    ("WHATSAPP_ACCESS_TOKEN", "the access token", False, None),
    ("WHATSAPP_PHONE_NUMBER_ID", "the phone number ID", True, None),
    ("WHATSAPP_BUSINESS_ACCOUNT_ID", "the WhatsApp Business Account ID", True, None),
    ("WHATSAPP_APP_SECRET", "the app secret", False, None),
    ("WHATSAPP_VERIFY_TOKEN", "the webhook verify token", False, None),
    ("GRAPH_API_VERSION", "the Graph API version", False, "v23.0"),
    ("WHATSAPP_TEMPLATE_NAME", "the template name", False, "payslip_"),
    ("WHATSAPP_TEMPLATE_LANGUAGE", "the template language", False, "en"),
]
OTHER = ("WHATSAPP_MODE", "WHATSAPP_ALLOWED_RECIPIENTS", "WHATSAPP_BASE_URL")
SECRET_NAMES = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN")


@dataclass
class Check:
    status: str
    title: str
    detail: list[str] = field(default_factory=list)
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, status: str, title: str, *detail: str, fix: str = "") -> Check:
        check = Check(status, title, list(detail), fix)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]


def load_env(env_file: Path = ENV_FILE) -> dict[str, str]:
    """The raw values the app would read: the .env file, overridden by real environment variables. No validation."""
    wanted = [n for n, *_ in REQUIRED] + list(OTHER)             # only the WhatsApp settings: nothing else in .env is touched
    from_file = dotenv_values(env_file) if env_file.exists() else {}
    values = {name: (from_file.get(name) or "") for name in wanted if name in from_file}
    for name in wanted:
        if name in os.environ:
            values[name] = os.environ[name]
    return values


def _is_missing(value: str) -> bool:
    return not value.strip() or value.strip().lower().startswith("your-")      # empty, or the .env.example placeholder


class _Scrubber:
    """Removes every secret value (and long digit runs, which could be phone numbers) from text before it is printed."""

    def __init__(self, env: dict[str, str]):
        self.secrets = [env.get(n, "").strip().strip("\"'") for n in SECRET_NAMES]
        self.secrets = [s for s in self.secrets if len(s) >= 4]

    def __call__(self, text: str | None) -> str:
        out = clean_error_text(text, 300) or ""
        for secret in self.secrets:
            out = out.replace(secret, "[hidden]")
        return out


# ---- 1. settings -------------------------------------------------------------------------------------------------

def check_settings(env: dict[str, str], report: Report) -> dict[str, str]:
    """Returns the effective values (defaults applied). Prints only setting NAMES, never values (template name/language aside)."""
    effective: dict[str, str] = {}
    missing, malformed, defaulted = [], [], []
    for name, _, digits_only, default in REQUIRED:
        raw = env.get(name, "")
        if _is_missing(raw):
            if default is not None and not raw.strip():
                effective[name] = default
                defaulted.append(name)
                continue
            missing.append(name)
            effective[name] = ""
            continue
        effective[name] = raw.strip().strip("\"'")
        if digits_only and not effective[name].isdigit():
            malformed.append(f"{name} (should be only digits)")
        if name == "GRAPH_API_VERSION" and not re.fullmatch(r"v\d+\.\d+", effective[name]):
            malformed.append("GRAPH_API_VERSION (should look like v23.0)")
    if missing:
        report.add(FAIL, "Settings: some are missing", "Missing (or still the .env.example placeholder): " + ", ".join(missing),
                   fix="Put real values for these in your .env file. " + (
                       "WHATSAPP_BUSINESS_ACCOUNT_ID is the WhatsApp Business ACCOUNT id (different from the phone number id): "
                       "find it in Meta's WhatsApp Manager." if "WHATSAPP_BUSINESS_ACCOUNT_ID" in missing else ""))
    if malformed:
        report.add(FAIL, "Settings: some look wrong", "Check: " + ", ".join(malformed),
                   fix="Copy the values again from Meta's dashboard (IDs are digits only, no spaces).")
    if not missing and not malformed:
        extra = f" ({', '.join(defaulted)} not set: the app's default is used)" if defaulted else ""
        report.add(PASS, "Settings: all 8 live settings are present" + extra)
    report.add(INFO, f"Template to check: name '{effective['WHATSAPP_TEMPLATE_NAME']}', language '{effective['WHATSAPP_TEMPLATE_LANGUAGE']}'")
    mode = env.get("WHATSAPP_MODE", "").strip() or "mock"
    report.add(INFO, f"WHATSAPP_MODE is currently '{mode}'" + (
        " (that is fine for now: switch to live only after this check passes)" if mode != "live" else ""))
    return effective


# ---- Meta errors in plain language -------------------------------------------------------------------------------

def _error_parts(response: httpx.Response) -> tuple[int | None, str]:
    try:
        error = response.json().get("error") or {}
        code = error.get("code")
        return (int(code) if code is not None else None), str(error.get("error_data", {}).get("details") or error.get("message") or "")
    except Exception:
        return None, ""


def explain_http_error(response: httpx.Response, what: str, scrub) -> tuple[str, str]:
    """(what happened, what to do) in plain language for a non-2xx answer."""
    code, message = _error_parts(response)
    status = response.status_code
    if code == 190 or status == 401:
        return ("Meta rejected the access token (it is wrong, expired or revoked).",
                "Create a new token (a System User token in Meta Business settings does not expire), put it in WHATSAPP_ACCESS_TOKEN, and run this check again.")
    if code in (10, 200) or status == 403:
        return ("The token is valid but is not allowed to do this.",
                f"Give the token permission for {what}. Tokens usually need whatsapp_business_management (templates/phone numbers) and "
                "whatsapp_business_messaging (sending); Meta's reference pages do not name the permissions, so check the token in the app dashboard.")
    if code in (803,) or status == 404:
        return ("Meta cannot find that ID, or this token cannot see it.",
                "Check the ID is copied correctly, belongs to the same business as the token, and is the right KIND of ID "
                "(phone number ID versus Business Account ID are different numbers).")
    if code == 100 or status == 400:
        return ("Meta did not accept the request: an ID or a value is probably wrong.",
                "Check that the IDs are the right kind and only digits (phone number ID versus Business Account ID), and the GRAPH_API_VERSION exists.")
    if status == 429 or code in (4, 80007, 130429):
        return ("Meta says there were too many requests.", "Wait a few minutes and run this check again.")
    if status >= 500 or code == 2:
        return ("Meta had a temporary problem.", "Try again in a few minutes (see Meta's status page if it keeps happening).")
    return (f"Meta returned an unexpected answer (HTTP {status}" + (f", code {code}" if code is not None else "") + f"): {scrub(message) or 'no details'}",
            "Run the check again; if it keeps happening, tell your developer the HTTP status and code above.")


def _get(client: httpx.Client, url: str, token: str, params: dict) -> httpx.Response:
    return client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})


# ---- 2. token and phone number -----------------------------------------------------------------------------------

PHONE_FIELDS = "id,display_phone_number,verified_name,quality_rating,code_verification_status,name_status"
QUALITY = {"GREEN": (PASS, "GREEN (good)"), "YELLOW": (WARN, "YELLOW (medium: watch your message quality)"),
           "RED": (WARN, "RED (poor: Meta may limit your sending)"), "NA": (INFO, "not rated yet (normal for a new number)")}
NAME_STATUS = {"APPROVED": (PASS, "APPROVED"), "AVAILABLE_WITHOUT_REVIEW": (PASS, "AVAILABLE_WITHOUT_REVIEW (usable)"),
               "PENDING_REVIEW": (WARN, "PENDING_REVIEW (Meta is still reviewing the display name)"),
               "DECLINED": (WARN, "DECLINED (Meta refused the display name: change it in WhatsApp Manager)"),
               "EXPIRED": (WARN, "EXPIRED (the name approval expired: request it again in WhatsApp Manager)"),
               "NONE": (WARN, "NONE (no display name approved yet)")}


def check_phone_number(client: httpx.Client, base: str, version: str, phone_id: str, token: str, report: Report, scrub) -> str:
    """"ok" (token works AND the phone number id is valid), "token" (Meta rejected the token itself) or "failed" (anything else)."""
    try:
        response = _get(client, f"{base}/{version}/{phone_id}", token, {"fields": PHONE_FIELDS})
    except httpx.HTTPError as exc:
        report.add(FAIL, "Token and phone number: could not reach Meta", f"The request failed ({type(exc).__name__}).",
                   fix="Check this computer's internet connection (and WHATSAPP_BASE_URL if you changed it), then run the check again.")
        return "failed"
    if response.status_code // 100 != 2:
        what, fix = explain_http_error(response, "reading the phone number", scrub)
        report.add(FAIL, "Token and phone number: Meta said no", what, fix=fix)
        code, _ = _error_parts(response)
        return "token" if (code == 190 or response.status_code == 401) else "failed"
    try:
        data = response.json()
    except ValueError:
        report.add(FAIL, "Token and phone number: unreadable answer", "Meta's answer was not JSON.", fix="Run the check again.")
        return "failed"
    report.add(PASS, "Token works and the phone number ID is valid")
    shown = mask_phone(data.get("display_phone_number")) if data.get("display_phone_number") else "(not returned)"
    report.add(INFO, f"WhatsApp number: {shown} (last 3 digits)" + (f", display name '{scrub(data['verified_name'])}'" if data.get("verified_name") else ""))
    if str(data.get("id", phone_id)) != phone_id:
        report.add(WARN, "The phone number ID Meta returned differs from WHATSAPP_PHONE_NUMBER_ID", fix="Copy the phone number ID again from WhatsApp Manager.")
    quality = str(data.get("quality_rating") or "NA")
    status, text = QUALITY.get(quality, (INFO, f"{scrub(quality)} (unknown value)"))
    report.add(status, f"Quality rating: {text}", fix="Meta limits numbers with a poor rating. Keep messages expected by recipients." if status == WARN else "")
    name_status = str(data.get("name_status") or "NONE")
    status, text = NAME_STATUS.get(name_status, (INFO, f"{scrub(name_status)} (unknown value)"))
    report.add(status, f"Name status: {text}", fix="Sort out the display name in Meta's WhatsApp Manager." if status == WARN else "")
    code_status = data.get("code_verification_status")
    if code_status == "UNVERIFIED":
        report.add(WARN, "The phone number is not verified yet (code_verification_status UNVERIFIED)", fix="Finish verifying the number in WhatsApp Manager.")
    return "ok"


# ---- 3. templates ------------------------------------------------------------------------------------------------

TEMPLATE_FIELDS = "name,language,status,category,parameter_format,components"
STATUS_HELP = {
    "PENDING": "Meta is still reviewing it. Wait for approval (usually minutes to a day), then run this check again.",
    "REJECTED": "Meta rejected it. Open it in WhatsApp Manager, read the reason, edit and resubmit it.",
    "PAUSED": "Meta paused it because of poor quality. Edit it in WhatsApp Manager, or wait for the pause to end.",
    "DISABLED": "Meta disabled it. Create a new template.",
    "IN_APPEAL": "An appeal is in progress. Wait for the result.",
    "LIMIT_EXCEEDED": "The account has hit its template limit. Delete unused templates or wait.",
    "ARCHIVED": "It is archived. Create or restore a template.",
    "DELETED": "It was deleted. Create a new template.",
    "PENDING_DELETION": "It is being deleted. Create a new template.",
}


def list_templates(client: httpx.Client, base: str, version: str, waba_id: str, token: str) -> list[dict]:
    """Every template on the account (all pages). Raises httpx.HTTPError or _MetaError."""
    url, templates, after = f"{base}/{version}/{waba_id}/message_templates", [], None
    for _ in range(MAX_PAGES):
        params = {"fields": TEMPLATE_FIELDS, "limit": str(PAGE_LIMIT)}
        if after:
            params["after"] = after
        response = _get(client, url, token, params)
        if response.status_code // 100 != 2:
            raise _MetaError(response)
        body = response.json()
        templates.extend(t for t in body.get("data") or [] if isinstance(t, dict))
        paging = body.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after") if paging.get("next") else None   # never follow 'next': it carries the token
        if not after:
            break
    return templates


class _MetaError(Exception):
    def __init__(self, response: httpx.Response):
        super().__init__("meta error")
        self.response = response


def _components(template: dict) -> tuple[bool, int, bool]:
    """(has a DOCUMENT header, number of body variables, uses named parameters) as far as the answer shows it."""
    parts = [c for c in template.get("components") or [] if isinstance(c, dict)]
    has_doc_header = any(str(c.get("type")).upper() == "HEADER" and str(c.get("format")).upper() == "DOCUMENT" for c in parts)
    body_text = " ".join(str(c.get("text") or "") for c in parts if str(c.get("type")).upper() == "BODY")
    variables = len(set(re.findall(r"\{\{\s*(\d+)\s*\}\}", body_text)))
    return has_doc_header, variables, str(template.get("parameter_format")).upper() == "NAMED"


def check_templates(client, base, version, waba_id, token, name, language, report: Report, scrub) -> None:
    try:
        templates = list_templates(client, base, version, waba_id, token)
    except _MetaError as exc:
        what, fix = explain_http_error(exc.response, "reading message templates", scrub)
        if exc.response.status_code in (400, 404):
            fix += " WHATSAPP_BUSINESS_ACCOUNT_ID must be the Business ACCOUNT id, not the phone number id."
        report.add(FAIL, "Templates: Meta said no", what, fix=fix)
        return
    except httpx.HTTPError as exc:
        report.add(FAIL, "Templates: could not reach Meta", f"The request failed ({type(exc).__name__}).", fix="Check your internet connection and run the check again.")
        return
    except ValueError:
        report.add(FAIL, "Templates: unreadable answer", "Meta's answer was not JSON.", fix="Run the check again.")
        return

    same_name = [t for t in templates if t.get("name") == name]
    if not same_name:
        names = sorted({str(t.get("name")) for t in templates if t.get("name")})
        detail = [f"The account has {len(templates)} template(s) in total."]
        if names:
            detail.append("Template names on this account: " + ", ".join(names[:15]) + (" ..." if len(names) > 15 else ""))
        else:
            detail.append("There are no templates on this account at all (or this token cannot see them).")
        report.add(FAIL, f"Template '{name}' was not found", *detail,
                   fix="Create the template in Meta's WhatsApp Manager (a DOCUMENT header and two body variables {{1}} name and {{2}} pay month), "
                       "or set WHATSAPP_TEMPLATE_NAME to one of the names above. Check WHATSAPP_BUSINESS_ACCOUNT_ID is the right account.")
        return

    report.add(PASS, f"Template '{name}' exists")
    by_language = {str(t.get("language")): str(t.get("status")) for t in same_name}
    approved = sorted(lang for lang, st in by_language.items() if st == "APPROVED")
    listing = ", ".join(f"{lang} ({st})" for lang, st in sorted(by_language.items()))
    if language not in by_language:
        close = [lang for lang in by_language if lang.lower().replace("-", "_") == language.lower().replace("-", "_")]
        if close:
            fix = f"Only the spelling differs. Change WHATSAPP_TEMPLATE_LANGUAGE to {close[0]} (it must match exactly)."
        elif len(approved) == 1:
            fix = f"Change WHATSAPP_TEMPLATE_LANGUAGE to {approved[0]} (the only approved language), or add a '{language}' version of the template."
        elif approved:
            fix = "Change WHATSAPP_TEMPLATE_LANGUAGE to one of the approved languages: " + ", ".join(approved) + f", or add a '{language}' version."
        else:
            fix = f"None of its languages is approved yet. Add or approve a '{language}' version in WhatsApp Manager."
        report.add(FAIL, f"Template language '{language}' does not exist for '{name}'", "Available languages: " + listing, fix=fix)
        return
    status = by_language[language]
    if status != "APPROVED":
        other = [lang for lang in approved if lang != language]
        extra = f" Another language is approved ({', '.join(other)}): you could set WHATSAPP_TEMPLATE_LANGUAGE to it." if other else ""
        report.add(FAIL, f"Template '{name}' ({language}) is {status}, not APPROVED", fix=STATUS_HELP.get(status, "Open it in WhatsApp Manager to see why.") + extra)
        return
    report.add(PASS, f"Template '{name}' language '{language}' exists and is APPROVED")
    if len(by_language) > 1:
        report.add(INFO, "All languages of this template: " + listing)

    entry = next(t for t in same_name if t.get("language") == language)
    if "components" not in entry:
        report.add(INFO, "Meta did not list the template's parts, so its header and variables were not checked")
        return
    has_doc, variables, named = _components(entry)
    if not has_doc:
        report.add(WARN, "The template has no DOCUMENT header", fix="The app sends each payslip PDF as the template's document header. Without one, sending will fail "
                   "(error 132000 or 132012). Create a template with a DOCUMENT header.")
    if named:
        report.add(WARN, "The template uses NAMED parameters", fix="The app fills positional variables {{1}} and {{2}}. Create the template with positional variables.")
    elif variables != 2:
        report.add(WARN, f"The template body has {variables} variable(s), the app sends 2", fix="The app sends {{1}} = employee name and {{2}} = pay month. "
                   "A different number makes WhatsApp reject the message (error 132000).")
    if has_doc and not named and variables == 2:
        report.add(PASS, "The template has a DOCUMENT header and 2 body variables, as the app expects")


# ---- 4. allowed recipients ---------------------------------------------------------------------------------------

def check_allowed_recipients(env: dict[str, str], report: Report) -> None:
    items = [p.strip() for p in env.get("WHATSAPP_ALLOWED_RECIPIENTS", "").split(",") if p.strip()]
    if not items:
        report.add(WARN, "WHATSAPP_ALLOWED_RECIPIENTS is NOT set",
                   "Live mode would send to every employee in a batch.",
                   fix="While testing, set WHATSAPP_ALLOWED_RECIPIENTS to your own number in international format (a plus, then the country code and number). Clear it only after a test send works.")
        return
    bad = [i for i in items if not re.fullmatch(r"\+[1-9]\d{7,14}", i)]
    if bad:
        report.add(FAIL, f"WHATSAPP_ALLOWED_RECIPIENTS has {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} that are not valid phone numbers",
                   "The app refuses to start with an invalid list.", fix="Use international format with a plus, then digits only, separated by commas (a plus, the country code, then the number, with no spaces).")
        return
    report.add(INFO, f"WHATSAPP_ALLOWED_RECIPIENTS is set: {len(items)} number{'s' if len(items) != 1 else ''}. Live mode will send ONLY to {'it' if len(items) == 1 else 'them'}.")


# ---- running everything ------------------------------------------------------------------------------------------

def run_checks(env: dict[str, str], client: httpx.Client) -> Report:
    report = Report()
    scrub = _Scrubber(env)
    eff = check_settings(env, report)
    base = (env.get("WHATSAPP_BASE_URL", "").strip() or "https://graph.facebook.com").rstrip("/")
    version, token = eff["GRAPH_API_VERSION"], eff["WHATSAPP_ACCESS_TOKEN"]
    phone_id, waba_id = eff["WHATSAPP_PHONE_NUMBER_ID"], eff["WHATSAPP_BUSINESS_ACCOUNT_ID"]
    version_ok = bool(re.fullmatch(r"v\d+\.\d+", version))

    phone_result = "skipped"
    if token and phone_id.isdigit() and version_ok:
        phone_result = check_phone_number(client, base, version, phone_id, token, report, scrub)
    else:
        report.add(SKIP, "Token and phone number: not checked", "WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID and GRAPH_API_VERSION must be set correctly first.")

    if not (token and waba_id.isdigit() and version_ok):
        report.add(SKIP, "Templates: not checked", "WHATSAPP_ACCESS_TOKEN, WHATSAPP_BUSINESS_ACCOUNT_ID and GRAPH_API_VERSION must be set correctly first.")
    elif phone_result == "token":
        report.add(SKIP, "Templates: not checked", "Fix the access token problem above first.")
    else:
        check_templates(client, base, version, waba_id, token, eff["WHATSAPP_TEMPLATE_NAME"], eff["WHATSAPP_TEMPLATE_LANGUAGE"], report, scrub)

    check_allowed_recipients(env, report)
    return report


def render(report: Report) -> str:
    lines = ["WhatsApp live-mode preflight (no message is sent)", "=" * 50]
    for check in report.checks:
        lines.append(f"[{check.status}] {check.title}")
        lines.extend(f"       {d}" for d in check.detail)
    lines += ["", "=" * 50]
    if report.failed:
        lines.append(f"RESULT: FAIL ({len(report.failed)} problem{'s' if len(report.failed) != 1 else ''} to fix before going live)")
    else:
        lines.append("RESULT: PASS" + (f" (with {len(report.warned)} warning{'s' if len(report.warned) != 1 else ''})" if report.warned else ""))
    steps = [c for c in report.checks if c.status in (FAIL, WARN) and c.fix]
    if steps:
        lines += ["", "What to do next:"]
        for n, c in enumerate(steps, 1):
            lines.append(f" {n}. [{c.status}] {c.title}")
            lines.append(f"      {c.fix}")
    elif not report.failed:
        lines += ["", "What to do next:", " 1. Set WHATSAPP_MODE=live in .env and restart the app.",
                  " 2. Send ONE test batch to your own number first (see the README, 'Going live')."]
    if not report.failed and steps:
        lines += ["", "Warnings do not block going live, but read them first. Then: set WHATSAPP_MODE=live, restart, and send ONE test batch to your own number."]
    return "\n".join(lines)


def main(argv: list[str] | None = None, client: httpx.Client | None = None, env: dict[str, str] | None = None) -> int:
    argparse.ArgumentParser(description="Preflight check for live WhatsApp sending. Sends no message.").parse_args(argv)
    if client is None and os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("The preflight check needs a mocked HTTP client while tests run.")
    env = load_env() if env is None else env
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        report = run_checks(env, http)
    finally:
        if client is None:
            http.close()
    print(render(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
