"""The inactivity warning's two small endpoints (used by static/session.js). Login + CSRF like everything else.

GET  /session/status     how many seconds are left. It does NOT count as activity (sessions.NO_TOUCH_PATHS).
POST /session/keepalive  "Stay signed in": any authenticated request refreshes the idle clock; this one just says so.
Neither returns anything personal: one number.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.security.permissions import require
from app.security.sessions import seconds_left

router = APIRouter(prefix="/session", dependencies=[Depends(require("account.self"))])
NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@router.get("/status")
def status(request: Request):
    return JSONResponse({"remaining": seconds_left(request)}, headers=NO_STORE)


@router.post("/keepalive")
def keepalive(request: Request):
    return JSONResponse({"remaining": seconds_left(request)}, headers=NO_STORE)
