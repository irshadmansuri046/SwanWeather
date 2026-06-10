import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def get_database_url():
    return os.getenv("DATABASE_URL", "postgresql://postgres:postgrespassword@localhost:5432/weatherdb")

def get_environment():
    return os.getenv("ENVIRONMENT", "development")

def get_port():
    return int(os.getenv("PORT", "8000"))

def get_log_level():
    return os.getenv("LOG_LEVEL", "INFO")

def get_supabase_url():
    return os.getenv("SUPABASE_URL")

def get_supabase_key():
    return os.getenv("SUPABASE_KEY")

def get_supabase_service_role_key():
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY")

def get_raw_grib_storage_dir():
    return os.getenv("RAW_GRIB_STORAGE_DIR", "./data/raw_grib")

def get_use_local_storage():
    return os.getenv("USE_LOCAL_STORAGE", "true").lower() == "true"

def get_allow_on_the_fly_downloads():
    return os.getenv("ALLOW_ON_THE_FLY_DOWNLOADS", "true").lower() == "true"

def get_ecmwf_data_source():
    return os.getenv("ECMWF_DATA_SOURCE", "azure")

def get_weather_data_provider():
    return os.getenv("WEATHER_DATA_PROVIDER", "ecmwf")

def get_imd_api_key():
    return os.getenv("IMD_API_KEY", "482b1874147b5ba1fd9eb8fd5da2b72280dd29108a859b9b2d9364d68651c77c")

def get_retention_days():
    return int(os.getenv("RETENTION_DAYS", "2"))

def get_scheduler_secret():
    return os.getenv("SCHEDULER_SECRET")

# Mapping of attribute names to their getter functions
_GETTERS = {
    "DATABASE_URL": get_database_url,
    "ENVIRONMENT": get_environment,
    "PORT": get_port,
    "LOG_LEVEL": get_log_level,
    "SUPABASE_URL": get_supabase_url,
    "SUPABASE_KEY": get_supabase_key,
    "SUPABASE_SERVICE_ROLE_KEY": get_supabase_service_role_key,
    "RAW_GRIB_STORAGE_DIR": get_raw_grib_storage_dir,
    "USE_LOCAL_STORAGE": get_use_local_storage,
    "ALLOW_ON_THE_FLY_DOWNLOADS": get_allow_on_the_fly_downloads,
    "ECMWF_DATA_SOURCE": get_ecmwf_data_source,
    "WEATHER_DATA_PROVIDER": get_weather_data_provider,
    "IMD_API_KEY": get_imd_api_key,
    "RETENTION_DAYS": get_retention_days,
    "SCHEDULER_SECRET": get_scheduler_secret,
}

def __getattr__(name):
    if name in _GETTERS:
        return _GETTERS[name]()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
