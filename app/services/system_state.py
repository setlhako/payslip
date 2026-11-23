"""Small facts about the running system, kept in the system_state table (never secrets)."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import SystemState
from app.models.mixins import utcnow


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(SystemState, key)
    if row is None:
        db.add(SystemState(key=key, value=value))
    else:
        row.value, row.updated_at = value, utcnow()
    db.commit()


def get_time(db: Session, key: str) -> datetime | None:
    """When this fact was last written (None if never)."""
    row = db.get(SystemState, key)
    return row.updated_at if row else None
