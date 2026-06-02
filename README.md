# ECMWF Weather Backend Platform

A production-grade weather backend platform that downloads forecast data from ECMWF Open Data, processes GRIB parameters, stores weather records in a Supabase PostgreSQL database, and exposes highly optimized APIs for mobile clients.

## Tech Stack
- **Language:** Python 3.12
- **API Framework:** FastAPI
- **Database:** PostgreSQL (Supabase compatible)
- **Meteorological Parsing:** xarray, cfgrib, eccodes, ecmwf-opendata
- **ORM:** SQLAlchemy (2.0+)
- **Validation:** Pydantic
- **Containerization:** Docker & Docker Compose

## Quick Start (Local Development)

1. Clone or navigate to the directory:
   ```bash
   cd weather-platform
   ```

2. Copy environment template:
   ```bash
   cp .env.example .env
   ```

3. Spin up the backend, database, and ingestion worker containers:
   ```bash
   docker-compose up --build
   ```

4. View API documentation at [http://localhost:8000/docs](http://localhost:8000/docs).
