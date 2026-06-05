import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
from backend.api.main import app
from backend.database.connection import get_db

client = TestClient(app)

# Mock pipeline response structure
MOCK_WEATHER_RESPONSE = {
    "latitude": 19.0,
    "longitude": 73.0,
    "cycle_time": "2026-06-02 05:30:00 PM",
    "current": {
        "time": "2026-06-02 08:30:00 PM",
        "temperature_2m": 28.5,
        "apparent_temperature": 33.2,
        "relative_humidity_2m": 78.0,
        "surface_pressure": 1008.0,
        "cloud_cover": 50.0,
        "wind_speed_10m": 4.5,
        "wind_direction_10m": 220.0,
        "dew_point_2m": 24.0,
        "uv_index": 8.0,
        "visibility": 10000.0,
        "precipitation": 0.5,
        "weather_code": "RAIN"
    },
    "hourly": {
        "time": ["2026-06-02 08:30:00 PM"],
        "temperature_2m": [28.5],
        "apparent_temperature": [33.2],
        "precipitation": [0.5],
        "relative_humidity_2m": [78.0],
        "surface_pressure": [1008.0],
        "cloud_cover": [50.0],
        "wind_speed_10m": [4.5],
        "wind_direction_10m": [220.0],
        "weather_code": ["RAIN"]
    },
    "daily": {
        "time": ["2026-06-02"],
        "temperature_2m_max": [30.0],
        "temperature_2m_min": [25.0],
        "precipitation_sum": [5.0],
        "humidity_avg": [78.0],
        "wind_speed_10m_max": [6.0],
        "weather_code": ["RAIN"]
    }
}

@pytest.fixture
def mock_db():
    db = MagicMock()
    # Mocking cache check to return None (cache miss)
    db.execute.return_value.scalars.return_value.first.return_value = None
    return db

@pytest.fixture
def override_db(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    yield
    app.dependency_overrides.clear()

@patch("backend.api.routes.WeatherPipeline")
def test_get_full_weather(mock_pipeline_class, override_db):
    # Mock pipeline instance method get_or_create_forecast
    mock_pipeline = MagicMock()
    mock_pipeline.get_or_create_forecast.return_value = MOCK_WEATHER_RESPONSE
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.get("/weather/full?latitude=19.0&longitude=73.0")
    
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["latitude"] == 19.0
    assert json_data["current"]["weather_code"] == "RAIN"
    assert len(json_data["hourly"]["time"]) == 1
    assert len(json_data["daily"]["time"]) == 1

@patch("backend.api.routes.WeatherPipeline")
def test_get_current_weather(mock_pipeline_class, override_db):
    mock_pipeline = MagicMock()
    mock_pipeline.get_or_create_forecast.return_value = MOCK_WEATHER_RESPONSE
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.get("/weather/current?latitude=19.0&longitude=73.0")
    
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["temperature_2m"] == 28.5
    assert json_data["weather_code"] == "RAIN"

@patch("backend.api.routes.WeatherPipeline")
def test_get_hourly_weather(mock_pipeline_class, override_db):
    mock_pipeline = MagicMock()
    mock_pipeline.get_or_create_forecast.return_value = MOCK_WEATHER_RESPONSE
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.get("/weather/hourly?latitude=19.0&longitude=73.0")
    
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["temperature_2m"][0] == 28.5

@patch("backend.api.routes.WeatherPipeline")
def test_get_daily_weather(mock_pipeline_class, override_db):
    mock_pipeline = MagicMock()
    mock_pipeline.get_or_create_forecast.return_value = MOCK_WEATHER_RESPONSE
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.get("/weather/daily?latitude=19.0&longitude=73.0")
    
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["temperature_2m_max"][0] == 30.0

@patch("backend.api.routes.WeatherPipeline")
def test_route_fallback_error_handling(mock_pipeline_class, override_db):
    mock_pipeline = MagicMock()
    mock_pipeline.get_or_create_forecast.side_effect = RuntimeError(
        "Weather forecast is not available. On-the-fly downloads are disabled and no fallback data exists in the database for (19.0, 73.0)."
    )
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.get("/weather/full?latitude=19.0&longitude=73.0")
    
    assert response.status_code == 404
    assert "On-the-fly downloads are disabled" in response.json()["detail"]

@patch("backend.api.routes.WeatherPipeline")
def test_route_temporary_unavailable_error_handling(mock_pipeline_class, override_db):
    mock_pipeline = MagicMock()
    mock_pipeline.get_or_create_forecast.side_effect = RuntimeError(
        "Could not download GRIB file for cycle 2026-06-02 12:00:00+00:00: Connection refused"
    )
    mock_pipeline_class.return_value = mock_pipeline
    
    response = client.get("/weather/full?latitude=19.0&longitude=73.0")
    
    assert response.status_code == 503
    assert "Weather service temporarily unavailable" in response.json()["detail"]

@patch("backend.api.routes.WeatherScheduler")
def test_trigger_scheduler_run_authorized(mock_scheduler_class, override_db, monkeypatch):
    monkeypatch.setenv("SCHEDULER_SECRET", "testsecret")
    
    mock_scheduler = MagicMock()
    mock_scheduler_class.return_value = mock_scheduler
    
    response = client.post("/weather/scheduler/run", headers={"X-Scheduler-Secret": "testsecret"})
    
    assert response.status_code == 200
    assert response.json()["status"] == "RUNNING"
    mock_scheduler.run_once.assert_called_once()

@patch("backend.api.routes.WeatherScheduler")
def test_trigger_scheduler_run_unauthorized(mock_scheduler_class, override_db, monkeypatch):
    monkeypatch.setenv("SCHEDULER_SECRET", "testsecret")
    
    response = client.post("/weather/scheduler/run", headers={"X-Scheduler-Secret": "wrongsecret"})
    
    assert response.status_code == 403
    assert "Forbidden" in response.json()["detail"]
