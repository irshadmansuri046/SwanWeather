from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from backend import config

DATABASE_URL = config.DATABASE_URL

is_sqlite = DATABASE_URL.startswith("sqlite")

engine_kwargs = {
    "pool_pre_ping": True
}

if is_sqlite:
    # SQLite configuration
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL configuration
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20
    engine_kwargs["connect_args"] = {"connect_timeout": 10}

engine = create_engine(
    DATABASE_URL,
    **engine_kwargs
)

# Create SessionLocal class for database sessions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for ORM models
Base = declarative_base()

def get_db():
    """Dependency for getting DB session in FastAPI endpoints."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
