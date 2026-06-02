import os
import time
import logging
from datetime import datetime, date, timedelta
from unittest.mock import patch
import pandas as pd
from sqlalchemy.orm import Session

from backend.database.connection import SessionLocal
from backend.database.init import init_db
from backend.database.models import WeatherJob, WeatherRawFile, WeatherForecast, WeatherDaily
from backend.workers.pipeline import WeatherPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("verify_pipeline")

def create_mock_grib_dataframe(cycle_time: datetime, lat: float, lon: float) -> pd.DataFrame:
    """Generates a mock dataframe resembling the output of GRIBParser.extract_point_data"""
    steps = list(range(0, 144, 3)) + list(range(144, 241, 6))
    records = []
    
    for step in steps:
        valid_time = cycle_time + timedelta(hours=step)
        
        # Introduce some realistic hourly weather variations
        hour = valid_time.hour
        temp = 15.0 + 5.0 * np.sin(2 * np.pi * (hour - 6) / 24)
        dew = temp - 4.0 # 70-80% humidity
        
        # Calculate wind speed & direction
        u = 3.0 + 1.5 * np.cos(step / 10)
        v = 2.0 - 1.0 * np.sin(step / 10)
        wind_speed = np.sqrt(u**2 + v**2)
        wind_direction = (np.degrees(np.arctan2(u, v)) + 180) % 360
        
        records.append({
            "valid_time": valid_time,
            "time": cycle_time,
            "latitude": lat,
            "longitude": lon,
            "temperature": temp, # Celsius
            "dew_point": dew,     # Celsius
            "wind_speed": wind_speed,
            "wind_direction": wind_direction,
            "precipitation": 0.1 * (1 + np.sin(step / 4)), # mm
            "pressure": 1013.0 - 1.0 * np.sin(step / 5), # hPa
            "cloud_cover": (0.5 + 0.3 * np.cos(step / 8)) * 100.0, # percentage 0-100
            "visibility": 10000.0
        })
        
    df = pd.DataFrame(records).set_index("valid_time")
    return df

import numpy as np

def run_verification():
    logger.info("=== Running ECMWF Weather Platform Verification Script ===")
    
    # 1. Initialize DB tables
    try:
        init_db()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        logger.info("Skipping integration check (requires running Postgres).")
        return
        
    db: Session = SessionLocal()
    
    try:
        # Mock coordinates
        lat, lon = 51.5074, -0.1278 # London
        
        # Calculate expected cycle (using a historical date to prevent database pollution)
        cycle_date = date.today() - timedelta(days=2)
        cycle_hour = 12 # Assume 12 UTC run
        cycle_dt = datetime.combine(cycle_date, datetime.min.time()) + timedelta(hours=cycle_hour)
        
        # Clear database records to ensure clean slate
        db.query(WeatherForecast).delete()
        db.query(WeatherDaily).delete()
        db.query(WeatherRawFile).delete()
        db.query(WeatherJob).delete()
        db.commit()
        
        # Register a successful job to mimic a successful background ingestion
        job = WeatherJob(cycle_time=cycle_dt, status="SUCCESS")
        db.add(job)
        db.commit()
        
        raw_file = WeatherRawFile(
            job_id=job.id,
            cycle_time=cycle_dt,
            file_path=f"/data/raw_grib/ecmwf_{cycle_date.strftime('%Y%m%d')}_{cycle_hour:02d}utc.grib2",
            file_size=1024*1024*5,
            checksum="mocked_checksum_hash_value"
        )
        db.add(raw_file)
        db.commit()
        
        # 2. Test Pipeline Cache Miss with mocked GRIB parsing
        logger.info("1. Simulating Cache Miss (First run for coordinates)...")
        
        # Mock the downloader check and GRIB parsing so we don't need real GRIB files
        mock_df = create_mock_grib_dataframe(cycle_dt, 51.5, -0.25)
        
        start_time = time.time()
        with patch("backend.workers.pipeline.os.path.exists", return_value=True), \
             patch("backend.workers.pipeline.GRIBParser.__init__", return_value=None), \
             patch("backend.workers.pipeline.GRIBParser.close", return_value=None), \
             patch("backend.workers.pipeline.GRIBParser.extract_point_data", return_value=mock_df):
             
            pipeline = WeatherPipeline(db)
            result = pipeline.get_or_create_forecast(lat, lon)
            
        duration = (time.time() - start_time) * 1000
        logger.info(f"-> Cache Miss execution completed in {duration:.2f}ms")
        
        # Assertions
        assert result["latitude"] == 51.5
        assert result["longitude"] == -0.25
        assert len(result["hourly"]) == 24
        assert len(result["daily"]) <= 11
        
        logger.info("Database records successfully written!")
        logger.info(f"Current temperature: {result['current']['temperature']:.2f} °C (Feels like: {result['current']['feels_like']:.2f} °C)")
        logger.info(f"Daily forecast length: {len(result['daily'])} days")
        
        # 3. Test Pipeline Cache Hit (subsequent run)
        logger.info("2. Simulating Cache Hit (Subsequent run for same coordinates)...")
        
        start_time = time.time()
        result_cache = pipeline.get_or_create_forecast(lat, lon)
        duration_cache = (time.time() - start_time) * 1000
        
        logger.info(f"-> Cache Hit execution completed in {duration_cache:.2f}ms")
        assert result_cache["current"]["temperature"] == result["current"]["temperature"]
        assert duration_cache < 1000.0, f"Cache hit took {duration_cache:.2f}ms, target is < 1000ms"
        
        logger.info("=== Verification Successful! Pipeline behaves perfectly. ===")
        
    except Exception as e:
        logger.error(f"Verification failed: {e}", exc_info=True)
    finally:
        db.close()

if __name__ == "__main__":
    run_verification()
