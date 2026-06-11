import uuid
import datetime as dt_module
from sqlalchemy import Column, String, DateTime, Numeric, BigInteger, Text, ForeignKey, Date, JSON, Index, func, Integer
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects.postgresql import UUID
from .connection import Base

class TZDateTime(TypeDecorator):
    """DateTime type that ensures datetime objects are timezone-aware UTC on retrieval and bind,

    even when using database engines like SQLite that do not store timezone info.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt_module.timezone.utc)
            else:
                value = value.astimezone(dt_module.timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt_module.timezone.utc)
        return value

class WeatherJob(Base):
    __tablename__ = "weather_jobs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_time = Column(TZDateTime, nullable=False)
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING, RUNNING, SUCCESS, FAILED
    error_message = Column(Text, nullable=True)
    provider = Column(String(20), nullable=False, default="ecmwf")
    created_at = Column(TZDateTime, server_default=func.now())
    updated_at = Column(TZDateTime, server_default=func.now(), onupdate=func.now())

class WeatherRawFile(Base):
    __tablename__ = "weather_raw_files"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("weather_jobs.id"), nullable=True)
    cycle_time = Column(TZDateTime, nullable=False)
    file_path = Column(String(255), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    checksum = Column(String(64), nullable=False)
    provider = Column(String(20), nullable=False, default="ecmwf")
    created_at = Column(TZDateTime, server_default=func.now())

class WeatherForecast(Base):
    __tablename__ = "weather_forecast"
    __table_args__ = (
        Index("idx_weather_forecast_lat_lon_time", "latitude", "longitude", "forecast_time", "provider"),
        {"postgresql_partition_by": "RANGE (forecast_time)"}
    )
    
    latitude = Column(Numeric(5, 2), primary_key=True, nullable=False)  # Part of primary key due to partitioning constraints
    longitude = Column(Numeric(5, 2), primary_key=True, nullable=False)
    forecast_time = Column(TZDateTime, primary_key=True, nullable=False)
    provider = Column(String(20), primary_key=True, nullable=False, default="ecmwf")
    cycle_time = Column(TZDateTime, nullable=False)
    
    temperature = Column(Numeric(5, 2), nullable=False)
    feels_like = Column(Numeric(5, 2), nullable=False)
    precipitation = Column(Numeric(5, 2), nullable=False)
    humidity = Column(Numeric(5, 2), nullable=False)
    pressure = Column(Numeric(6, 2), nullable=False)
    cloud_cover = Column(Numeric(5, 2), nullable=False)
    wind_speed = Column(Numeric(5, 2), nullable=False)
    wind_direction = Column(Numeric(5, 2), nullable=False)
    dew_point = Column(Numeric(5, 2), nullable=False)
    uv_index = Column(Numeric(4, 2), nullable=False)
    visibility = Column(Numeric(8, 2), nullable=False)
    weather_code = Column(String(30), nullable=False)
    
    created_at = Column(TZDateTime, server_default=func.now())

class WeatherDaily(Base):
    __tablename__ = "weather_daily"
    __table_args__ = (
        Index("idx_weather_daily_lat_lon_date", "latitude", "longitude", "forecast_date", "provider"),
    )
    
    latitude = Column(Numeric(5, 2), primary_key=True, nullable=False)
    longitude = Column(Numeric(5, 2), primary_key=True, nullable=False)
    forecast_date = Column(Date, primary_key=True, nullable=False)
    provider = Column(String(20), primary_key=True, nullable=False, default="ecmwf")
    cycle_time = Column(TZDateTime, nullable=False)
    
    temp_min = Column(Numeric(5, 2), nullable=False)
    temp_max = Column(Numeric(5, 2), nullable=False)
    precipitation = Column(Numeric(5, 2), nullable=False)
    humidity_avg = Column(Numeric(5, 2), nullable=False)
    wind_speed_max = Column(Numeric(5, 2), nullable=False)
    weather_code = Column(String(30), nullable=False)
    
    created_at = Column(TZDateTime, server_default=func.now())
    updated_at = Column(TZDateTime, server_default=func.now(), onupdate=func.now())

class ApiCache(Base):
    __tablename__ = "api_cache"
    
    cache_key = Column(String(255), primary_key=True)
    cache_value = Column(JSON, nullable=False)
    expires_at = Column(TZDateTime, nullable=False)
    created_at = Column(TZDateTime, server_default=func.now())

class Crop(Base):
    __tablename__ = "crops"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    category = Column(String(50), nullable=False)
    created_at = Column(TZDateTime, server_default=func.now())

class CropStage(Base):
    __tablename__ = "crop_stages"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    crop_id = Column(Integer, ForeignKey("crops.id", ondelete="CASCADE"), nullable=False)
    stage_name = Column(String(100), nullable=False)
    stage_order = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
 
class CropStageRule(Base):
    __tablename__ = "crop_stage_rules"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    crop_stage_id = Column(Integer, ForeignKey("crop_stages.id", ondelete="CASCADE"), nullable=False)
    rule_name = Column(String(100), nullable=False)
    parameter = Column(String(50), nullable=False)  # temp_max, temp_min, precipitation, humidity_avg, wind_speed_max
    operator = Column(String(5), nullable=False)     # >, <, >=, <=
    threshold_value = Column(Numeric(5, 2), nullable=False)
    severity = Column(String(20), nullable=False, default="WARNING")
    advisory_text = Column(Text, nullable=False)
    created_at = Column(TZDateTime, server_default=func.now())
