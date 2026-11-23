"""Plain-language explanations for WhatsApp / Meta error codes, checked against Meta's error-codes page (Nov 2026).

Meta advises handling errors by `code` (titles may change). Unknown codes are shown with the raw code and message.
Our own pre-send checks use LOCAL_... codes. Nothing here contains personal data; digits runs are masked in raw messages.
"""
import re

# ---- own (local) codes ----------------------------------------------------------------------------------------
LOCAL_NOT_ALLOWED = "LOCAL_NOT_ALLOWED"
LOCAL_NO_PASSWORD = "LOCAL_NO_PASSWORD"
LOCAL_NO_PHONE = "LOCAL_NO_PHONE"
LOCAL_FILE_MISSING = "LOCAL_FILE_MISSING"
LOCAL_PROTECT = "LOCAL_PROTECT"
LOCAL_TIMEOUT = "LOCAL_TIMEOUT"
LOCAL_RESTART = "LOCAL_RESTART"
LOCAL_UNKNOWN = "LOCAL_UNKNOWN"
LOCAL_NOT_READY = "LOCAL_NOT_READY"

TOKEN_CODES = {"190", "0", "102", "3", "10", "131005", "200"}          # the access token / permissions: stop the whole batch
RATE_LIMIT_CODES = {"130429", "80007", "4", "131056"}                  # throttling: wait and retry
TRANSIENT_CODES = RATE_LIMIT_CODES | {"131016", "133004", "2"}         # temporary problems on Meta's side

_TEXT = {
    LOCAL_NOT_ALLOWED: "This number is not on the allowed recipients list (test mode), so nothing was sent. "
                       "Add it to WHATSAPP_ALLOWED_RECIPIENTS in .env, or switch test mode off.",
    LOCAL_NO_PASSWORD: "This employee has no ID number, so the PDF password cannot be set. Nothing was sent.",
    LOCAL_NO_PHONE: "This employee has no phone number. Nothing was sent.",
    LOCAL_FILE_MISSING: "The stored PDF is no longer available (it may have been deleted after the retention period).",
    LOCAL_PROTECT: "The PDF could not be password-protected safely, so nothing was sent.",
    LOCAL_TIMEOUT: "WhatsApp did not answer in time. The payslip may or may not have been sent: check with the employee "
                   "before sending it again, because sending again could deliver it twice.",
    LOCAL_RESTART: "The app was restarted while this payslip was being sent. It may or may not have been delivered: check "
                   "with the employee before sending again, because sending again could deliver it twice.",
    LOCAL_NOT_READY: "This employee is no longer ready (inactive, or missing a phone number or ID). Nothing was sent.",
    "131026": "WhatsApp could not deliver this message. The number may not be on WhatsApp, or the person has not accepted "
              "WhatsApp's latest terms or uses an old version. Check the number with the employee.",
    "131030": "This number is not in the allowed list for this WhatsApp test number. Add it as a test recipient in Meta's "
              "dashboard, or use the live number.",
    "132000": "The WhatsApp template does not match what the app sends (the number of parameters is wrong). Check the "
              "template in WhatsApp Manager: it needs the document header and two body variables.",
    "132012": "The WhatsApp template parameters have the wrong format. Check the template in WhatsApp Manager.",
    "132001": "The WhatsApp template was not found in this language, or it has not been approved. Check "
              "WHATSAPP_TEMPLATE_NAME and WHATSAPP_TEMPLATE_LANGUAGE, and the template's approval status.",
    "132005": "The translated template text is too long. Check the template in WhatsApp Manager.",
    "132007": "The template content breaks a WhatsApp policy. Check its review status in WhatsApp Manager.",
    "132015": "The template is paused because of low quality, so it cannot be sent right now.",
    "132016": "The template was paused too many times and is now permanently disabled. Create a new template.",
    "130429": "WhatsApp's sending limit was reached. The app waited and tried again, but it still failed. Wait a few minutes "
              "and press Retry failed.",
    "80007": "The WhatsApp account's rate limit was reached. Wait a while, then press Retry failed.",
    "4": "The app's WhatsApp request limit was reached. Wait a while, then press Retry failed.",
    "131056": "Too many messages were sent to this same number in a short time. Wait a little, then retry this payslip.",
    "131048": "WhatsApp is limiting messages from this number because of spam reports or quality. Check the number's "
              "quality in WhatsApp Manager.",
    "190": "WhatsApp rejected the access token (it has expired or is invalid). Sending was stopped. Create a new token, "
           "put it in WHATSAPP_ACCESS_TOKEN, restart the app, then press Resume.",
    "0": "WhatsApp could not authenticate the app (the access token is invalid). Sending was stopped. Check "
         "WHATSAPP_ACCESS_TOKEN, then press Resume.",
    "102": "The WhatsApp session expired (access token). Sending was stopped. Create a new token, then press Resume.",
    "3": "The access token does not have the needed WhatsApp permission. Sending was stopped. Check the token's permissions.",
    "10": "WhatsApp permission was not granted or was removed. Sending was stopped. Check the token's permissions.",
    "131005": "WhatsApp permission was not granted or was removed. Sending was stopped. Check the token's permissions.",
    "200": "No access token was accepted by WhatsApp. Sending was stopped. Check WHATSAPP_ACCESS_TOKEN.",
    "131047": "More than 24 hours have passed since this person last messaged you, so a plain document message cannot be "
              "sent. Use the template message mode (WHATSAPP_SEND_KIND=template).",
    "131049": "WhatsApp chose not to deliver this message to keep recipients' experience healthy. Try again later; retrying "
              "does not guarantee delivery.",
    "131050": "This person chose to stop receiving messages from your business. Do not retry.",
    "131021": "The sender and the recipient number are the same.",
    "131031": "The WhatsApp business account is restricted or disabled. Check WhatsApp Manager.",
    "368": "The WhatsApp business account is restricted or disabled for a policy violation. Check WhatsApp Manager.",
    "131037": "The WhatsApp sending number does not have an approved display name yet.",
    "131042": "There is a problem with the WhatsApp payment method. Check billing in WhatsApp Manager.",
    "131045": "The WhatsApp sending number is not properly registered.",
    "133010": "The WhatsApp sending number is not registered on the WhatsApp Business Platform.",
    "131053": "WhatsApp could not accept the uploaded PDF. Check that it is a valid PDF.",
    "131000": "WhatsApp reported an unknown error. Press Retry on this payslip; if it keeps happening, check WhatsApp's status.",
    "131016": "WhatsApp had a temporary service problem. Try again in a few minutes.",
    "133004": "WhatsApp had a temporary service problem. Try again in a few minutes.",
    "131008": "WhatsApp says a required value was missing in the request.",
    "131009": "WhatsApp says a value in the request was not valid.",
    "100": "WhatsApp says a value in the request was not valid or was misspelled.",
}

_DIGITS = re.compile(r"\d{7,}")


def clean_error_text(text: str | None, limit: int = 500) -> str | None:
    """Raw provider text for storage: long digit runs (could be phone numbers) are masked."""
    if not text:
        return None
    return _DIGITS.sub("•••", " ".join(str(text).split()))[:limit]


def explain(code: str | None, raw_message: str | None = None) -> str:
    """The sentence HR sees. Unknown codes show the raw code and message."""
    if not code and not raw_message:
        return ""
    key = str(code) if code else ""
    if key in _TEXT:
        return _TEXT[key]
    raw = clean_error_text(raw_message, 200) or "no details"
    return f"WhatsApp returned error {key or 'unknown'}: {raw}"
