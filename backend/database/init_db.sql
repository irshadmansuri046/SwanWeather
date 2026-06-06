-- init_db.sql
-- Database initialization for ECMWF Weather Platform

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Weather Ingestion Jobs
CREATE TABLE IF NOT EXISTS weather_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    cycle_time TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Weather Raw Files Metadata
CREATE TABLE IF NOT EXISTS weather_raw_files (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID REFERENCES weather_jobs(id) ON DELETE SET NULL,
    cycle_time TIMESTAMP WITH TIME ZONE NOT NULL,
    file_path VARCHAR(255) NOT NULL,
    file_size BIGINT NOT NULL,
    checksum VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Weather Forecast (Hourly step-by-step) - Partitioned by forecast_time
CREATE TABLE IF NOT EXISTS weather_forecast (
    latitude NUMERIC(5, 2) NOT NULL,
    longitude NUMERIC(5, 2) NOT NULL,
    forecast_time TIMESTAMP WITH TIME ZONE NOT NULL,
    cycle_time TIMESTAMP WITH TIME ZONE NOT NULL,
    
    temperature NUMERIC(5, 2) NOT NULL,
    feels_like NUMERIC(5, 2) NOT NULL,
    precipitation NUMERIC(5, 2) NOT NULL,
    humidity NUMERIC(5, 2) NOT NULL,
    pressure NUMERIC(6, 2) NOT NULL,
    cloud_cover NUMERIC(5, 2) NOT NULL,
    wind_speed NUMERIC(5, 2) NOT NULL,
    wind_direction NUMERIC(5, 2) NOT NULL,
    dew_point NUMERIC(5, 2) NOT NULL,
    uv_index NUMERIC(4, 2) NOT NULL,
    visibility NUMERIC(8, 2) NOT NULL,
    weather_code VARCHAR(30) NOT NULL,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (latitude, longitude, forecast_time)
) PARTITION BY RANGE (forecast_time);

-- Create DEFAULT partition for weather_forecast to prevent insertion crashes
CREATE TABLE IF NOT EXISTS weather_forecast_default PARTITION OF weather_forecast DEFAULT;

-- Indexes for fast query retrieval
CREATE INDEX IF NOT EXISTS idx_weather_forecast_lat_lon_time 
ON weather_forecast(latitude, longitude, forecast_time DESC);

-- 4. Weather Daily Summaries (10-day forecasts)
CREATE TABLE IF NOT EXISTS weather_daily (
    latitude NUMERIC(5, 2) NOT NULL,
    longitude NUMERIC(5, 2) NOT NULL,
    forecast_date DATE NOT NULL,
    cycle_time TIMESTAMP WITH TIME ZONE NOT NULL,
    
    temp_min NUMERIC(5, 2) NOT NULL,
    temp_max NUMERIC(5, 2) NOT NULL,
    precipitation NUMERIC(5, 2) NOT NULL,
    humidity_avg NUMERIC(5, 2) NOT NULL,
    wind_speed_max NUMERIC(5, 2) NOT NULL,
    weather_code VARCHAR(30) NOT NULL,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (latitude, longitude, forecast_date)
);

CREATE INDEX IF NOT EXISTS idx_weather_daily_lat_lon_date 
ON weather_daily(latitude, longitude, forecast_date DESC);

-- 5. API Response Cache
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR(255) PRIMARY KEY,
    cache_value JSONB NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index for expiration cleanup
CREATE INDEX IF NOT EXISTS idx_api_cache_expires_at ON api_cache(expires_at);

-- 6. Crops Table
CREATE TABLE IF NOT EXISTS crops (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    category VARCHAR(50) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 7. Crop Growth Stages
CREATE TABLE IF NOT EXISTS crop_stages (
    id SERIAL PRIMARY KEY,
    crop_id INT NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    stage_name VARCHAR(100) NOT NULL,
    stage_order INT NOT NULL,
    description TEXT,
    UNIQUE(crop_id, stage_name)
);

-- 8. Crop Stage Weather Rules (Thresholds)
CREATE TABLE IF NOT EXISTS crop_stage_rules (
    id SERIAL PRIMARY KEY,
    crop_stage_id INT NOT NULL REFERENCES crop_stages(id) ON DELETE CASCADE,
    rule_name VARCHAR(100) NOT NULL,
    parameter VARCHAR(50) NOT NULL,
    operator VARCHAR(5) NOT NULL,
    threshold_value NUMERIC(5, 2) NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'WARNING',
    advisory_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
