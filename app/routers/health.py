import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """Public. Only says ok / not ok: no internal details."""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        log.error("Database health check failed: %s", type(exc).__name__)  # type only: message may hold connection details
        return JSONResponse({"status": "error"}, status_code=503)
    return {"status": "ok"}
