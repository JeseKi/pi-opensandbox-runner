from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import ManagerSettings


class Base(DeclarativeBase):
    pass


class ManagerDatabase:
    def __init__(self, settings: ManagerSettings):
        path = settings.sqlite_path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(
            settings.database_url,
            connect_args=connect_args if path is not None else {},
            pool_pre_ping=True,
        )
        if path is not None:
            _configure_sqlite(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )

    def initialize(self) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
        config.set_main_option("sqlalchemy.url", str(self.engine.url))
        config.attributes["connection"] = self.engine
        command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_pragmas(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
