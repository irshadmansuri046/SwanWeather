import pytest
from datetime import datetime, date, timezone, timedelta
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.database.connection import get_db
from backend.workers import calculations
from backend.workers.pipeline import WeatherPipeline
from backend.database.models import WeatherForecast, WeatherDaily, WeatherJob, WeatherRawFile

client = TestClient(app)

# Mock coordinates
LAT = 19.07
LON = 72.88
LAT_GRID = 19.0
LON_GRID = 73.0

def test_calculations_absolute_humidity():
    # standard values
    ah = calculations.calculate_absolute_humidity(25.0, 60.0)
    assert ah > 0
    # boundary values
    assert calculations.calculate_absolute_humidity(25.0, -10.0) == calculations.calculate_absolute_humidity(25.0, 0.0)
    assert calculations.calculate_absolute_humidity(25.0, 110.0) == calculations.calculate_absolute_humidity(25.0, 100.0)

def test_calculations_soil_frost():
    assert calculations.calculate_soil_frost(-5.0) == 100.0
    assert calculations.calculate_soil_frost(0.0) == 100.0
    assert calculations.calculate_soil_frost(5.0) == 0.0
    assert calculations.calculate_soil_frost(10.0) == 0.0
    assert 0.0 < calculations.calculate_soil_frost(2.5) < 100.0

def test_calculations_sunrise_sunset():
    d = date(2026, 6, 4)
    rise, sunset = calculations.calculate_sunrise_sunset(LAT_GRID, LON_GRID, d)
    assert rise is not None
    assert sunset is not None
    assert rise < sunset

def test_calculations_moon_info():
    d = date(2026, 6, 4)
    age, phase = calculations.calculate_moon_info(d)
    assert 0.0 <= age <= 29.6
    assert 0.0 <= phase <= 1.0

def test_calculations_moonrise_moonset():
    rise, sunset = 1780617600, 1780660800  # arbitrary timestamps
    mrise, mset = calculations.calculate_moonrise_moonset(rise, sunset, 0.5)
    assert mrise > rise
    assert mset > sunset

@pytest.fixture(autouse=True)
def clean_db(db_session):
    """Ensure a clean database state before each test run by truncating tables."""
    db_session.query(WeatherForecast).delete()
    db_session.query(WeatherDaily).delete()
    db_session.query(WeatherRawFile).delete()
    db_session.query(WeatherJob).delete()
    db_session.commit()

@pytest.mark.usefixtures("db_session")
def test_pipeline_get_custom_forecast(db_session):
    cycle_dt = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    
    # 1. Register successful job
    job = WeatherJob(cycle_time=cycle_dt, status="SUCCESS")
    db_session.add(job)
    
    # 2. Add 24 hours of forecast records
    forecasts = []
    for h in range(24):
        f_time = cycle_dt + timedelta(hours=h)
        forecasts.append(WeatherForecast(
            latitude=LAT_GRID,
            longitude=LON_GRID,
            forecast_time=f_time,
            cycle_time=cycle_dt,
            temperature=25.0 + h * 0.1,
            feels_like=26.0 + h * 0.1,
            precipitation=0.1 if h % 6 == 0 else 0.0,
            humidity=60.0,
            pressure=1010.0,
            cloud_cover=10.0,
            wind_speed=2.0,
            wind_direction=180.0,
            dew_point=16.0,
            uv_index=5.0,
            visibility=10000.0,
            weather_code="CLEAR"
        ))
    db_session.bulk_save_objects(forecasts)
    
    # 3. Add 10 days of daily records
    dailies = []
    for d in range(10):
        f_date = cycle_dt.date() + timedelta(days=d)
        dailies.append(WeatherDaily(
            latitude=LAT_GRID,
            longitude=LON_GRID,
            forecast_date=f_date,
            cycle_time=cycle_dt,
            temp_min=20.0,
            temp_max=30.0,
            precipitation=0.5,
            humidity_avg=60.0,
            wind_speed_max=5.0,
            weather_code="CLEAR"
        ))
    db_session.bulk_save_objects(dailies)
    db_session.commit()
    
    pipeline = WeatherPipeline(db_session)
    # Mock download / extraction logic so it just reads from DB (cache hit behavior of get_or_create_forecast)
    with patch.object(pipeline, "get_or_create_forecast") as mock_get_or_create:
        mock_get_or_create.return_value = {}  # get_or_create_forecast doesn't need to return anything here because get_custom_forecast does its own queries
        res = pipeline.get_custom_forecast(LAT, LON)
        
    assert res["status"] == 200
    assert "data" in res
    data = res["data"]
    assert "daily" in data
    assert "weekly" in data
    assert "common" in data
    
    # Check parameters list
    daily_params = [p["parameter"] for p in data["daily"]]
    assert "temperature" in daily_params
    assert "soil_frost" in daily_params
    
    # Check specific calculation results inside response
    temp_param = next(p for p in data["daily"] if p["parameter"] == "temperature")
    assert len(temp_param["coordinates"][0]["dates"]) == 24
    
    weekly_params = [p["parameter"] for p in data["weekly"]]
    assert "min_temperature" in weekly_params
    assert "precipitation_probability" in weekly_params
    
    common_params = [p["parameter"] for p in data["common"]]
    assert "moon_age" in common_params
    assert "sunrise" in common_params
    assert "moonrise" in common_params

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = None
    return db

@pytest.fixture
def override_db(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()

@patch("backend.api.routes.WeatherPipeline")
def test_route_custom_weather(mock_pipeline_class, override_db):
    mock_pipeline = MagicMock()
    # Mock custom forecast response matching WeatherCustomResponse
    mock_pipeline.get_custom_forecast.return_value = {
        "status": 200,
        "message": "weather data fetched successfully",
        "data": {
            "daily": [{
                "parameter": "temperature",
                "raw_key": "t_2m:C",
                "height": "2m",
                "unit": "°C",
                "label": "TEMPERATURE",
                "coordinates": [{
                    "lat": LAT_GRID,
                    "lon": LON_GRID,
                    "dates": [{"date": "2026-06-04T12:00:00Z", "value": 25.0}]
                }]
            }],
            "weekly": [],
            "common": []
        }
    }
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.post("/weather/custom", json={"latitude": LAT, "longitude": LON})
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == 200
    assert json_data["data"]["daily"][0]["parameter"] == "temperature"
    assert json_data["data"]["daily"][0]["coordinates"][0]["dates"][0]["value"] == 25.0
