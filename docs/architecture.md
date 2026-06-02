# System Architecture & Design

This document details the software architecture, database design, and workflow sequence diagrams for the ECMWF Weather Platform.

---

## 1. System Architecture Diagram

```mermaid
graph TD
    ECMWF[ECMWF Open Data Portal] -->|Retrieves GRIB files| Downloader[ECMWFDownloader]
    Downloader -->|Saves GRIB locally / Supabase Storage| GRIB_Storage[(GRIB raw storage)]
    Downloader -->|Log Job Metadata| DB_Jobs[(weather_jobs / weather_raw_files)]
    
    Mobile[Mobile App Client] -->|GET /weather/full| FastAPI[FastAPI API Service]
    FastAPI -->|Query check| DB_Forecast[(weather_forecast / weather_daily)]
    
    FastAPI -->|On Cache Miss: Request Grid Data| Pipeline[WeatherPipeline]
    Pipeline -->|Loads file| GRIB_Storage
    Pipeline -->|Extract point series| Parser[GRIBParser]
    Parser -->|Selects coordinates| xarray[xarray / cfgrib]
    
    Pipeline -->|Interpolate and Compute| Transform[WeatherComputation]
    Transform -->|Insert grid forecasts| DB_Forecast
    DB_Forecast -->|Return JSON response| FastAPI
    FastAPI -->|Gzipped response| Mobile
```

---

## 2. Sequence Diagrams

### 2.1 Periodical Ingestion Workflow (6-Hourly Scheduler)

The scheduler checks every 30 minutes for new ECMWF forecast cycles (00, 06, 12, 18 UTC) and downloads them.

```mermaid
sequenceDiagram
    autonumber
    participant Scheduler as WeatherScheduler
    participant DB as PostgreSQL (Supabase)
    participant ECMWF as ECMWF Open Data API
    participant FS as Local / Cloud Storage

    Scheduler->>Scheduler: Calculate latest available cycle (e.g. 12 UTC)
    Scheduler->>DB: Query WeatherJob for status of cycle=12 UTC
    alt Cycle Already Logged (SUCCESS or RUNNING)
        DB-->>Scheduler: Job entry exists
        Scheduler->>Scheduler: Skip download
    else Cycle Missing
        DB-->>Scheduler: No successful job found
        Scheduler->>DB: Insert WeatherJob (status='RUNNING')
        Scheduler->>ECMWF: Request cycle GRIB (temp, pressure, precip, wind...)
        ECMWF-->>Scheduler: Stream GRIB file (.grib2)
        Scheduler->>FS: Save file and calculate SHA-256 Checksum
        Scheduler->>DB: Insert WeatherRawFile entry
        Scheduler->>DB: Update WeatherJob (status='SUCCESS')
        Scheduler->>DB: Prune records older than 90 days
    end
```

### 2.2 On-Demand Mobile API Fetch Workflow (Cache Hit vs Cache Miss)

Mobile clients request the weather forecast for arbitrary latitudes and longitudes. 

```mermaid
sequenceDiagram
    autonumber
    actor Mobile as Mobile Application
    participant API as FastAPI Service
    participant Cache as Response Cache (api_cache)
    participant DB as Forecast DB (weather_forecast)
    participant Pipeline as WeatherPipeline
    participant GRIB as GRIB File (.grib2)

    Mobile->>API: GET /weather/full?latitude=51.5&longitude=-0.12
    API->>API: Round coordinates to nearest 0.25 grid point (51.5, -0.25)
    API->>Cache: Look up cached JSON for (51.5, -0.25)
    
    alt HTTP Cache Hit (< 5ms)
        Cache-->>API: Return cached JSON payload
        API-->>Mobile: 200 OK (Gzipped JSON response)
    else HTTP Cache Miss
        Cache-->>API: Cache miss
        API->>DB: Query weather_forecast for lat=51.5, lon=-0.25 and latest_cycle_time
        
        alt DB Forecast Hit (< 20ms)
            DB-->>API: Return hourly list & daily list
            API->>Cache: Store JSON payload (expires in 15 mins)
            API-->>Mobile: 200 OK (Gzipped JSON response)
        else DB Forecast Miss (First query or new cycle)
            API->>Pipeline: Extract weather data on-the-fly
            Pipeline->>Pipeline: Locate GRIB file for latest cycle
            Pipeline->>GRIB: Open dataset via xarray / cfgrib
            Pipeline->>GRIB: Slice grid at nearest coordinate (51.5, -0.25)
            Pipeline->>Pipeline: Interpolate 3-hourly steps to 1-hourly
            Pipeline->>Pipeline: Compute feels-like, UV index, and weather condition codes
            Pipeline->>DB: Bulk insert hourly forecasts (weather_forecast)
            Pipeline->>Pipeline: Aggregate hourly forecasts into 10-day daily records
            Pipeline->>DB: Bulk insert daily summaries (weather_daily)
            Pipeline->>API: Return computed full weather structure
            API->>Cache: Store JSON payload (expires in 15 mins)
            API-->>Mobile: 200 OK (Gzipped JSON response)
        end
    end
```

---

## 3. Database Schema

### 3.1 `weather_jobs`
Tracks GRIB download jobs.
- `id` (UUID): Primary key.
- `cycle_time` (timestamp with time zone): The UTC start time of the forecast run (00, 06, 12, or 18 UTC).
- `status` (varchar): Ingestion status (`PENDING`, `RUNNING`, `SUCCESS`, `FAILED`).
- `error_message` (text): Stacktrace / description if job failed.

### 3.2 `weather_raw_files`
Tracks downloaded physical GRIB files.
- `id` (UUID): Primary key.
- `job_id` (UUID): Reference to the creator job.
- `cycle_time` (timestamp with time zone).
- `file_path` (varchar): Absolute location path of GRIB file.
- `file_size` (bigint): File size in bytes.
- `checksum` (varchar): SHA-256 hash.

### 3.3 `weather_forecast` (Partitioned table)
Hourly forecast coordinates. Partitioned by range on `forecast_time`.
- `latitude` (numeric): Point latitude.
- `longitude` (numeric): Point longitude.
- `forecast_time` (timestamp with time zone): Target hour for the forecast.
- `cycle_time` (timestamp with time zone): The ECMWF cycle this forecast was generated from.
- `temperature` (numeric): °C.
- `feels_like` (numeric): °C.
- `precipitation` (numeric): mm.
- `humidity` (numeric): %.
- `pressure` (numeric): hPa.
- `cloud_cover` (numeric): %.
- `wind_speed` (numeric): m/s.
- `wind_direction` (numeric): Degrees.
- `dew_point` (numeric): °C.
- `uv_index` (numeric): 0 to 15.
- `visibility` (numeric): Meters.
- `weather_code` (varchar): `CLEAR`, `PARTLY_CLOUDY`, `CLOUDY`, `RAIN`, `THUNDER`, `SNOW`, `FOG`, `WINDY`.

### 3.4 `weather_daily`
10-day summary aggregate forecasts.
- `latitude` (numeric).
- `longitude` (numeric).
- `forecast_date` (date).
- `cycle_time` (timestamp with time zone).
- `temp_min` (numeric): °C.
- `temp_max` (numeric): °C.
- `precipitation` (numeric): Total rainfall for the day in mm.
- `humidity_avg` (numeric): %.
- `wind_speed_max` (numeric): m/s.
- `weather_code` (varchar): Dominant weather condition.
