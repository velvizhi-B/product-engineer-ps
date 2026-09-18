from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

# check_same_thread only matters for the SQLite path used in tests.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables. Fine for this exercise; a real deployment would use
    Alembic migrations instead of create_all()."""
    from app import models  # noqa: F401 (ensure models are registered)
    Base.metadata.create_all(bind=engine)
