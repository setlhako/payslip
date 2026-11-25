import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.config import get_settings
from app.flash import get_flashes
from app.security.csrf import csrf_input
from app.security.permissions import ROLE_LABELS, can
from app.services.ids import FULL_MASK
from app.services.names import name_words

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def icon(name: str, extra_class: str = "") -> Markup:
    """An icon from the self-hosted sprite app/static/icons.svg. It takes the text colour (currentColor)."""
    return Markup(
        '<svg class="icon {}" aria-hidden="true" focusable="false"><use href="/static/icons.svg#i-{}"></use></svg>'
    ).format(escape(extra_class), escape(name))


STATIC_DIR = Path(__file__).resolve().parent / "static"
_static_versions: dict[str, tuple[float, str]] = {}


def static_url(name: str) -> str:
    """/static/<name>?v=<hash of the file>. When the file changes the address changes, so a browser can never keep
    using an old app.js or style.css (an old script would not know about newer buttons)."""
    path = STATIC_DIR / name
    try:
        mtime = path.stat().st_mtime
        cached = _static_versions.get(name)
        if cached is None or cached[0] != mtime:
            cached = (mtime, hashlib.sha256(path.read_bytes()).hexdigest()[:10])
            _static_versions[name] = cached
        return f"/static/{name}?v={cached[1]}"
    except OSError:
        return f"/static/{name}"


BADGE_ICONS = {
    "warning": "alert", "error": "x-circle",
    "needs_review": "alert", "possible_mismatch": "alert", "unmatched": "x-circle", "invalid": "x-circle",
    "failed": "x-circle", "uncertain": "alert",
}


def badge(kind: str, label: str) -> Markup:
    """A coloured status pill. kind: active, inactive, new, update, unchanged, warning, error, and the batch file
    states exact, confirmed, needs_review, possible_mismatch, unmatched, invalid, pending."""
    glyph = Markup(icon(BADGE_ICONS[kind])) if kind in BADGE_ICONS else Markup('<span class="dot" aria-hidden="true"></span>')
    return Markup('<span class="badge badge-{}">{}{}</span>').format(escape(kind), glyph, escape(label))


def initials(first_name: str, surname: str) -> str:
    a = name_words(first_name)
    b = name_words(surname)
    return ((a[0][0] if a else "") + (b[0][0] if b else "")).upper() or "?"


def avatar(first_name: str, surname: str, key: int = 0, size: str = "") -> Markup:
    """A round initials badge. The colour is picked from the id so each person keeps the same one."""
    return Markup('<span class="avatar av-{} {}" aria-hidden="true">{}</span>').format(
        key % 6, escape(size), initials(first_name, surname)
    )


templates.env.globals.update(
    csrf_input=csrf_input,  # use {{ csrf_input(request) }} inside every <form method="post">
    get_flashes=get_flashes,
    icon=icon,
    badge=badge,
    avatar=avatar,
    static_url=static_url,
    can=can,                                     # {% if can(current_user, 'employees.edit') %} ... (hides buttons; the SERVER still enforces)
    role_label=lambda role: ROLE_LABELS.get(role, role),
    live_whatsapp_mode=lambda: get_settings().whatsapp_mode,   # "mock" or "live" (read each time, never cached)
    session_idle_seconds=lambda: get_settings().session_idle_minutes * 60,   # for the inactivity warning (static/session.js)
    id_mask=FULL_MASK,  # the dots shown instead of a full ID number
)
