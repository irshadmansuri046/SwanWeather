import sys
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Configure logging to show all logs and tracebacks
logging.basicConfig(level=logging.INFO)

from backend.workers.scheduler import WeatherScheduler
from backend.database.models import WeatherJob
from backend.database.connection import SessionLocal, engine, Base

# Set up test DB
Base.metadata.create_all(bind=engine)
db_session = SessionLocal()

mock_ecmwf = MagicMock()
mock_ecmwf.name = "ecmwf"
mock_ecmwf.get_latest_available_cycle.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
mock_ecmwf.download_cycle.return_value = ("/data/ecmwf.nc", "hash1", 100)

mock_imd = MagicMock()
mock_imd.name = "imd"
mock_imd.get_latest_available_cycle.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
mock_imd.download_cycle.return_value = ("/data/imd.nc", "hash2", 100)

@patch("backend.workers.scheduler.SessionLocal", return_value=db_session)
@patch("backend.workers.scheduler.ECMWFDownloader")
@patch("backend.workers.scheduler.WeatherProviderFactory")
def run_test(mock_factory, mock_downloader_class, mock_session_local):
    mock_factory.get_provider.side_effect = lambda name: mock_ecmwf if name == "ecmwf" else mock_imd
    
    scheduler = WeatherScheduler(retention_days=2, concurrent=False)
    scheduler.run_once()

try:
    run_test()
finally:
    db_session.close()
