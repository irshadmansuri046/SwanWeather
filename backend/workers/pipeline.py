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

    def _bilinear_interpolate(self, lat: float, lon: float, points_data: Dict[tuple[float, float], Dict[str, float]]) -> Dict[str, float]:
        """
        Interpolates weather variables for a target (lat, lon) using surrounding grid points data.
        points_data is a dict mapping (g_lat, g_lon) -> dict of variables.
        """
        lats = sorted(list(set(p[0] for p in points_data.keys())))
        lons = sorted(list(set(p[1] for p in points_data.keys())))
        
        # If we only have 1 unique lat and lon, it's exactly a grid point
        if len(lats) == 1 and len(lons) == 1:
            coord = (lats[0], lons[0])
            return points_data.get(coord) or list(points_data.values())[0]
            
        if len(lats) == 1:
            # Linear interpolation in longitude
            lon1, lon2 = lons[0], lons[1]
            lat1 = lats[0]
            tx = (lon - lon1) / (lon2 - lon1) if lon2 != lon1 else 0.0
            v1 = points_data.get((lat1, lon1))
            v2 = points_data.get((lat1, lon2))
            
            if not v1 or not v2:
                return v1 or v2 or {}
                
            interpolated = {}
            for key in v1.keys():
                if key in ["wind_speed", "wind_direction"]:
                    continue
                if isinstance(v1[key], (int, float)):
                    interpolated[key] = (1 - tx) * v1[key] + tx * v2[key]
                else:
                    interpolated[key] = v1[key]
            
            # Interpolate wind
            w1_speed, w1_dir = v1["wind_speed"], v1["wind_direction"]
            w2_speed, w2_dir = v2["wind_speed"], v2["wind_direction"]
            
            r1 = np.radians(w1_dir - 180)
            u1, v_wind1 = w1_speed * np.sin(r1), w1_speed * np.cos(r1)
            
            r2 = np.radians(w2_dir - 180)
            u2, v_wind2 = w2_speed * np.sin(r2), w2_speed * np.cos(r2)
            
            u_interp = (1 - tx) * u1 + tx * u2
            v_interp = (1 - tx) * v_wind1 + tx * v_wind2
            
            interpolated["wind_speed"] = float(np.sqrt(u_interp**2 + v_interp**2))
            interpolated["wind_direction"] = float((np.degrees(np.arctan2(u_interp, v_interp)) + 180) % 360)
            return interpolated
            
        if len(lons) == 1:
            # Linear interpolation in latitude
            lat1, lat2 = lats[0], lats[1]
            lon1 = lons[0]
            ty = (lat - lat1) / (lat2 - lat1) if lat2 != lat1 else 0.0
            v1 = points_data.get((lat1, lon1))
            v2 = points_data.get((lat2, lon1))
            
            if not v1 or not v2:
                return v1 or v2 or {}
                
            interpolated = {}
            for key in v1.keys():
                if key in ["wind_speed", "wind_direction"]:
                    continue
                if isinstance(v1[key], (int, float)):
                    interpolated[key] = (1 - ty) * v1[key] + ty * v2[key]
                else:
                    interpolated[key] = v1[key]
            
            # Interpolate wind
            w1_speed, w1_dir = v1["wind_speed"], v1["wind_direction"]
            w2_speed, w2_dir = v2["wind_speed"], v2["wind_direction"]
            
            r1 = np.radians(w1_dir - 180)
            u1, v_wind1 = w1_speed * np.sin(r1), w1_speed * np.cos(r1)
            
            r2 = np.radians(w2_dir - 180)
            u2, v_wind2 = w2_speed * np.sin(r2), w2_speed * np.cos(r2)
            
            u_interp = (1 - ty) * u1 + ty * u2
            v_interp = (1 - ty) * v_wind1 + ty * v_wind2
            
            interpolated["wind_speed"] = float(np.sqrt(u_interp**2 + v_interp**2))
            interpolated["wind_direction"] = float((np.degrees(np.arctan2(u_interp, v_interp)) + 180) % 360)
            return interpolated

        # Bilinear interpolation using 4 surrounding points
        lat1, lat2 = lats[0], lats[1]
        lon1, lon2 = lons[0], lons[1]
        
        tx = (lon - lon1) / (lon2 - lon1) if lon2 != lon1 else 0.0
        ty = (lat - lat1) / (lat2 - lat1) if lat2 != lat1 else 0.0
        
        p11 = points_data.get((lat1, lon1))
        p21 = points_data.get((lat1, lon2))
        p12 = points_data.get((lat2, lon1))
        p22 = points_data.get((lat2, lon2))
        
        if not (p11 and p21 and p12 and p22):
            # Fallback to nearest available point
            available = [c for c in [(lat1, lon1), (lat1, lon2), (lat2, lon1), (lat2, lon2)] if c in points_data]
            if not available:
                return list(points_data.values())[0] if points_data else {}
            closest = min(available, key=lambda c: (c[0] - lat)**2 + (c[1] - lon)**2)
            return points_data[closest]
            
        interpolated = {}
        for key in p11.keys():
            if key in ["wind_speed", "wind_direction"]:
                continue
            if isinstance(p11[key], (int, float)):
                val = (1 - tx) * (1 - ty) * p11[key] + \
                      tx * (1 - ty) * p21[key] + \
                      (1 - tx) * ty * p12[key] + \
                      tx * ty * p22[key]
                interpolated[key] = val
            else:
                interpolated[key] = p11[key]
                
        # Interpolate wind
        uv_components = []
        for p in [p11, p21, p12, p22]:
            speed = p["wind_speed"]
            direction = p["wind_direction"]
            rad = np.radians(direction - 180)
            u = speed * np.sin(rad)
            v_wind = speed * np.cos(rad)
            uv_components.append((u, v_wind))
            
        u11, v11 = uv_components[0]
        u21, v21 = uv_components[1]
        u12, v12 = uv_components[2]
        u22, v22 = uv_components[3]
        
        u_interp = (1 - tx) * (1 - ty) * u11 + tx * (1 - ty) * u21 + (1 - tx) * ty * u12 + tx * ty * u22
        v_interp = (1 - tx) * (1 - ty) * v11 + tx * (1 - ty) * v21 + (1 - tx) * ty * v12 + tx * ty * v22
        
        interpolated["wind_speed"] = float(np.sqrt(u_interp**2 + v_interp**2))
        interpolated["wind_direction"] = float((np.degrees(np.arctan2(u_interp, v_interp)) + 180) % 360)
        
        return interpolated

    def _ingest_grid_point(self, g_lat: float, g_lon: float, latest_cycle_dt: datetime, provider) -> None:
        """
        Extracts forecast data for a single grid point from the GRIB file and saves it to the database.
        """
        # Ensure we have the latest GRIB file
        target_path = provider.get_target_path(latest_cycle_dt)
        
        if not os.path.exists(target_path) or not provider.downloader.use_local_storage:
            logger.info(f"Downloading GRIB file for grid point ({g_lat}, {g_lon}) (missing locally or cloud-only).")
            if not provider.downloader.allow_on_the_fly_downloads and provider.downloader.use_local_storage:
                raise RuntimeError(
                    f"Weather forecast is not available. On-the-fly downloads are disabled and no fallback data exists in the database for grid point ({g_lat}, {g_lon})."
                )

            logger.info(f"GRIB file not found locally. Downloading cycle {latest_cycle_dt}...")
            # Register job in DB (check if already registered to avoid duplicates)
            stmt = select(WeatherJob).where(
                and_(
                    WeatherJob.cycle_time == latest_cycle_dt,
                    WeatherJob.provider == self.provider_name
                )
            )
            job = self.db.execute(stmt).scalars().first()
            if not job:
                job = WeatherJob(cycle_time=latest_cycle_dt, status="RUNNING", provider=self.provider_name)
                self.db.add(job)
                self.db.commit()
            
            try:
                path, checksum, size = provider.download_cycle(latest_cycle_dt)
                # Register raw file if not already registered
                stmt_rf = select(WeatherRawFile).where(WeatherRawFile.checksum == checksum)
                raw_file = self.db.execute(stmt_rf).scalars().first()
                if not raw_file:
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
                self.db.rollback()
                if job and job.status == "RUNNING":
                    job.status = "FAILED"
                    job.error_message = str(e)
                    self.db.commit()
                raise RuntimeError(f"Could not download GRIB file for cycle {latest_cycle_dt} to extract ({g_lat}, {g_lon}): {e}")

        # Extract from GRIB/NetCDF file
        hourly_df = provider.extract_and_interpolate(target_path, g_lat, g_lon)
        
        # derived weather computations
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
            uv = WeatherComputation.estimate_uv_index(g_lat, g_lon, f_time, cloud)
            weather_code = WeatherComputation.get_weather_code(temp, precip, cloud, vis, wind)
            
            hourly_records.append({
                "latitude": g_lat,
                "longitude": g_lon,
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
            
        # Clean up stale forecasts for this exact grid point
        self.db.execute(
            delete(WeatherForecast).where(
                and_(
                    WeatherForecast.latitude == g_lat,
                    WeatherForecast.longitude == g_lon,
                    WeatherForecast.provider == self.provider_name
                )
            )
        )
        
        db_hourly_objs = [WeatherForecast(**rec) for rec in hourly_records]
        self.db.bulk_save_objects(db_hourly_objs)
        
        # Daily aggregates
        daily_records = self._generate_daily_summaries(hourly_records, g_lat, g_lon, latest_cycle_dt)
        self.db.execute(
            delete(WeatherDaily).where(
                and_(
                    WeatherDaily.latitude == g_lat,
                    WeatherDaily.longitude == g_lon,
                    WeatherDaily.provider == self.provider_name
                )
            )
        )
        db_daily_objs = [WeatherDaily(**rec) for rec in daily_records]
        self.db.bulk_save_objects(db_daily_objs)
        
        self.db.commit()

    def _get_latest_fallback_cycle(self, grid_points: List[tuple[float, float]]) -> Optional[datetime]:
        """
        Finds the latest available cycle time in the database for any of the surrounding grid points.
        """
        from sqlalchemy import or_
        conditions = []
        for g_lat, g_lon in grid_points:
            conditions.append(and_(WeatherForecast.latitude == g_lat, WeatherForecast.longitude == g_lon))
        stmt = select(WeatherForecast.cycle_time).where(
            and_(
                or_(*conditions),
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.cycle_time.desc()).limit(1)
        return self.db.execute(stmt).scalars().first()

    def _build_interpolated_response(self, lat: float, lon: float, cycle_time: datetime, db_forecasts: List[WeatherForecast], db_dailies: List[WeatherDaily]) -> Dict[str, Any]:
        """
        Builds the structured API dictionary from in-memory interpolated records.
        """
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

    def get_or_create_forecast(self, lat: float, lon: float) -> Dict[str, Any]:
        """
        Main query handler. Finds surrounding grid points in DB, ingests them if missing,
        and performs spatial bilinear interpolation in-memory.
        """
        # Determine surrounding grid points on 0.25-degree grid
        lat_low = float(np.floor(lat * 4) / 4)
        lat_high = float(np.ceil(lat * 4) / 4)
        lon_low = float(np.floor(lon * 4) / 4)
        lon_high = float(np.ceil(lon * 4) / 4)
        
        grid_points = sorted(list(set([
            (lat_low, lon_low),
            (lat_low, lon_high),
            (lat_high, lon_low),
            (lat_high, lon_high)
        ])))
        
        # 1. Determine target cycle time
        latest_cycle_dt = self.get_latest_successful_cycle()
        
        # Get active provider
        provider = WeatherProviderFactory.get_provider(self.provider_name)
        
        # If no cycle has run, calculate what should be available
        if not latest_cycle_dt:
            latest_cycle_dt = provider.get_latest_available_cycle(datetime.now(timezone.utc))
            
        logger.info(f"Retrieving surrounding grid points {grid_points} at cycle {latest_cycle_dt}")
        
        # 2. Check cache for surrounding grid points
        from sqlalchemy import or_
        conditions = []
        for g_lat, g_lon in grid_points:
            conditions.append(and_(WeatherForecast.latitude == g_lat, WeatherForecast.longitude == g_lon))
            
        hourly_stmt = select(WeatherForecast).where(
            and_(
                or_(*conditions),
                WeatherForecast.cycle_time == latest_cycle_dt,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.forecast_time.asc())
        
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()
        
        # Determine which grid points are missing
        forecasts_by_grid = {}
        for f in db_forecasts:
            coord = (float(f.latitude), float(f.longitude))
            forecasts_by_grid.setdefault(coord, []).append(f)
            
        missing_points = []
        for g_lat, g_lon in grid_points:
            coord = (g_lat, g_lon)
            if coord not in forecasts_by_grid or len(forecasts_by_grid[coord]) == 0:
                missing_points.append(coord)
                
        # 3. Ingest missing grid points
        if missing_points:
            logger.info(f"Database cache MISS for grid points: {missing_points}. Extracting from GRIB...")
            for g_lat, g_lon in missing_points:
                try:
                    self._ingest_grid_point(g_lat, g_lon, latest_cycle_dt, provider)
                except Exception as e:
                    # Fallback cycle check if downloads are disabled
                    if not provider.downloader.allow_on_the_fly_downloads and provider.downloader.use_local_storage:
                        logger.info("On-the-fly downloads are disabled. Looking for fallback cycle in database...")
                        fallback_cycle_dt = self._get_latest_fallback_cycle(grid_points)
                        if fallback_cycle_dt:
                            # Re-query using fallback_cycle_dt
                            hourly_stmt = select(WeatherForecast).where(
                                and_(
                                    or_(*conditions),
                                    WeatherForecast.cycle_time == fallback_cycle_dt,
                                    WeatherForecast.provider == self.provider_name
                                )
                            ).order_by(WeatherForecast.forecast_time.asc())
                            db_forecasts = self.db.execute(hourly_stmt).scalars().all()
                            latest_cycle_dt = fallback_cycle_dt
                            
                            forecasts_by_grid = {}
                            for f in db_forecasts:
                                coord = (float(f.latitude), float(f.longitude))
                                forecasts_by_grid.setdefault(coord, []).append(f)
                            # Mark missing points resolved via fallback
                            missing_points = []
                            break
                    raise e
                    
            # Re-query if we successfully ingested missing points
            if missing_points:
                db_forecasts = self.db.execute(hourly_stmt).scalars().all()
                forecasts_by_grid = {}
                for f in db_forecasts:
                    coord = (float(f.latitude), float(f.longitude))
                    forecasts_by_grid.setdefault(coord, []).append(f)

        # If database is completely empty and couldn't fetch anything, error out
        if not db_forecasts:
            raise RuntimeError(
                f"Weather forecast is not available. No forecast data exists in the database for cycle {latest_cycle_dt}."
            )
            
        # 4. Align hourly forecasts by time and interpolate in-memory
        forecasts_by_time = {}
        for f in db_forecasts:
            forecasts_by_time.setdefault(f.forecast_time, []).append(f)
            
        interpolated_hourly_records = []
        for f_time, pts in sorted(forecasts_by_time.items()):
            points_data = {}
            for pt in pts:
                coord = (float(pt.latitude), float(pt.longitude))
                points_data[coord] = {
                    "temperature": float(pt.temperature),
                    "feels_like": float(pt.feels_like),
                    "precipitation": float(pt.precipitation),
                    "humidity": float(pt.humidity),
                    "pressure": float(pt.pressure),
                    "cloud_cover": float(pt.cloud_cover),
                    "wind_speed": float(pt.wind_speed),
                    "wind_direction": float(pt.wind_direction),
                    "dew_point": float(pt.dew_point),
                    "uv_index": float(pt.uv_index),
                    "visibility": float(pt.visibility),
                }
            
            interp_vals = self._bilinear_interpolate(lat, lon, points_data)
            
            # Use weather code of nearest grid point
            nearest_pt = min(pts, key=lambda pt: (float(pt.latitude) - lat)**2 + (float(pt.longitude) - lon)**2)
            weather_code = nearest_pt.weather_code
            
            interpolated_hourly_records.append({
                "latitude": lat,
                "longitude": lon,
                "forecast_time": f_time,
                "cycle_time": latest_cycle_dt,
                "provider": self.provider_name,
                "temperature": interp_vals["temperature"],
                "feels_like": interp_vals["feels_like"],
                "precipitation": interp_vals["precipitation"],
                "humidity": interp_vals["humidity"],
                "pressure": interp_vals["pressure"],
                "cloud_cover": interp_vals["cloud_cover"],
                "wind_speed": interp_vals["wind_speed"],
                "wind_direction": interp_vals["wind_direction"],
                "dew_point": interp_vals["dew_point"],
                "uv_index": interp_vals["uv_index"],
                "visibility": interp_vals["visibility"],
                "weather_code": weather_code
            })
            
        # Instantiate in-memory WeatherForecast objects
        interpolated_forecasts = [WeatherForecast(**rec) for rec in interpolated_hourly_records]
        
        # Generate in-memory Daily aggregates from interpolated hourly records
        daily_records = self._generate_daily_summaries(interpolated_hourly_records, lat, lon, latest_cycle_dt)
        interpolated_dailies = [WeatherDaily(**rec) for rec in daily_records]
        
        return self._build_interpolated_response(lat, lon, latest_cycle_dt, interpolated_forecasts, interpolated_dailies)

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
        lat_low = float(np.floor(lat * 4) / 4)
        lat_high = float(np.ceil(lat * 4) / 4)
        lon_low = float(np.floor(lon * 4) / 4)
        lon_high = float(np.ceil(lon * 4) / 4)
        
        grid_points = sorted(list(set([
            (lat_low, lon_low),
            (lat_low, lon_high),
            (lat_high, lon_low),
            (lat_high, lon_high)
        ])))

        # Calling get_or_create_forecast guarantees the database is populated for all surrounding grid points
        self.get_or_create_forecast(lat, lon)

        # Get the actual cycle time returned
        g_lat, g_lon = grid_points[0]
        stmt = select(WeatherForecast.cycle_time).where(
            and_(
                WeatherForecast.latitude == g_lat,
                WeatherForecast.longitude == g_lon,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.cycle_time.desc()).limit(1)
        cycle_time = self.db.execute(stmt).scalars().first()
        
        if not cycle_time:
            raise RuntimeError(f"No weather data exists in database for grid point ({g_lat}, {g_lon})")

        # Query all hourly forecasts for these grid points
        from sqlalchemy import or_
        conditions = []
        for g_lat, g_lon in grid_points:
            conditions.append(and_(WeatherForecast.latitude == g_lat, WeatherForecast.longitude == g_lon))
            
        hourly_stmt = select(WeatherForecast).where(
            and_(
                or_(*conditions),
                WeatherForecast.cycle_time == cycle_time,
                WeatherForecast.provider == self.provider_name
            )
        ).order_by(WeatherForecast.forecast_time.asc())
        db_forecasts = self.db.execute(hourly_stmt).scalars().all()

        # Perform bilinear interpolation
        forecasts_by_time = {}
        for f in db_forecasts:
            forecasts_by_time.setdefault(f.forecast_time, []).append(f)
            
        interpolated_hourly_records = []
        for f_time, pts in sorted(forecasts_by_time.items()):
            points_data = {}
            for pt in pts:
                coord = (float(pt.latitude), float(pt.longitude))
                points_data[coord] = {
                    "temperature": float(pt.temperature),
                    "feels_like": float(pt.feels_like),
                    "precipitation": float(pt.precipitation),
                    "humidity": float(pt.humidity),
                    "pressure": float(pt.pressure),
                    "cloud_cover": float(pt.cloud_cover),
                    "wind_speed": float(pt.wind_speed),
                    "wind_direction": float(pt.wind_direction),
                    "dew_point": float(pt.dew_point),
                    "uv_index": float(pt.uv_index),
                    "visibility": float(pt.visibility),
                }
            
            interp_vals = self._bilinear_interpolate(lat, lon, points_data)
            nearest_pt = min(pts, key=lambda pt: (float(pt.latitude) - lat)**2 + (float(pt.longitude) - lon)**2)
            weather_code = nearest_pt.weather_code
            
            interpolated_hourly_records.append({
                "latitude": lat,
                "longitude": lon,
                "forecast_time": f_time,
                "cycle_time": cycle_time,
                "provider": self.provider_name,
                "temperature": interp_vals["temperature"],
                "feels_like": interp_vals["feels_like"],
                "precipitation": interp_vals["precipitation"],
                "humidity": interp_vals["humidity"],
                "pressure": interp_vals["pressure"],
                "cloud_cover": interp_vals["cloud_cover"],
                "wind_speed": interp_vals["wind_speed"],
                "wind_direction": interp_vals["wind_direction"],
                "dew_point": interp_vals["dew_point"],
                "uv_index": interp_vals["uv_index"],
                "visibility": interp_vals["visibility"],
                "weather_code": weather_code
            })
            
        interpolated_forecasts = [WeatherForecast(**rec) for rec in interpolated_hourly_records]
        
        daily_records = self._generate_daily_summaries(interpolated_hourly_records, lat, lon, cycle_time)
        interpolated_dailies = [WeatherDaily(**rec) for rec in daily_records]

        return self._build_custom_response(lat, lon, cycle_time, interpolated_forecasts, interpolated_dailies)

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


