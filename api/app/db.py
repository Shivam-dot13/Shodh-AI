from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_conn, _record):  # type: ignore[no-untyped-def]
    with dbapi_conn.cursor() as cur:
        # A stuck statement must never pin an API worker thread forever.
        cur.execute("SET statement_timeout = '15s'")
        cur.execute("SET idle_in_transaction_session_timeout = '30s'")


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
