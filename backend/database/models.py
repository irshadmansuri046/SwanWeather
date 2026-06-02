import uuid
from sqlalchemy import Column, String, DateTime, Numeric, BigInteger, Text, ForeignKey, Date, JSON, Index, func
from sqlalchemy.dialects.postgresql import UUID
from .connection import Base

class WeatherJob(Base):
    __tablename__ = "weather_jobs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING, RUNNING, SUCCESS, FAILED
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class WeatherRawFile(Base):
    __tablename__ = "weather_raw_files"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("weather_jobs.id"), nullable=True)
    cycle_time = Column(DateTime(timezone=True), nullable=False)
    file_path = Column(String(255), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    checksum = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class WeatherForecast(Base):
    __tablename__ = "weather_forecast"
    __table_args__ = (
        Index("idx_weather_forecast_lat_lon_time", "latitude", "longitude", "forecast_time"),
        {"postgresql_partition_by": "RANGE (forecast_time)"}
    )
    
    latitude = Column(Numeric(5, 2), primary_key=True, nullable=False)  # Part of primary key due to partitioning constraints
    longitude = Column(Numeric(5, 2), primary_key=True, nullable=False)
    forecast_time = Column(DateTime(timezone=True), primary_key=True, nullable=False)
    cycle_time = Column(DateTime(timezone=True), nullable=False)
    
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
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class WeatherDaily(Base):
    __tablename__ = "weather_daily"
    __table_args__ = (
        Index("idx_weather_daily_lat_lon_date", "latitude", "longitude", "forecast_date"),
    )
    
    latitude = Column(Numeric(5, 2), primary_key=True, nullable=False)
    longitude = Column(Numeric(5, 2), primary_key=True, nullable=False)
    forecast_date = Column(Date, primary_key=True, nullable=False)
    cycle_time = Column(DateTime(timezone=True), nullable=False)
    
    temp_min = Column(Numeric(5, 2), nullable=False)
    temp_max = Column(Numeric(5, 2), nullable=False)
    precipitation = Column(Numeric(5, 2), nullable=False)
    humidity_avg = Column(Numeric(5, 2), nullable=False)
    wind_speed_max = Column(Numeric(5, 2), nullable=False)
    weather_code = Column(String(30), nullable=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class ApiCache(Base):
    __tablename__ = "api_cache"
    
    cache_key = Column(String(255), primary_key=True)
    cache_value = Column(JSON, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
