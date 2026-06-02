# Development & Deployment Guide

This guide covers setting up the ECMWF Weather Platform locally for development and deploying it to production on Supabase.

---

## 1. Local Development Setup

### 1.1 Prerequisites
- Docker & Docker Compose
- Python 3.12+ (if running without containers)
- system eccodes library (if running parser without Docker):
  - macOS: `brew install eccodes`
  - Linux (Ubuntu/Debian): `sudo apt-get install libeccodes-dev`

### 1.2 Setup Environment
1. Clone/navigate to the codebase folder:
   ```bash
   cd weather-platform
   ```
2. Copy environment template to `.env`:
   ```bash
   cp .env.example .env
   ```
3. Boot the environment using Docker Compose:
   ```bash
   docker-compose up --build
   ```
   *This starts the PostgreSQL database, FastAPI app on port 8000, and the periodic checker worker.*

4. Go to [http://localhost:8000/docs](http://localhost:8000/docs) in your browser to interact with the API endpoints.

### 1.3 Running Tests
Run tests inside the API container or locally:
```bash
docker-compose exec api pytest tests/
```

---

## 2. Supabase Production Setup

To host the weather database on Supabase Postgres:

### 2.1 Schema Provisioning
1. Open your project on the **Supabase Dashboard**.
2. Click on the **SQL Editor** tab in the sidebar.
3. Create a new query, paste the contents of [init_db.sql](file:///Users/irshadmansuri/Documents/Antigravity/WeatherBackend/weather-platform/backend/database/init_db.sql), and click **Run**.
4. This will create the indexes, partitions, and tables (`weather_jobs`, `weather_raw_files`, `weather_forecast`, `weather_daily`, `api_cache`).

### 2.2 Storage Bucket Creation (Optional)
If you want to archive raw GRIB files in Supabase Storage instead of local disks:
1. Go to **Storage** in the Supabase Dashboard.
2. Click **New Bucket**, name it `raw-grib-archive`, and make it private.
3. Obtain the Supabase access keys and supply them as environment variables (see below).

---

## 3. Production Deployment (Docker)

To deploy the FastAPI service and Ingestion worker to standard container clouds (e.g. AWS ECS, GCP Cloud Run, Render, or DigitalOcean App Platform):

### 3.1 Required Production Environment Variables
Configure the following variables in your hosting panel:

| Variable | Description | Example |
| :--- | :--- | :--- |
| `DATABASE_URL` | Direct connection string to Supabase Postgres | `postgresql://postgres.[id]:[pass]@aws-0-[loc].pooler.supabase.com:5432/postgres` |
| `ENVIRONMENT` | Run environment mode | `production` |
| `PORT` | API server listening port | `8000` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `SUPABASE_URL` | Supabase endpoint URL | `https://your-project.supabase.co` |
| `SUPABASE_KEY` | Supabase Anon Key | `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...` |
| `USE_LOCAL_STORAGE` | Storage mode (set to false for Supabase Storage) | `true` |

### 3.2 Build and Run Production Docker Containers

#### API Service
Build the image:
```bash
docker build -t weather-api -f docker/Dockerfile.backend .
```
Run the API container:
```bash
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  --name weather_api_prod \
  weather-api
```

#### Scheduler Ingestion Worker
To run the scheduler worker, run the same image but overwrite the entrypoint to boot the scheduler daemon:
```bash
docker run -d \
  --env-file .env \
  --name weather_worker_prod \
  --entrypoint python \
  weather-api -m backend.workers.scheduler
```

---

## 4. API Endpoint Specifications

All weather responses support **Gzip compression** and round locations automatically to the nearest 0.25° grid point, ensuring sub-300ms query latency.

### 4.1 GET `/weather/full`
Returns complete current conditions, 24-hour hourly, and 10-day daily forecast summaries.
- **Parameters:**
  - `latitude` (float, required): `-90` to `90`.
  - `longitude` (float, required): `-180` to `180`.
- **Response Format:**
  ```json
  {
    "latitude": 51.5,
    "longitude": -0.25,
    "cycle_time": "2026-06-02T12:00:00",
    "current": {
      "temperature": 16.5,
      "feels_like": 16.5,
      "humidity": 72.4,
      "pressure": 1013.25,
      "cloud_cover": 42.0,
      "wind_speed": 4.2,
      "wind_direction": 180.0,
      "dew_point": 11.2,
      "uv_index": 4.5,
      "visibility": 10000.0,
      "weather_code": "PARTLY_CLOUDY",
      "valid_time": "2026-06-02T12:00:00"
    },
    "hourly": [
      {
        "temperature": 16.5,
        "feels_like": 16.5,
        "precipitation": 0.0,
        "humidity": 72.4,
        "pressure": 1013.25,
        "cloud_cover": 42.0,
        "wind_speed": 4.2,
        "wind_direction": 180.0,
        "weather_code": "PARTLY_CLOUDY",
        "forecast_time": "2026-06-02T12:00:00"
      }
    ],
    "daily": [
      {
        "forecast_date": "2026-06-02",
        "temp_min": 11.5,
        "temp_max": 18.2,
        "precipitation": 0.0,
        "humidity_avg": 70.5,
        "wind_speed_max": 6.5,
        "weather_code": "PARTLY_CLOUDY"
      }
    ]
  }
  ```

### 4.2 GET `/weather/current`
Returns only current conditions for coordinate.

### 4.3 GET `/weather/hourly`
Returns a 24-hour hourly list.

### 4.4 GET `/weather/daily`
Returns a 10-day daily summary list.
