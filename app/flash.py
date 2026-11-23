"""One-time messages shown on the next page (stored in the session cookie, so keep them short and free of personal data)."""
from fastapi import Request

KEY = "_flash"


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session[KEY] = (request.session.get(KEY) or [])[-4:] + [[kind, message[:300]]]


def get_flashes(request: Request) -> list[list[str]]:
    return request.session.pop(KEY, None) or []
