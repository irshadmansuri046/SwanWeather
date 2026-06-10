from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from backend import config

DATABASE_URL = config.DATABASE_URL

# Create engine
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args={"connect_timeout": 10}
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
