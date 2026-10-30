"""Engine / session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import PROJECT_ROOT
from database.models import Base


class Database:
    """Thin wrapper around an SQLAlchemy engine + session factory."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = self._resolve_sqlite_path(url)
        kwargs: dict = {"echo": echo, "future": True}
        if self.url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in self.url or self.url in {"sqlite://", "sqlite:///"}:
                kwargs["poolclass"] = StaticPool
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(self.url, **kwargs)
        if self.url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _resolve_sqlite_path(url: str) -> str:
        """Make relative SQLite paths relative to the project root and create the folder."""
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
            return url
        path = Path(parsed.database)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(parsed.set(database=str(path)))

    def create_all(self) -> None:
        """Create all tables (quick start). Use Alembic migrations for production."""
        Base.metadata.create_all(self.engine)
        logger.info("Database tables ensured at {}", self.engine.url.render_as_string(hide_password=True))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, rollback on error."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
