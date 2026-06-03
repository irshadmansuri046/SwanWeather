import os
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, delete

from backend.database.models import WeatherJob, WeatherRawFile, WeatherForecast, WeatherDaily
from backend.ingestion.downloader import ECMWFDownloader
from backend.ingestion.parser import GRIBParser
from backend.workers.transform import WeatherComputation

logger = logging.getLogger(__name__)

# Severity mapping for choosing dominant daily weather code
WEATHER_SEVERITY = {
    "THUNDER": 8,
    "RAIN": 7,
    "SNOW": 6,
    "FOG": 5,
    "WINDY": 4,
    "CLOUDY": 3,
    "PARTLY_CLOUDY": 2,
    "CLEAR": 1
}

class WeatherPipeline:
    """
    Orchestrates the ingestion, GRIB parsing, transformations,
    and database storage.
    """
    
    def __init__(self, db: Session, storage_dir: Optional[str] = None):
        self.db = db
        self.downloader = ECMWFDownloader(storage_dir=storage_dir)

    def get_latest_successful_cycle(self) -> Optional[datetime]:
        """
        Queries the database to find the latest successfully processed cycle time.
        Falls back to detecting from the downloader if database is empty.
        """
        # Query database for latest successful job
        stmt = select(WeatherJob).where(WeatherJob.status == "SUCCESS").order_by(WeatherJob.cycle_time.desc()).limit(1)
        job = self.db.execute(stmt).scalars().first()
        if job:
            if job.cycle_time.tzinfo is None:
                return job.cycle_time.replace(tzinfo=timezone.utc)
            return job.cycle_time
        return None

    def get_or_create_forecast(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        Main query handler. Finds coordinates in DB or extracts from GRIB on-the-fly.
        """
        # Round latitude/longitude to 2 decimal places to match ECMWF grid point roundings
        # ECMWF Open Data is usually on a 0.25 degree grid (e.g. 51.25, 51.50, 51.75)
        # We round to the nearest 0.25 degrees to hit the grid points perfectly.
        lat_grid = round(lat * 4) / 4
        lon_grid = round(lon * 4) / 4
        
        # 1. Determine target cycle time
        latest_cycle_dt = self.get_latest_successful_cycle()
        
        # If no cycle has run, calculate what should be available and trigger download
        if not latest_cycle_dt:
            run_date, run_hour = self.downloader.get_latest_available_cycle()
            latest_cycle_dt = datetime.combine(run_date, datetime.min.time()) + timedelta(hours=run_hour)
            latest_cycle_dt = latest_cycle_dt.replace(tzinfo=timezone.utc)
            
        logger.info(f"Checking database for grid point ({lat_grid}, {lon_grid}) at cycle {latest_cycle_dt}")
        
        # 2. Check if we already have forecast data in the DB for this coordinate and cycle
        hourly_stmt = select(WeatherForecast).where(
            and_(
                WeatherForecast.latitude == lat_grid,
                WeatherForecast.longitude == lon_grid,
                WeatherForecast.cycle_time == latest_cycle_dt
            )
        ).order_by(WeatherForecast.forecast_time.asc())
        
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()
        
        if db_forecasts:
            logger.info(f"Database cache HIT for grid point ({lat_grid}, {lon_grid})")
            return self._build_response_from_db(lat_grid, lon_grid, latest_cycle_dt, db_forecasts)
            
        # 3. Cache Miss: Extract from GRIB file
        logger.info(f"Database cache MISS for grid point ({lat_grid}, {lon_grid}). Extracting from GRIB...")
        
        # Ensure we have the latest GRIB file
        target_path = self.downloader.get_target_path(latest_cycle_dt.date(), latest_cycle_dt.hour)
        
        if not os.path.exists(target_path):
            logger.info(f"GRIB file not found locally. Downloading cycle {latest_cycle_dt}...")
            # Register job in DB
            job = WeatherJob(cycle_time=latest_cycle_dt, status="RUNNING")
            self.db.add(job)
            self.db.commit()
            
            try:
                path, checksum, size = self.downloader.download_cycle(latest_cycle_dt.date(), latest_cycle_dt.hour)
                # Register raw file
                raw_file = WeatherRawFile(
                    job_id=job.id,
                    cycle_time=latest_cycle_dt,
                    file_path=path,
                    file_size=size,
                    checksum=checksum
                )
                self.db.add(raw_file)
                job.status = "SUCCESS"
                self.db.commit()
            except Exception as e:
                job.status = "FAILED"
                job.error_message = str(e)
                self.db.commit()
                raise RuntimeError(f"Could not download GRIB file for cycle {latest_cycle_dt}: {e}")
                
        # Parse GRIB file and extract data for this coordinate
        parser = GRIBParser(target_path)
        try:
            raw_df = parser.extract_point_data(lat_grid, lon_grid)
            # Interpolate to hourly
            hourly_df = parser.interpolate_to_hourly(raw_df)
        finally:
            parser.close()
            
        # Run derived weather computations
        hourly_records = []
        for _, row in hourly_df.iterrows():
            f_time = row["forecast_time"].to_pydatetime().replace(tzinfo=timezone.utc)
            temp = float(row["temperature"])
            dew = float(row["dew_point"])
            wind = float(row["wind_speed"])
            vis = float(row["visibility"])
            cloud = float(row["cloud_cover"])
            precip = float(row["precipitation"])
            
            humidity = WeatherComputation.compute_relative_humidity(temp, dew)
            feels_like = WeatherComputation.calculate_feels_like(temp, humidity, wind)
            uv = WeatherComputation.estimate_uv_index(lat_grid, lon_grid, f_time, cloud)
            weather_code = WeatherComputation.get_weather_code(temp, precip, cloud, vis, wind)
            
            hourly_records.append({
                "latitude": lat_grid,
                "longitude": lon_grid,
                "forecast_time": f_time,
                "cycle_time": latest_cycle_dt,
                "temperature": temp,
                "feels_like": feels_like,
                "precipitation": precip,
                "humidity": humidity,
                "pressure": float(row["pressure"]),
                "cloud_cover": cloud,
                "wind_speed": wind,
                "wind_direction": float(row["wind_direction"]),
                "dew_point": dew,
                "uv_index": uv,
                "visibility": vis,
                "weather_code": weather_code
            })
            
        # Save hourly records to Database
        # Clean up any stale forecasts for this grid point (from old cycles) to prevent disk waste
        self.db.execute(
            delete(WeatherForecast).where(
                and_(
                    WeatherForecast.latitude == lat_grid,
                    WeatherForecast.longitude == lon_grid
                )
            )
        )
        
        db_hourly_objs = [WeatherForecast(**rec) for rec in hourly_records]
        self.db.bulk_save_objects(db_hourly_objs)
        
        # 4. Generate & Save Daily aggregates
        daily_records = self._generate_daily_summaries(hourly_records, lat_grid, lon_grid, latest_cycle_dt)
        
        self.db.execute(
            delete(WeatherDaily).where(
                and_(
                    WeatherDaily.latitude == lat_grid,
                    WeatherDaily.longitude == lon_grid
                )
            )
        )
        db_daily_objs = [WeatherDaily(**rec) for rec in daily_records]
        self.db.bulk_save_objects(db_daily_objs)
        
        self.db.commit()
        
        # Return response
        return self._build_response_from_db(lat_grid, lon_grid, latest_cycle_dt, db_hourly_objs)

    def _generate_daily_summaries(self, hourly_records: List[Dict[str, Any]], lat: float, lon: float, cycle_time: datetime) -> List[Dict[str, Any]]:
        """
        Aggregates hourly records into 10-day daily forecasts.
        """
        df = pd.DataFrame(hourly_records)
        df["date"] = df["forecast_time"].dt.date
        
        daily_groups = df.groupby("date")
        daily_records = []
        
        for forecast_date, group in daily_groups:
            # Weather Code: Dominant condition based on severity hierarchy
            # E.g. If it rains, the daily weather code should be RAIN rather than CLEAR
            codes = group["weather_code"].tolist()
            dominant_code = max(codes, key=lambda c: WEATHER_SEVERITY.get(c, 0))
            
            daily_records.append({
                "latitude": lat,
                "longitude": lon,
                "forecast_date": forecast_date,
                "cycle_time": cycle_time,
                "temp_min": float(group["temperature"].min()),
                "temp_max": float(group["temperature"].max()),
                "precipitation": float(group["precipitation"].sum()),
                "humidity_avg": float(group["humidity"].mean()),
                "wind_speed_max": float(group["wind_speed"].max()),
                "weather_code": dominant_code
            })
            
        return daily_records

    def _build_response_from_db(self, lat: float, lon: float, cycle_time: datetime, db_forecasts: List[WeatherForecast]) -> Dict[str, Any]:
        """
        Builds the structured API dictionary from database records.
        """
        # Fetch daily summaries
        daily_stmt = select(WeatherDaily).where(
            and_(
                WeatherDaily.latitude == lat,
                WeatherDaily.longitude == lon,
                WeatherDaily.cycle_time == cycle_time
            )
        ).order_by(WeatherDaily.forecast_date.asc())
        
        db_dailies = self.db.execute(daily_stmt).scalars().all()
        
        # Determine "current" weather point: nearest hourly forecast point in the future or current hour
        now_utc = datetime.now(timezone.utc)
        current_pt = None
        
        # Sort by proximity to now
        time_diffs = [abs((pt.forecast_time - now_utc).total_seconds()) for pt in db_forecasts]
        if time_diffs:
            min_idx = np.argmin(time_diffs)
            current_pt = db_forecasts[min_idx]
        else:
            current_pt = db_forecasts[0]
            
        return {
            "latitude": lat,
            "longitude": lon,
            "cycle_time": cycle_time,
            "current": {
                "temperature": float(current_pt.temperature),
                "feels_like": float(current_pt.feels_like),
                "humidity": float(current_pt.humidity),
                "pressure": float(current_pt.pressure),
                "cloud_cover": float(current_pt.cloud_cover),
                "wind_speed": float(current_pt.wind_speed),
                "wind_direction": float(current_pt.wind_direction),
                "dew_point": float(current_pt.dew_point),
                "uv_index": float(current_pt.uv_index),
                "visibility": float(current_pt.visibility),
                "weather_code": current_pt.weather_code,
                "valid_time": current_pt.forecast_time
            },
            "hourly": [
                {
                    "temperature": float(pt.temperature),
                    "feels_like": float(pt.feels_like),
                    "precipitation": float(pt.precipitation),
                    "humidity": float(pt.humidity),
                    "pressure": float(pt.pressure),
                    "cloud_cover": float(pt.cloud_cover),
                    "wind_speed": float(pt.wind_speed),
                    "wind_direction": float(pt.wind_direction),
                    "weather_code": pt.weather_code,
                    "forecast_time": pt.forecast_time
                }
                for pt in db_forecasts[:24]  # Return next 24 hours of hourly data
            ],
            "daily": [
                {
                    "forecast_date": d.forecast_date,
                    "temp_min": float(d.temp_min),
                    "temp_max": float(d.temp_max),
                    "precipitation": float(d.precipitation),
                    "humidity_avg": float(d.humidity_avg),
                    "wind_speed_max": float(d.wind_speed_max),
                    "weather_code": d.weather_code
                }
                for d in db_dailies[:10]  # Return up to 10 days
            ]
        }
