import pytest
import os
from datetime import datetime, date, timedelta, timezone
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np

from backend.database.models import WeatherJob, WeatherRawFile, WeatherForecast, WeatherDaily
from backend.workers.pipeline import WeatherPipeline

@pytest.fixture(autouse=True)
def clean_db(db_session):
    """Ensure a clean database state before each test run by truncating tables."""
    db_session.query(WeatherForecast).delete()
    db_session.query(WeatherDaily).delete()
    db_session.query(WeatherRawFile).delete()
    db_session.query(WeatherJob).delete()
    db_session.commit()

def create_mock_grib_dataframe(cycle_time: datetime, lat: float, lon: float) -> pd.DataFrame:
    steps = [0, 3, 6]
    records = []
    
    for step in steps:
        valid_time = cycle_time + timedelta(hours=step)
        records.append({
            "valid_time": valid_time,
            "time": cycle_time,
            "latitude": lat,
            "longitude": lon,
            "temperature": 15.0,
            "dew_point": 10.0,
            "wind_speed": 3.0,
            "wind_direction": 180.0,
            "precipitation": 0.0,
            "pressure": 1013.0,
            "cloud_cover": 0.0,
            "visibility": 10000.0
        })
        
    df = pd.DataFrame(records).set_index("valid_time")
    return df

@pytest.mark.usefixtures("db_session", "mock_ecmwf_client")
def test_pipeline_cache_hit(db_session, mock_ecmwf_client):
    # Setup mock coordinate and cycle time
    lat, lon = 20.0, 75.0
    cycle_dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    
    # 1. Register successful job
    job = WeatherJob(cycle_time=cycle_dt, status="SUCCESS")
    db_session.add(job)
    
    # 2. Add forecast records to database
    forecast = WeatherForecast(
        latitude=lat,
        longitude=lon,
        forecast_time=cycle_dt,
        cycle_time=cycle_dt,
        temperature=25.0,
        feels_like=25.0,
        precipitation=0.0,
        humidity=60.0,
        pressure=1010.0,
        cloud_cover=10.0,
        wind_speed=2.0,
        wind_direction=180.0,
        dew_point=16.0,
        uv_index=5.0,
        visibility=10000.0,
        weather_code="CLEAR"
    )
    db_session.add(forecast)
    db_session.commit()
    
    # 3. Instantiate pipeline and trigger query (Cache Hit)
    pipeline = WeatherPipeline(db_session)
    result = pipeline.get_or_create_forecast(lat, lon)
    
    assert result["latitude"] == lat
    assert result["longitude"] == lon
    assert result["cycle_time"] == WeatherPipeline._format_to_ist_str(cycle_dt)
    assert result["current"]["temperature"] == 25.0
    assert result["current"]["weather_code"] == "CLEAR"

@pytest.mark.usefixtures("db_session", "mock_ecmwf_client")
def test_pipeline_cache_miss_with_local_grib(db_session, mock_ecmwf_client):
    lat, lon = 20.0, 75.0
    cycle_dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    
    # 1. Register successful job to establish target cycle time
    job = WeatherJob(cycle_time=cycle_dt, status="SUCCESS")
    db_session.add(job)
    db_session.commit()
    
    mock_df = create_mock_grib_dataframe(cycle_dt, 20.0, 75.0)
    
    # 2. Mock GRIBParser and file presence
    with patch("backend.workers.pipeline.os.path.exists", return_value=True), \
         patch("backend.workers.pipeline.GRIBParser.__init__", return_value=None), \
         patch("backend.workers.pipeline.GRIBParser.close", return_value=None), \
         patch("backend.workers.pipeline.GRIBParser.extract_point_data", return_value=mock_df):
         
        pipeline = WeatherPipeline(db_session)
        result = pipeline.get_or_create_forecast(lat, lon)
        
    assert result["latitude"] == lat
    assert result["longitude"] == lon
    assert result["cycle_time"] == WeatherPipeline._format_to_ist_str(cycle_dt)
    assert result["current"]["temperature"] == 15.0
    
    # Verify that records were written to database
    db_records = db_session.query(WeatherForecast).filter(
        WeatherForecast.latitude == lat,
        WeatherForecast.longitude == lon,
        WeatherForecast.cycle_time == cycle_dt
    ).all()
    assert len(db_records) > 0

@pytest.mark.usefixtures("db_session", "mock_ecmwf_client")
def test_pipeline_cache_miss_downloads_disabled_with_fallback(db_session, mock_ecmwf_client, monkeypatch):
    monkeypatch.setenv("ALLOW_ON_THE_FLY_DOWNLOADS", "false")
    
    lat, lon = 20.0, 75.0
    older_cycle_dt = datetime(2026, 6, 2, 6, 0, tzinfo=timezone.utc)
    latest_cycle_dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    
    # 1. Pre-populate older cycle forecast data in DB
    job_older = WeatherJob(cycle_time=older_cycle_dt, status="SUCCESS")
    db_session.add(job_older)
    
    forecast_older = WeatherForecast(
        latitude=lat,
        longitude=lon,
        forecast_time=older_cycle_dt,
        cycle_time=older_cycle_dt,
        temperature=22.0,
        feels_like=22.0,
        precipitation=0.0,
        humidity=70.0,
        pressure=1012.0,
        cloud_cover=30.0,
        wind_speed=3.0,
        wind_direction=180.0,
        dew_point=16.0,
        uv_index=4.0,
        visibility=10000.0,
        weather_code="CLEAR"
    )
    db_session.add(forecast_older)
    
    # 2. Register job for latest cycle (to establish latest cycle)
    job_latest = WeatherJob(cycle_time=latest_cycle_dt, status="SUCCESS")
    db_session.add(job_latest)
    db_session.commit()
    
    # 3. Query forecast. Since local GRIB is missing and downloads are disabled, it should fall back to older cycle.
    pipeline = WeatherPipeline(db_session)
    result = pipeline.get_or_create_forecast(lat, lon)
    
    assert result["cycle_time"] == WeatherPipeline._format_to_ist_str(older_cycle_dt)
    assert result["current"]["temperature"] == 22.0

@pytest.mark.usefixtures("db_session", "mock_ecmwf_client")
def test_pipeline_cache_miss_downloads_disabled_no_fallback(db_session, mock_ecmwf_client, monkeypatch):
    monkeypatch.setenv("ALLOW_ON_THE_FLY_DOWNLOADS", "false")
    
    lat, lon = 20.0, 75.0
    latest_cycle_dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    
    # Register job for latest cycle but do NOT populate any forecasts
    job_latest = WeatherJob(cycle_time=latest_cycle_dt, status="SUCCESS")
    db_session.add(job_latest)
    db_session.commit()
    
    # Query forecast. Downloads are disabled and no older cycle forecast exists in the database.
    # It should raise RuntimeError.
    pipeline = WeatherPipeline(db_session)
    with pytest.raises(RuntimeError) as exc_info:
        pipeline.get_or_create_forecast(lat, lon)
        
    assert "On-the-fly downloads are disabled" in str(exc_info.value)
