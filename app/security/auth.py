"""The one reusable "you must be logged in" dependency."""
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security.sessions import get_session_user


class LoginRequired(Exception):
    """Raised when a page needs a login; main.py turns it into a redirect to /login."""


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    """Use on every protected router: APIRouter(dependencies=[Depends(require_login)]).
    Can also be taken as a parameter (`user: User = Depends(require_login)`) to get the current user."""
    user = get_session_user(request, db)
    if user is None:
        raise LoginRequired()
    return user
