import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from backend.workers.scheduler import WeatherScheduler
from backend.database.models import WeatherJob, WeatherRawFile, WeatherForecast, WeatherDaily

@pytest.fixture(autouse=True)
def clean_db(db_session):
    db_session.query(WeatherForecast).delete()
    db_session.query(WeatherDaily).delete()
    db_session.query(WeatherRawFile).delete()
    db_session.query(WeatherJob).delete()
    db_session.commit()

@pytest.mark.usefixtures("db_session")
@patch("backend.workers.scheduler.SessionLocal")
@patch("backend.workers.scheduler.ECMWFDownloader")
@patch("backend.workers.scheduler.WeatherProviderFactory")
def test_scheduler_run_once_both_providers(mock_factory, mock_downloader_class, mock_session_local, db_session):
    mock_session_local.return_value = db_session
    # Mock providers
    mock_ecmwf = MagicMock()
    mock_ecmwf.name = "ecmwf"
    mock_ecmwf.get_latest_available_cycle.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    mock_ecmwf.download_cycle.return_value = ("/data/ecmwf.nc", "hash1", 100)
    
    mock_imd = MagicMock()
    mock_imd.name = "imd"
    mock_imd.get_latest_available_cycle.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    mock_imd.download_cycle.return_value = ("/data/imd.nc", "hash2", 100)
    
    mock_factory.get_provider.side_effect = lambda name: mock_ecmwf if name == "ecmwf" else mock_imd
    
    # Initialize scheduler
    scheduler = WeatherScheduler(retention_days=2, concurrent=False)
    scheduler.run_once()
    
    # Check that job records were registered in DB for both providers
    jobs = db_session.query(WeatherJob).all()
    assert len(jobs) == 2
    providers_in_db = [j.provider for j in jobs]
    assert "ecmwf" in providers_in_db
    assert "imd" in providers_in_db
    
    # Check raw files were registered
    raw_files = db_session.query(WeatherRawFile).all()
    assert len(raw_files) == 2

@pytest.mark.usefixtures("db_session")
@patch("backend.workers.scheduler.os.path.exists", return_value=True)
@patch("backend.workers.scheduler.os.remove")
def test_scheduler_pruning(mock_remove, mock_exists, db_session):
    scheduler = WeatherScheduler(retention_days=2)
    
    # Pre-populate old raw files and jobs (older than 2 days)
    old_cycle = datetime.now(timezone.utc) - timedelta(days=3)
    new_cycle = datetime.now(timezone.utc) - timedelta(days=1)
    
    job_old = WeatherJob(cycle_time=old_cycle, status="SUCCESS", provider="ecmwf")
    job_new = WeatherJob(cycle_time=new_cycle, status="SUCCESS", provider="ecmwf")
    db_session.add(job_old)
    db_session.add(job_new)
    db_session.commit()
    
    raw_old = WeatherRawFile(job_id=job_old.id, cycle_time=old_cycle, file_path="/data/old.nc", file_size=100, checksum="abc", provider="ecmwf")
    raw_new = WeatherRawFile(job_id=job_new.id, cycle_time=new_cycle, file_path="/data/new.nc", file_size=100, checksum="def", provider="ecmwf")
    db_session.add(raw_old)
    db_session.add(raw_new)
    
    # Pre-populate forecast records
    forecast_old = WeatherForecast(
        latitude=19.0, longitude=73.0, forecast_time=old_cycle, cycle_time=old_cycle,
        temperature=25.0, feels_like=25.0, precipitation=0.0, humidity=60.0, pressure=1010.0,
        cloud_cover=10.0, wind_speed=2.0, wind_direction=180.0, dew_point=16.0, uv_index=5.0,
        visibility=10000.0, weather_code="CLEAR", provider="ecmwf"
    )
    forecast_new = WeatherForecast(
        latitude=19.0, longitude=73.0, forecast_time=new_cycle, cycle_time=new_cycle,
        temperature=25.0, feels_like=25.0, precipitation=0.0, humidity=60.0, pressure=1010.0,
        cloud_cover=10.0, wind_speed=2.0, wind_direction=180.0, dew_point=16.0, uv_index=5.0,
        visibility=10000.0, weather_code="CLEAR", provider="ecmwf"
    )
    db_session.add(forecast_old)
    db_session.add(forecast_new)
    db_session.commit()
    
    # Mock downloader delete method
    scheduler.downloader._delete_from_supabase = MagicMock()
    
    # Run pruning
    scheduler._prune_stale_data(db_session)
    
    # Verify database deleted old entries
    remaining_jobs = db_session.query(WeatherJob).all()
    assert len(remaining_jobs) == 1
    assert remaining_jobs[0].cycle_time == new_cycle
    
    remaining_forecasts = db_session.query(WeatherForecast).all()
    assert len(remaining_forecasts) == 1
    assert remaining_forecasts[0].forecast_time == new_cycle
    
    # Verify local file deletion was called
    mock_remove.assert_called_once_with("/data/old.nc")
    
    # Verify Supabase deletion was called
    scheduler.downloader._delete_from_supabase.assert_called_once_with("old.nc")


@pytest.mark.usefixtures("db_session")
@patch("backend.workers.scheduler.os.path.exists", return_value=True)
@patch("backend.workers.scheduler.os.remove")
def test_scheduler_pruning_calendar_days(mock_remove, mock_exists, db_session):
    # Retention days set to 2.
    scheduler = WeatherScheduler(retention_days=2)
    scheduler.downloader._delete_from_supabase = MagicMock()
    
    # Define exact times relative to start of today
    today_date = datetime.now(timezone.utc).date()
    
    today_12 = datetime.combine(today_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    yesterday_12 = datetime.combine(today_date - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    day_before_yesterday_23 = datetime.combine(today_date - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23)
    
    # Jobs
    job_today = WeatherJob(cycle_time=today_12, status="SUCCESS", provider="ecmwf")
    job_yesterday = WeatherJob(cycle_time=yesterday_12, status="SUCCESS", provider="ecmwf")
    job_day_before = WeatherJob(cycle_time=day_before_yesterday_23, status="SUCCESS", provider="ecmwf")
    
    db_session.add_all([job_today, job_yesterday, job_day_before])
    db_session.commit()
    
    # Raw Files
    raw_today = WeatherRawFile(job_id=job_today.id, cycle_time=today_12, file_path="/data/today.nc", file_size=100, checksum="abc", provider="ecmwf")
    raw_yesterday = WeatherRawFile(job_id=job_yesterday.id, cycle_time=yesterday_12, file_path="/data/yesterday.nc", file_size=100, checksum="def", provider="ecmwf")
    raw_day_before = WeatherRawFile(job_id=job_day_before.id, cycle_time=day_before_yesterday_23, file_path="/data/day_before.nc", file_size=100, checksum="ghi", provider="ecmwf")
    
    db_session.add_all([raw_today, raw_yesterday, raw_day_before])
    db_session.commit()
    
    # Run pruning
    scheduler._prune_stale_data(db_session)
    
    # Today and Yesterday should remain (2 days)
    remaining_jobs = db_session.query(WeatherJob).order_by(WeatherJob.cycle_time).all()
    assert len(remaining_jobs) == 2
    assert remaining_jobs[0].cycle_time == yesterday_12
    assert remaining_jobs[1].cycle_time == today_12
    
    # Only day before yesterday's file should be removed
    mock_remove.assert_called_once_with("/data/day_before.nc")
    scheduler.downloader._delete_from_supabase.assert_called_once_with("day_before.nc")


@pytest.mark.usefixtures("db_session")
@patch("backend.workers.scheduler.SessionLocal")
@patch("backend.workers.scheduler.ECMWFDownloader")
@patch("backend.workers.scheduler.WeatherProviderFactory")
def test_scheduler_run_once_both_providers_concurrent(mock_factory, mock_downloader_class, mock_session_local, db_session):
    import threading
    db_lock = threading.Lock()
    
    class ThreadSafeSessionProxy:
        def __init__(self, session):
            self._session = session
            
        def __getattr__(self, name):
            attr = getattr(self._session, name)
            if callable(attr):
                def wrapper(*args, **kwargs):
                    with db_lock:
                        return attr(*args, **kwargs)
                return wrapper
            return attr
            
        def close(self):
            pass
            
        def commit(self):
            with db_lock:
                self._session.commit()
                
        def add(self, instance):
            with db_lock:
                self._session.add(instance)
                
        def execute(self, statement, *args, **kwargs):
            with db_lock:
                return self._session.execute(statement, *args, **kwargs)
                
    proxy_session = ThreadSafeSessionProxy(db_session)
    mock_session_local.return_value = proxy_session
    
    # Mock providers
    mock_ecmwf = MagicMock()
    mock_ecmwf.name = "ecmwf"
    mock_ecmwf.get_latest_available_cycle.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    mock_ecmwf.download_cycle.return_value = ("/data/ecmwf.nc", "hash1", 100)
    
    mock_imd = MagicMock()
    mock_imd.name = "imd"
    mock_imd.get_latest_available_cycle.return_value = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    mock_imd.download_cycle.return_value = ("/data/imd.nc", "hash2", 100)
    
    mock_factory.get_provider.side_effect = lambda name: mock_ecmwf if name == "ecmwf" else mock_imd
    
    # Initialize scheduler with concurrent=True
    scheduler = WeatherScheduler(retention_days=2, concurrent=True)
    scheduler.run_once()
    
    # Check that job records were registered in DB for both providers
    jobs = db_session.query(WeatherJob).all()
    assert len(jobs) == 2
    providers_in_db = [j.provider for j in jobs]
    assert "ecmwf" in providers_in_db
    assert "imd" in providers_in_db


