from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import settings


database_url = settings.database_url
is_sqlite = database_url.startswith('sqlite:')
engine_kwargs = {'pool_pre_ping': not is_sqlite}
if is_sqlite:
    engine_kwargs['connect_args'] = {'check_same_thread': False}
engine = create_engine(database_url, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database() -> bool:
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    return True
