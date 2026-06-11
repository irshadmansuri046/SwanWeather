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
from backend.workers import calculations
from backend.workers.providers.factory import WeatherProviderFactory
from backend import config

logger = logging.getLogger(__name__)

# Severity mapping for choosing dominant daily weather code
WEATHER_SEVERITY = {
    "HEAVY_RAIN": 6,
    "RAIN": 5,
    "SHOWER": 4,
    "HEAVY_SNOW": 3,
    "SNOW": 2,
    "STORM": 1,
    "PARTLY_CLOUDY": 0,
    "CLEAR": -1,
}

class WeatherPipeline:
    """
    Orchestrates the conversion of GRIB/NetCDF files into structured database forecasts.
    Runs spatial interpolation, temporal hourly interpolation, calculations, and saves daily summaries.
    """
    
    def __init__(self, db: Session, storage_dir: Optional[str] = None, provider_name: Optional[str] = None):
        self.db = db
        self.downloader = ECMWFDownloader(storage_dir=storage_dir)
        self.provider_name = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()

    def get_latest_successful_cycle(self) -> Optional[datetime]:
        """
        Queries the database to find the latest successfully processed cycle time.
        Falls back to detecting from the downloader if database is empty.
        """
        # Query database for latest successful job
        stmt = select(WeatherJob).where(
            and_(
                WeatherJob.status == "SUCCESS",
                WeatherJob.provider == self.provider_name
            )
        ).order_by(WeatherJob.cycle_time.desc()).limit(1)
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
        
        # Get active provider
        provider = WeatherProviderFactory.get_provider(self.provider_name)
        
        # If no cycle has run, calculate what should be available and trigger download
        if not latest_cycle_dt:
            latest_cycle_dt = provider.get_latest_available_cycle(datetime.now(timezone.utc))
            
        logger.info(f"Checking database for grid point ({lat_grid}, {lon_grid}) at cycle {latest_cycle_dt}")
        
        # 2. Check if we already have forecast data in the DB for this coordinate and cycle
        hourly_stmt = select(WeatherForecast).where(
            and_(
                WeatherForecast.latitude == lat_grid,
                WeatherForecast.longitude == lon_grid,
                WeatherForecast.cycle_time == latest_cycle_dt,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.forecast_time.asc())
        
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()
        
        if db_forecasts:
            logger.info(f"Database cache HIT for grid point ({lat_grid}, {lon_grid})")
            return self._build_response_from_db(lat_grid, lon_grid, latest_cycle_dt, db_forecasts)
            
        # 3. Cache Miss: Extract from GRIB file
        logger.info(f"Database cache MISS for grid point ({lat_grid}, {lon_grid}). Extracting from GRIB...")
        
        # Ensure we have the latest GRIB file
        target_path = provider.get_target_path(latest_cycle_dt)
        
        if not os.path.exists(target_path) or not provider.downloader.use_local_storage:
            logger.info("Downloading GRIB file (either missing locally or cloud‑only mode).")
            # If direct on-the-fly downloads are disabled, check database for fallback cycle immediately
            if not provider.downloader.allow_on_the_fly_downloads and provider.downloader.use_local_storage:
                logger.info("On-the-fly downloads are disabled and local storage is in use. Looking for fallback cycle in database...")
                db_forecasts, fallback_cycle_dt = self._get_latest_fallback_forecast(lat_grid, lon_grid)
                if db_forecasts:
                    return self._build_response_from_db(lat_grid, lon_grid, fallback_cycle_dt, db_forecasts)
                raise RuntimeError(
                    f"Weather forecast is not available. On-the-fly downloads are disabled and no fallback data exists in the database for ({lat_grid}, {lon_grid})."
                )

            logger.info(f"GRIB file not found locally. Downloading cycle {latest_cycle_dt}...")
            # Register job in DB
            job = WeatherJob(cycle_time=latest_cycle_dt, status="RUNNING", provider=self.provider_name)
            self.db.add(job)
            self.db.commit()
            
            try:
                path, checksum, size = provider.download_cycle(latest_cycle_dt)
                # Register raw file
                raw_file = WeatherRawFile(
                    job_id=job.id,
                    cycle_time=latest_cycle_dt,
                    file_path=path,
                    file_size=size,
                    checksum=checksum,
                    provider=self.provider_name
                )
                self.db.add(raw_file)
                job.status = "SUCCESS"
                self.db.commit()
            except Exception as e:
                self.db.rollback()  # Rollback any failed insertion transactions to prevent transaction block corruption
                try:
                    job.status = "FAILED"
                    job.error_message = str(e)
                    self.db.commit()
                except Exception as commit_err:
                    logger.error(f"Failed to commit FAILED job status to DB: {commit_err}")
                    self.db.rollback()
                
                # Check database for fallback cycle if download fails
                logger.warning(f"Could not download GRIB file for cycle {latest_cycle_dt}: {e}. Looking for fallback cycle in database...")
                db_forecasts, fallback_cycle_dt = self._get_latest_fallback_forecast(lat_grid, lon_grid)
                if db_forecasts:
                    return self._build_response_from_db(lat_grid, lon_grid, fallback_cycle_dt, db_forecasts)
                    
                raise RuntimeError(f"Could not download GRIB file for cycle {latest_cycle_dt} and no fallback data exists: {e}")
                
        # Parse GRIB file and extract data for this coordinate
        hourly_df = provider.extract_and_interpolate(target_path, lat_grid, lon_grid)
            
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
                "provider": self.provider_name,
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
                    WeatherForecast.longitude == lon_grid,
                    WeatherForecast.provider == self.provider_name
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
                    WeatherDaily.longitude == lon_grid,
                    WeatherDaily.provider == self.provider_name
                )
            )
        )
        db_daily_objs = [WeatherDaily(**rec) for rec in daily_records]
        self.db.bulk_save_objects(db_daily_objs)
        
        self.db.commit()
        
        # Re-query the database to get the exact records stored (to match rounding, constraints, and defaults)
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()
        
        # Return response
        return self._build_response_from_db(lat_grid, lon_grid, latest_cycle_dt, db_forecasts)

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
                "provider": self.provider_name,
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
                WeatherDaily.cycle_time == cycle_time,
                WeatherDaily.provider == self.provider_name
            )
        ).order_by(WeatherDaily.forecast_date.asc())
        
        db_dailies = self.db.execute(daily_stmt).scalars().all()
        
        # Determine "current" weather point: nearest hourly forecast point in the future or current hour
        now_utc = datetime.now(timezone.utc)
        current_pt = None
        
        # Sort by proximity to now
        time_diffs = []
        for pt in db_forecasts:
            f_time = pt.forecast_time
            if f_time.tzinfo is None:
                f_time = f_time.replace(tzinfo=timezone.utc)
            time_diffs.append(abs((f_time - now_utc).total_seconds()))
        if time_diffs:
            min_idx = np.argmin(time_diffs)
            current_pt = db_forecasts[min_idx]
        else:
            current_pt = db_forecasts[0]
            
        hourly_slice = db_forecasts[:24]  # Return next 24 hours of hourly data
        daily_slice = db_dailies[:10]      # Return up to 10 days
        
        return {
            "latitude": lat,
            "longitude": lon,
            "cycle_time": self._format_to_ist_str(cycle_time),
            "current": {
                "time": self._format_to_ist_str(current_pt.forecast_time),
                "temperature_2m": float(current_pt.temperature),
                "apparent_temperature": float(current_pt.feels_like),
                "relative_humidity_2m": float(current_pt.humidity),
                "surface_pressure": float(current_pt.pressure),
                "cloud_cover": float(current_pt.cloud_cover),
                "wind_speed_10m": float(current_pt.wind_speed),
                "wind_direction_10m": float(current_pt.wind_direction),
                "dew_point_2m": float(current_pt.dew_point),
                "uv_index": float(current_pt.uv_index),
                "visibility": float(current_pt.visibility),
                "precipitation": float(current_pt.precipitation),
                "weather_code": current_pt.weather_code,
            },
            "hourly": {
                "time": [self._format_to_ist_str(pt.forecast_time) for pt in hourly_slice],
                "temperature_2m": [float(pt.temperature) for pt in hourly_slice],
                "apparent_temperature": [float(pt.feels_like) for pt in hourly_slice],
                "precipitation": [float(pt.precipitation) for pt in hourly_slice],
                "relative_humidity_2m": [float(pt.humidity) for pt in hourly_slice],
                "surface_pressure": [float(pt.pressure) for pt in hourly_slice],
                "cloud_cover": [float(pt.cloud_cover) for pt in hourly_slice],
                "wind_speed_10m": [float(pt.wind_speed) for pt in hourly_slice],
                "wind_direction_10m": [float(pt.wind_direction) for pt in hourly_slice],
                "weather_code": [pt.weather_code for pt in hourly_slice],
            },
            "daily": {
                "time": [d.forecast_date for d in daily_slice],
                "temperature_2m_max": [float(d.temp_max) for d in daily_slice],
                "temperature_2m_min": [float(d.temp_min) for d in daily_slice],
                "precipitation_sum": [float(d.precipitation) for d in daily_slice],
                "humidity_avg": [float(d.humidity_avg) for d in daily_slice],
                "wind_speed_10m_max": [float(d.wind_speed_max) for d in daily_slice],
                "weather_code": [d.weather_code for d in daily_slice],
            }
        }

    @staticmethod
    def _format_to_ist_str(dt: datetime) -> str:
        """Converts a UTC datetime to IST and formats as '%Y-%m-%d %I:%M:%S %p'."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        dt_ist = dt.astimezone(ist_tz)
        return dt_ist.strftime("%Y-%m-%d %I:%M:%S %p")

    @staticmethod
    def _timestamp_to_ist_hm(ts: int) -> str:
        """Converts Unix timestamp to IST HH:MM string."""
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        dt_ist = dt.astimezone(ist_tz)
        return dt_ist.strftime("%H:%M")

    def _get_latest_fallback_forecast(self, lat: float, lon: float) -> tuple[List[WeatherForecast], Optional[datetime]]:
        """
        Retrieves the latest available forecast cycle in the database for a coordinate.
        """
        # Find the latest cycle time for this coordinate in the database
        stmt = select(WeatherForecast.cycle_time).where(
            and_(
                WeatherForecast.latitude == lat,
                WeatherForecast.longitude == lon,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.cycle_time.desc()).limit(1)
        
        fallback_cycle = self.db.execute(stmt).scalars().first()
        if not fallback_cycle:
            return [], None
            
        # Get all forecasts for that cycle
        hourly_stmt = select(WeatherForecast).where(
            and_(
                WeatherForecast.latitude == lat,
                WeatherForecast.longitude == lon,
                WeatherForecast.cycle_time == fallback_cycle,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.forecast_time.asc())
        
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()
        return list(db_forecasts), fallback_cycle

    def get_custom_forecast(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        Retrieves forecast data (ensuring it is ingested) and builds the custom Meteomatics response.
        """
        lat_grid = round(lat * 4) / 4
        lon_grid = round(lon * 4) / 4

        # Calling get_or_create_forecast guarantees the database is populated for this grid point (either fresh or fallback)
        self.get_or_create_forecast(lat_grid, lon_grid)

        # Get the actual cycle time returned (which could be the fallback cycle if downloads are disabled/failed)
        stmt = select(WeatherForecast.cycle_time).where(
            and_(
                WeatherForecast.latitude == lat_grid,
                WeatherForecast.longitude == lon_grid,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.cycle_time.desc()).limit(1)
        cycle_time = self.db.execute(stmt).scalars().first()
        
        if not cycle_time:
            raise RuntimeError(f"No weather data exists in database for ({lat_grid}, {lon_grid})")

        # Query all hourly forecasts for this cycle
        hourly_stmt = select(WeatherForecast).where(
            and_(
                WeatherForecast.latitude == lat_grid,
                WeatherForecast.longitude == lon_grid,
                WeatherForecast.cycle_time == cycle_time,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.forecast_time.asc())
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()

        # Query daily aggregates
        daily_stmt = select(WeatherDaily).where(
            and_(
                WeatherDaily.latitude == lat_grid,
                WeatherDaily.longitude == lon_grid,
                WeatherDaily.cycle_time == cycle_time,
                WeatherDaily.provider == self.provider_name
            )
        ).order_by(WeatherDaily.forecast_date.asc())
        db_dailies = self.db.execute(daily_stmt).scalars().all()

        return self._build_custom_response(lat_grid, lon_grid, cycle_time, list(db_forecasts), list(db_dailies))

    def _build_custom_response(
        self,
        lat: float,
        lon: float,
        cycle_time: datetime,
        db_forecasts: List[WeatherForecast],
        db_dailies: List[WeatherDaily]
    ) -> Dict[str, Any]:
        
        hourly_slice = db_forecasts[:24]
        daily_slice = db_dailies[:10]
        
        # 1. Helper to format coordinate nested structure
        def make_coord_dates(dates_list):
            return [{
                "lat": lat,
                "lon": lon,
                "dates": dates_list
            }]
            
        # 2. Build hourly parameter lists (grouped under "daily" key)
        daily_params = []
        
        # Helper to generate parameter dict
        def add_hourly_param(parameter, raw_key, height, unit, label, val_extractor):
            dates_list = []
            for pt in hourly_slice:
                val = val_extractor(pt)
                dt_utc = pt.forecast_time
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dates_list.append({
                    "date": dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "value": val
                })
            daily_params.append({
                "parameter": parameter,
                "raw_key": raw_key,
                "height": height,
                "unit": unit,
                "label": label,
                "coordinates": make_coord_dates(dates_list)
            })

        add_hourly_param("temperature", "t_2m:C", "2m", "°C", "TEMPERATURE at 2m (°C)", lambda pt: float(pt.temperature))
        add_hourly_param("precipitation", "precip_1h:mm", "", "mm", "PRECIPITATION at 1h (mm)", lambda pt: float(pt.precipitation))
        add_hourly_param("wind_speed", "wind_speed_10m:kmh", "10m", "km/h", "WIND SPEED at 10m (km/h)", lambda pt: round(float(pt.wind_speed) * 3.6, 1))
        add_hourly_param("wind_direction", "wind_dir_10m:d", "10m", "°", "WIND DIRECTION at 10m (°)", lambda pt: float(pt.wind_direction))
        add_hourly_param("wind_gusts", "wind_gusts_10m_1h:kmh", "10m_1h", "km/h", "WIND GUSTS at 10m_1h (km/h)", lambda pt: round(float(pt.wind_speed) * 3.6 * 1.3, 1))
        add_hourly_param("relative_humidity", "relative_humidity_2m:p", "2m", "%", "RELATIVE HUMIDITY at 2m (%)", lambda pt: float(pt.humidity))
        add_hourly_param("absolute_humidity", "absolute_humidity_2m:gm3", "2m", "g/m³", "ABSOLUTE HUMIDITY at 2m (g/m³)", lambda pt: calculations.calculate_absolute_humidity(float(pt.temperature), float(pt.humidity)))
        add_hourly_param("dew_point", "dew_point_2m:C", "2m", "°C", "DEW POINT at 2m (°C)", lambda pt: float(pt.dew_point))
        add_hourly_param("mean_effective_cloud_cover", "effective_cloud_cover_mean_1h:p", "1h", "%", "MEAN EFFECTIVE CLOUD COVER at 1h (%)", lambda pt: float(pt.cloud_cover))
        add_hourly_param("soil_frost", "soil_frost:p", "", "%", "SOIL FROST (%)", lambda pt: calculations.calculate_soil_frost(float(pt.temperature)))

        # 3. Build daily parameter lists (grouped under "weekly" key)
        weekly_params = []
        
        def add_daily_param(parameter, raw_key, height, unit, label, val_extractor):
            dates_list = []
            for d in daily_slice:
                val = val_extractor(d)
                date_str = d.forecast_date.strftime("%Y-%m-%dT06:30:00Z")
                dates_list.append({
                    "date": date_str,
                    "value": val
                })
            weekly_params.append({
                "parameter": parameter,
                "raw_key": raw_key,
                "height": height,
                "unit": unit,
                "label": label,
                "coordinates": make_coord_dates(dates_list)
            })

        # Estimate daily probability of precipitation (PoP)
        def estimate_pop(d):
            precip = float(d.precipitation)
            humidity = float(d.humidity_avg)
            if precip == 0.0:
                return round(max(0.0, min(30.0, humidity - 60.0)), 1)
            else:
                base = 40.0
                precip_factor = min(35.0, precip * 5.0)  # up to 35% for 7mm rain
                humidity_factor = max(0.0, min(20.0, (humidity - 50.0) * 0.5))  # up to 20% for 90% humidity
                return round(min(95.0, base + precip_factor + humidity_factor), 1)

        add_daily_param("min_temperature", "t_min_2m_24h:C", "2m_24h", "°C", "MIN TEMPERATURE at 2m_24h (°C)", lambda d: float(d.temp_min))
        add_daily_param("max_temperature", "t_max_2m_24h:C", "2m_24h", "°C", "MAX TEMPERATURE at 2m_24h (°C)", lambda d: float(d.temp_max))
        add_daily_param("precipitation_probability", "prob_precip_24h:p", "24h", "%", "PRECIPITATION PROBABILITY at 24h (%)", estimate_pop)
        add_daily_param("wind_gusts", "wind_gusts_10m_24h:kmh", "10m_24h", "km/h", "WIND GUSTS at 10m_24h (km/h)", lambda d: round(float(d.wind_speed_max) * 3.6 * 1.3, 1))
        add_daily_param("relative_humidity_min", "relative_humidity_min_2m_24h:p", "2m_24h", "%", "RELATIVE HUMIDITY MIN at 2m_24h (%)", lambda d: round(max(10.0, float(d.humidity_avg) - 20.0), 1))
        add_daily_param("absolute_humidity", "absolute_humidity_2m:gm3", "2m", "g/m³", "ABSOLUTE HUMIDITY at 2m (g/m³)", lambda d: calculations.calculate_absolute_humidity(float(d.temp_min), float(d.humidity_avg)))
        add_daily_param("dew_point", "dew_point_2m:C", "2m", "°C", "DEW POINT at 2m (°C)", lambda d: round(float(d.temp_min) - 4.0, 1))
        add_daily_param("soil_frost", "soil_frost:p", "", "%", "SOIL FROST (%)", lambda d: calculations.calculate_soil_frost(float(d.temp_min)))
        add_daily_param("temperature_mean", "t_mean_2m_24h:C", "2m_24h", "°C", "TEMPERATURE MEAN at 2m_24h (°C)", lambda d: round((float(d.temp_min) + float(d.temp_max)) / 2.0, 1))
        add_daily_param("wind_speed", "wind_speed_10m:kmh", "10m", "km/h", "WIND SPEED at 10m (km/h)", lambda d: round(float(d.wind_speed_max) * 3.6, 1))

        # 4. Build common/astronomical parameter lists (grouped under "common" key)
        common_params = []
        
        moon_ages = []
        moon_phases = []
        sunrise_vals = []
        sunset_vals = []
        moonrise_vals = []
        moonset_vals = []
        heavy_rain_warnings = []
        wind_warnings = []
        precip_vals = []
        
        for d in daily_slice:
            date_str = d.forecast_date.strftime("%Y-%m-%dT06:30:00Z")
            
            age, phase = calculations.calculate_moon_info(d.forecast_date)
            moon_ages.append({"date": date_str, "value": age})
            moon_phases.append({"date": date_str, "value": phase})
            
            rise_ts, set_ts = calculations.calculate_sunrise_sunset(lat, lon, d.forecast_date)
            sunrise_str = self._timestamp_to_ist_hm(rise_ts)
            sunset_str = self._timestamp_to_ist_hm(set_ts)
            sunrise_vals.append({"date": date_str, "value": sunrise_str})
            sunset_vals.append({"date": date_str, "value": sunset_str})
            
            mrise, mset = calculations.calculate_moonrise_moonset(rise_ts, set_ts, phase)
            moonrise_str = self._timestamp_to_ist_hm(mrise)
            moonset_str = self._timestamp_to_ist_hm(mset)
            moonrise_vals.append({"date": date_str, "value": moonrise_str})
            moonset_vals.append({"date": date_str, "value": moonset_str})
            
            precip_vals.append({"date": date_str, "value": float(d.precipitation)})
            
            # IMD Heavy Rain definition: >= 64.5 mm
            heavy_rain_warnings.append({"date": date_str, "value": 1.0 if float(d.precipitation) >= 64.5 else 0.0})
            wind_warnings.append({"date": date_str, "value": 1.0 if float(d.wind_speed_max) > 10.0 else 0.0})

        def add_common_param(parameter, raw_key, height, unit, label, dates_list):
            common_params.append({
                "parameter": parameter,
                "raw_key": raw_key,
                "height": height,
                "unit": unit,
                "label": label,
                "coordinates": make_coord_dates(dates_list)
            })

        add_common_param("lightning_strikes", "lightning_strikes_3km_24h:x", "3km_24h", "count", "LIGHTNING STRIKES 3KM 24H (count)", [{"date": d.forecast_date.strftime("%Y-%m-%dT06:30:00Z"), "value": 0.0} for d in daily_slice])
        add_common_param("moon_age", "moon_age:d", "", "d", "MOON AGE (d)", moon_ages)
        add_common_param("moon_phase", "moon_phase:idx", "", "index", "MOON PHASE (index)", moon_phases)
        add_common_param("precipitation", "precip_24h:mm", "24h", "mm", "PRECIPITATION at 24h (mm)", precip_vals)
        add_common_param("sunrise", "sunrise:sql", "", "hh:mm", "SUNRISE (hh:mm)", sunrise_vals)
        add_common_param("sunset", "sunset:sql", "", "hh:mm", "SUNSET (hh:mm)", sunset_vals)
        add_common_param("moonrise", "moonrise:sql", "", "hh:mm", "MOONRISE (hh:mm)", moonrise_vals)
        add_common_param("moonset", "moonset:sql", "", "hh:mm", "MOONSET (hh:mm)", moonset_vals)
        add_common_param("heavy_rain_warning", "heavy_rain_warning_6h:idx", "6h", "index", "HEAVY RAIN WARNING 6H (index)", heavy_rain_warnings)
        add_common_param("wind_warning", "wind_warning_1h:idx", "1h", "index", "WIND WARNING 1H (index)", wind_warnings)

        return {
            "status": 200,
            "message": "weather data fetched successfully",
            "data": {
                "daily": daily_params,
                "weekly": weekly_params,
                "common": common_params
            }
        }


