"""Database connection (SQLAlchemy 2.x) - works with MySQL or MariaDB."""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """All models inherit from this."""


def _make_engine():
    return create_engine(
        get_settings().database_url.get_secret_value(),
        pool_pre_ping=True,   # silently reconnect if MySQL dropped an idle connection
        pool_recycle=1800,
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: gives a request its own DB session, always closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
