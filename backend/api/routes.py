import os
import logging
from typing import Optional, Any
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy import select, delete

from backend.database.connection import get_db
from backend.database.models import ApiCache
from backend.api.schemas import WeatherCurrentDTO, WeatherHourlyDTO, WeatherDailyDTO, WeatherFullResponse
from backend.workers.pipeline import WeatherPipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/weather", tags=["weather"])

CACHE_TTL_MINUTES = 15

def get_cached_response(db: Session, cache_key: str) -> Optional[dict]:
    """Helper to retrieve cached JSON payload from the database."""
    now = datetime.now(timezone.utc)
    stmt = select(ApiCache).where(
        ApiCache.cache_key == cache_key,
        ApiCache.expires_at > now
    )
    cache_entry = db.execute(stmt).scalars().first()
    if cache_entry:
        logger.debug(f"HTTP Cache HIT for key: {cache_key}")
        return cache_entry.cache_value
    return None

def set_cached_response(db: Session, cache_key: str, value: Any, ttl_minutes: int = CACHE_TTL_MINUTES):
    """Helper to store JSON payload into the database cache."""
    # Delete any stale entry for the key
    db.execute(delete(ApiCache).where(ApiCache.cache_key == cache_key))
    
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    
    # Use jsonable_encoder to convert datetime objects to ISO strings, ensuring it is JSON serializable
    json_compatible_value = jsonable_encoder(value)
    
    cache_entry = ApiCache(
        cache_key=cache_key,
        cache_value=json_compatible_value,
        expires_at=expires_at
    )
    db.add(cache_entry)
    db.commit()

@router.get("/full", response_model=WeatherFullResponse)
def get_full_weather(
    latitude: float = Query(..., ge=-90.0, le=90.0, description="Latitude of the location"),
    longitude: float = Query(..., ge=-180.0, le=180.0, description="Longitude of the location"),
    db: Session = Depends(get_db)
):
    """
    Returns the complete weather forecast package (Current, Hourly, and 10-day Daily forecast)
    for the nearest grid point.
    """
    # Round to nearest 0.25 to align with grid caching
    lat_grid = round(latitude * 4) / 4
    lon_grid = round(longitude * 4) / 4
    
    cache_key = f"weather_full_{lat_grid:.2f}_{lon_grid:.2f}"
    
    # Try reading from HTTP Response Cache
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        
        # Save to HTTP Response Cache
        set_cached_response(db, cache_key, data)
        
        return data
    except Exception as e:
        logger.error(f"Error handling full weather query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

@router.get("/current", response_model=WeatherCurrentDTO)
def get_current_weather(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the current weather conditions for the location."""
    lat_grid = round(latitude * 4) / 4
    lon_grid = round(longitude * 4) / 4
    cache_key = f"weather_current_{lat_grid:.2f}_{lon_grid:.2f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        current_data = data["current"]
        
        set_cached_response(db, cache_key, current_data)
        return current_data
    except Exception as e:
        logger.error(f"Error handling current weather query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/hourly", response_model=list[WeatherHourlyDTO])
def get_hourly_forecast(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the hourly forecast for the next 24 hours."""
    lat_grid = round(latitude * 4) / 4
    lon_grid = round(longitude * 4) / 4
    cache_key = f"weather_hourly_{lat_grid:.2f}_{lon_grid:.2f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        hourly_data = data["hourly"]
        
        set_cached_response(db, cache_key, hourly_data)
        return hourly_data
    except Exception as e:
        logger.error(f"Error handling hourly weather query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/daily", response_model=list[WeatherDailyDTO])
def get_daily_forecast(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the daily forecast for the next 10 days."""
    lat_grid = round(latitude * 4) / 4
    lon_grid = round(longitude * 4) / 4
    cache_key = f"weather_daily_{lat_grid:.2f}_{lon_grid:.2f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        daily_data = data["daily"]
        
        set_cached_response(db, cache_key, daily_data)
        return daily_data
    except Exception as e:
        logger.error(f"Error handling daily weather query: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

from backend.workers.scheduler import WeatherScheduler

@router.post("/scheduler/run")
def trigger_scheduler_run(
    secret: Optional[str] = Header(None, alias="X-Scheduler-Secret"),
    db: Session = Depends(get_db)
):
    """
    HTTP trigger to run the forecast ingestion cycle check.
    Secured by a secret header token (X-Scheduler-Secret).
    """
    expected_secret = os.getenv("SCHEDULER_SECRET")
    if not expected_secret:
        raise HTTPException(status_code=500, detail="SCHEDULER_SECRET environment variable is not configured on the server.")
        
    if secret != expected_secret:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid scheduler secret token.")
        
    try:
        # Instantiate and execute scheduler check once
        scheduler = WeatherScheduler()
        scheduler.run_once()
        return {"status": "SUCCESS", "message": "Scheduler execution completed successfully."}
    except Exception as e:
        logger.error(f"Scheduler HTTP trigger failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scheduler failed: {str(e)}")
