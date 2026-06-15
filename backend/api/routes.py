import os
import logging
from typing import Optional, Any, List
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Header, BackgroundTasks
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
from sqlalchemy import select, delete

from backend.database.connection import get_db
from backend.database.models import ApiCache, Crop, CropStage
from backend.api.schemas import WeatherCurrentDTO, WeatherHourlyResponse, WeatherDailyResponse, WeatherFullResponse, WeatherCustomResponse, CustomWeatherRequest, CropStageQuery
from backend.workers.pipeline import WeatherPipeline
from backend import config

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

def handle_pipeline_exception(e: Exception):
    err_msg = str(e)
    if "On-the-fly downloads are disabled and no fallback data exists" in err_msg:
        raise HTTPException(status_code=404, detail=err_msg)
    elif "Could not download GRIB file" in err_msg or "ECMWF Client download" in err_msg or "missing locally and on-the-fly downloads" in err_msg:
        raise HTTPException(status_code=503, detail=f"Weather service temporarily unavailable: {err_msg}")
    elif isinstance(e, ValueError):
        raise HTTPException(status_code=400, detail=err_msg)
    else:
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {err_msg}")


def handle_full_weather(latitude: float, longitude: float, db: Session, provider_name: Optional[str] = None):
    provider = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()
    cache_key = f"weather_full_{provider}_{latitude:.4f}_{longitude:.4f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db, provider_name=provider)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        set_cached_response(db, cache_key, data)
        return data
    except Exception as e:
        logger.error(f"Error handling full weather query ({provider}): {e}", exc_info=True)
        handle_pipeline_exception(e)

@router.get("/full", response_model=WeatherFullResponse)
def get_full_weather(
    latitude: float = Query(..., ge=-90.0, le=90.0, description="Latitude of the location"),
    longitude: float = Query(..., ge=-180.0, le=180.0, description="Longitude of the location"),
    db: Session = Depends(get_db)
):
    """
    Returns the complete weather forecast package (Current, Hourly, and 10-day Daily forecast)
    for the nearest grid point using the default provider.
    """
    return handle_full_weather(latitude, longitude, db)

@router.get("/imd/full", response_model=WeatherFullResponse)
def get_imd_full_weather(
    latitude: float = Query(..., ge=-90.0, le=90.0, description="Latitude of the location"),
    longitude: float = Query(..., ge=-180.0, le=180.0, description="Longitude of the location"),
    db: Session = Depends(get_db)
):
    """
    Returns the complete weather forecast package (Current, Hourly, and 10-day Daily forecast)
    for the nearest grid point using the IMD provider.
    """
    return handle_full_weather(latitude, longitude, db, provider_name="imd")


def handle_current_weather(latitude: float, longitude: float, db: Session, provider_name: Optional[str] = None):
    provider = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()
    cache_key = f"weather_current_{provider}_{latitude:.4f}_{longitude:.4f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db, provider_name=provider)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        current_data = data["current"]
        set_cached_response(db, cache_key, current_data)
        return current_data
    except Exception as e:
        logger.error(f"Error handling current weather query ({provider}): {e}", exc_info=True)
        handle_pipeline_exception(e)

@router.get("/current", response_model=WeatherCurrentDTO)
def get_current_weather(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the current weather conditions for the location using the default provider."""
    return handle_current_weather(latitude, longitude, db)

@router.get("/imd/current", response_model=WeatherCurrentDTO)
def get_imd_current_weather(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the current weather conditions for the location using the IMD provider."""
    return handle_current_weather(latitude, longitude, db, provider_name="imd")


def handle_hourly_weather(latitude: float, longitude: float, db: Session, provider_name: Optional[str] = None):
    provider = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()
    cache_key = f"weather_hourly_{provider}_{latitude:.4f}_{longitude:.4f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db, provider_name=provider)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        hourly_data = data["hourly"]
        set_cached_response(db, cache_key, hourly_data)
        return hourly_data
    except Exception as e:
        logger.error(f"Error handling hourly weather query ({provider}): {e}", exc_info=True)
        handle_pipeline_exception(e)

@router.get("/hourly", response_model=WeatherHourlyResponse)
def get_hourly_forecast(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the hourly forecast for the next 24 hours using the default provider."""
    return handle_hourly_weather(latitude, longitude, db)

@router.get("/imd/hourly", response_model=WeatherHourlyResponse)
def get_imd_hourly_forecast(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the hourly forecast for the next 24 hours using the IMD provider."""
    return handle_hourly_weather(latitude, longitude, db, provider_name="imd")


def handle_daily_weather(latitude: float, longitude: float, db: Session, provider_name: Optional[str] = None):
    provider = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()
    cache_key = f"weather_daily_{provider}_{latitude:.4f}_{longitude:.4f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        return cached
        
    try:
        pipeline = WeatherPipeline(db, provider_name=provider)
        data = pipeline.get_or_create_forecast(latitude, longitude)
        daily_data = data["daily"]
        set_cached_response(db, cache_key, daily_data)
        return daily_data
    except Exception as e:
        logger.error(f"Error handling daily weather query ({provider}): {e}", exc_info=True)
        handle_pipeline_exception(e)

@router.get("/daily", response_model=WeatherDailyResponse)
def get_daily_forecast(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the daily forecast for the next 10 days using the default provider."""
    return handle_daily_weather(latitude, longitude, db)

@router.get("/imd/daily", response_model=WeatherDailyResponse)
def get_imd_daily_forecast(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    db: Session = Depends(get_db)
):
    """Returns the daily forecast for the next 10 days using the IMD provider."""
    return handle_daily_weather(latitude, longitude, db, provider_name="imd")


def handle_custom_weather(req: CustomWeatherRequest, db: Session, provider_name: Optional[str] = None):
    provider = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()
    cache_key = f"weather_custom_{provider}_{req.latitude:.4f}_{req.longitude:.4f}"
    
    cached = get_cached_response(db, cache_key)
    if cached:
        custom_data = cached
    else:
        try:
            pipeline = WeatherPipeline(db, provider_name=provider)
            custom_data = pipeline.get_custom_forecast(req.latitude, req.longitude)
            set_cached_response(db, cache_key, custom_data)
        except Exception as e:
            logger.error(f"Error handling custom weather query ({provider}): {e}", exc_info=True)
            handle_pipeline_exception(e)
            return None
            
    advisories = None
    if req.crop_stages:
        try:
            from backend.workers.advisory_engine import AdvisoryEngine
            pairs = [{"crop_id": cs.crop_id, "stage_id": cs.stage_id} for cs in req.crop_stages]
            advisories = AdvisoryEngine.evaluate_rules(db, req.latitude, req.longitude, pairs, provider_name=provider)
        except Exception as e:
            logger.error(f"Error evaluating crop advisories in custom weather ({provider}): {e}", exc_info=True)
            advisories = []
            
    return {
        "status": custom_data["status"],
        "message": custom_data["message"],
        "data": custom_data["data"],
        "advisories": advisories
    }

@router.post("/custom", response_model=WeatherCustomResponse)
def get_custom_weather(
    req: CustomWeatherRequest,
    db: Session = Depends(get_db)
):
    """
    Returns the custom Meteomatics-style weather forecast package using the default provider.
    Optionally evaluates and merges crop stage advisories if crop_stages is provided.
    """
    return handle_custom_weather(req, db)

@router.post("/imd/custom", response_model=WeatherCustomResponse)
def get_imd_custom_weather(
    req: CustomWeatherRequest,
    db: Session = Depends(get_db)
):
    """
    Returns the custom Meteomatics-style weather forecast package using the IMD provider.
    Optionally evaluates and merges crop stage advisories if crop_stages is provided.
    """
    return handle_custom_weather(req, db, provider_name="imd")

from backend.workers.scheduler import WeatherScheduler

def run_scheduler_task():
    try:
        scheduler = WeatherScheduler()
        scheduler.run_once()
    except Exception as e:
        logger.error(f"Background scheduler task failed: {e}", exc_info=True)

@router.post("/scheduler/run")
def trigger_scheduler_run(
    background_tasks: BackgroundTasks,
    secret: Optional[str] = Header(None, alias="X-Scheduler-Secret"),
    db: Session = Depends(get_db)
):
    """
    HTTP trigger to run the forecast ingestion cycle check.
    Secured by a secret header token (X-Scheduler-Secret).
    Runs asynchronously in the background to prevent Gateway 502 timeouts.
    """
    expected_secret = config.SCHEDULER_SECRET
    if not expected_secret:
        raise HTTPException(status_code=500, detail="SCHEDULER_SECRET environment variable is not configured on the server.")
        
    if secret != expected_secret:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid scheduler secret token.")
        
    background_tasks.add_task(run_scheduler_task)
    return {"status": "RUNNING", "message": "Scheduler execution started in the background."}

class AdvisoryRequest(BaseModel):
    latitude: float
    longitude: float
    crop_stages: List[CropStageQuery]

@router.get("/crops")
def get_crops(db: Session = Depends(get_db)):
    """List all supported crops."""
    stmt = select(Crop).order_by(Crop.name.asc())
    return db.execute(stmt).scalars().all()

@router.get("/crops/{crop_id}/stages")
def get_crop_stages(crop_id: int, db: Session = Depends(get_db)):
    """Get all growth stages for a crop."""
    stmt = select(CropStage).where(CropStage.crop_id == crop_id).order_by(CropStage.stage_order.asc())
    return db.execute(stmt).scalars().all()

def handle_advisories(req: AdvisoryRequest, db: Session, provider_name: Optional[str] = None):
    provider = (provider_name or config.WEATHER_DATA_PROVIDER).strip().lower()
    try:
        # Check cache / ensure weather data exists for the coordinates
        # We invoke get_or_create_forecast to trigger download/load of GRIB if missing
        pipeline = WeatherPipeline(db, provider_name=provider)
        pipeline.get_or_create_forecast(req.latitude, req.longitude)
        
        from backend.workers.advisory_engine import AdvisoryEngine
        pairs = [{"crop_id": cs.crop_id, "stage_id": cs.stage_id} for cs in req.crop_stages]
        advisories = AdvisoryEngine.evaluate_rules(db, req.latitude, req.longitude, pairs, provider_name=provider)
        return {"latitude": req.latitude, "longitude": req.longitude, "advisories": advisories}
    except Exception as e:
        logger.error(f"Error evaluating crop advisories ({provider}): {e}", exc_info=True)
        handle_pipeline_exception(e)

@router.post("/advisory")
def get_advisories(req: AdvisoryRequest, db: Session = Depends(get_db)):
    """Evaluate weather forecast against crop rules and return alerts + advisories using the default provider."""
    return handle_advisories(req, db)

@router.post("/imd/advisory")
def get_imd_advisories(req: AdvisoryRequest, db: Session = Depends(get_db)):
    """Evaluate weather forecast against crop rules and return alerts + advisories using the IMD provider."""
    return handle_advisories(req, db, provider_name="imd")
