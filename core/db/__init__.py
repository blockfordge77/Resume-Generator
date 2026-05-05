from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


_engines: dict[Path, Engine] = {}
_sessionmakers: dict[Path, sessionmaker] = {}
_lock = RLock()


def _enable_sqlite_pragmas(dbapi_conn, _conn_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA foreign_keys=ON')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.close()


def get_engine(db_path: Path) -> Engine:
    db_path = db_path.resolve()
    with _lock:
        engine = _engines.get(db_path)
        if engine is not None:
            return engine
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f'sqlite:///{db_path}',
            future=True,
            connect_args={'check_same_thread': False, 'timeout': 30},
        )
        event.listen(engine, 'connect', _enable_sqlite_pragmas)
        Base.metadata.create_all(engine)
        _engines[db_path] = engine
        _sessionmakers[db_path] = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        return engine


def get_sessionmaker(db_path: Path) -> sessionmaker:
    get_engine(db_path)
    return _sessionmakers[db_path.resolve()]


@contextmanager
def session_scope(db_path: Path) -> Iterator[Session]:
    factory = get_sessionmaker(db_path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
