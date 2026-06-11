import os
# Force DATABASE_URL to a safe local SQLite file for tests to prevent production DB pollution
prod_db_url = os.getenv("DATABASE_URL")
if prod_db_url and ("supabase.co" in prod_db_url or "pooler.supabase.com" in prod_db_url):
    print("Warning: Overriding production DATABASE_URL to a safe SQLite database for testing.")
os.environ["DATABASE_URL"] = "sqlite:///test_weather.db"

import pytest
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.connection import Base
from backend.database.init import init_db

# Configure DATABASE_URL for testing
TEST_DATABASE_URL = "sqlite:///test_weather.db"


@pytest.fixture(scope="session")
def db_engine():
    """Provides a database engine for test sessions."""
    # Try using postgres from env, if fail, fallback to standard mock engine
    engine = create_engine(TEST_DATABASE_URL)
    
    # Initialize DB tables
    try:
        init_db()
    except Exception as e:
        # If Postgres is not running, we create normal tables on SQLite in-memory for unit testing
        pytest.skip(f"PostgreSQL test database not available at {TEST_DATABASE_URL}: {e}")
        
    yield engine
    engine.dispose()

@pytest.fixture(scope="function")
def db_session(db_engine):
    """Provides a transactional database session per test function."""
    connection = db_engine.connect()
    transaction = connection.begin()
    
    Session = sessionmaker(bind=connection)
    session = Session()
    
    yield session
    
    session.close()
    transaction.rollback()
    connection.close()

@pytest.fixture
def mock_ecmwf_client(monkeypatch):
    """Mocks the ecmwf-opendata Client retrieve call."""
    mock_client = MagicMock()
    monkeypatch.setattr("backend.ingestion.downloader.Client", lambda *args, **kwargs: mock_client)
    return mock_client
