import pytest
from datetime import date, datetime, timezone
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from backend.api.main import app
from backend.database.connection import get_db
from backend.database.models import Crop, CropStage, CropStageRule, WeatherDaily

client = TestClient(app)

@pytest.fixture(autouse=True)
def override_get_db(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield
    app.dependency_overrides.clear()

@pytest.mark.usefixtures("db_session")
def test_get_crops(db_session):
    # 1. Verify that crops are seeded (since seed script ran, we have crops in the test database)
    # If not seeded, we can insert one crop for testing
    crop = db_session.query(Crop).filter(Crop.name == "Paddy (Rice)").first()
    if not crop:
        crop = Crop(name="Paddy (Rice)", category="Cereal")
        db_session.add(crop)
        db_session.commit()

    response = client.get("/weather/crops")
    assert response.status_code == 200
    crops = response.json()
    assert len(crops) > 0
    assert any(c["name"] == "Paddy (Rice)" for c in crops)

@pytest.mark.usefixtures("db_session")
def test_get_crop_stages(db_session):
    # 1. Fetch or create a crop
    crop = db_session.query(Crop).filter(Crop.name == "Paddy (Rice)").first()
    if not crop:
        crop = Crop(name="Paddy (Rice)", category="Cereal")
        db_session.add(crop)
        db_session.commit()

    # 2. Check stages
    stages = db_session.query(CropStage).filter(CropStage.crop_id == crop.id).all()
    if not stages:
        stage = CropStage(crop_id=crop.id, stage_name="Vegetative Growth", stage_order=2)
        db_session.add(stage)
        db_session.commit()
        stages = [stage]

    response = client.get(f"/weather/crops/{crop.id}/stages")
    assert response.status_code == 200
    stages_res = response.json()
    assert len(stages_res) > 0
    assert any(s["stage_name"] == stages[0].stage_name for s in stages_res)

@pytest.mark.usefixtures("db_session")
def test_get_advisory_engine(db_session):
    # 1. Set up crop, stage, and rule in test DB
    crop = Crop(name="Test Crop For Advisory", category="Cereal")
    db_session.add(crop)
    db_session.commit()

    stage = CropStage(crop_id=crop.id, stage_name="Flowering Test", stage_order=3)
    db_session.add(stage)
    db_session.commit()

    rule = CropStageRule(
        crop_stage_id=stage.id,
        rule_name="Heat Stress Test",
        parameter="temp_max",
        operator=">",
        threshold_value=35.0,
        severity="CRITICAL",
        advisory_text="Heat stress advisory text"
    )
    db_session.add(rule)
    db_session.commit()

    # 2. Set up daily weather forecast record in DB
    lat, lon = 20.0, 75.0
    forecast_dt = date(2026, 6, 2)
    cycle_dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    
    # Check if a daily forecast already exists, otherwise add it
    daily_weather = WeatherDaily(
        latitude=lat,
        longitude=lon,
        forecast_date=forecast_dt,
        cycle_time=cycle_dt,
        temp_min=25.0,
        temp_max=38.0,  # Exceeds the rule's 35.0 threshold!
        precipitation=0.0,
        humidity_avg=60.0,
        wind_speed_max=3.0,
        weather_code="CLEAR"
    )
    db_session.add(daily_weather)
    db_session.commit()

    # 3. Request advisory via API. Mock the WeatherPipeline.get_or_create_forecast to avoid triggering downloader
    with patch("backend.api.routes.WeatherPipeline.get_or_create_forecast", return_value=None):
        payload = {
            "latitude": lat,
            "longitude": lon,
            "crop_stages": [
                {"crop_id": crop.id, "stage_id": stage.id}
            ]
        }
        response = client.post("/weather/advisory", json=payload)
        
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["latitude"] == lat
    assert res_data["longitude"] == lon
    
    advisories = res_data["advisories"]
    assert len(advisories) > 0
    
    # Verify the triggered rule details
    first_advisory = advisories[0]
    assert first_advisory["crop_name"] == "Test Crop For Advisory"
    assert first_advisory["stage_name"] == "Flowering Test"
    assert first_advisory["date"] == forecast_dt.isoformat()
    
    alerts = first_advisory["alerts"]
    assert len(alerts) > 0
    assert alerts[0]["rule_name"] == "Heat Stress Test"
    assert alerts[0]["severity"] == "CRITICAL"
    assert alerts[0]["actual_value"] == 38.0
    assert alerts[0]["advisory_text"] == "Heat stress advisory text"

@pytest.mark.usefixtures("db_session")
def test_post_custom_weather_advisory(db_session):
    # 1. Set up crop, stage, and rule in test DB
    crop = Crop(name="Custom Weather Test Crop", category="Cereal")
    db_session.add(crop)
    db_session.commit()

    stage = CropStage(crop_id=crop.id, stage_name="Flowering Custom", stage_order=3)
    db_session.add(stage)
    db_session.commit()

    rule = CropStageRule(
        crop_stage_id=stage.id,
        rule_name="Heat Stress Custom",
        parameter="temp_max",
        operator=">",
        threshold_value=35.0,
        severity="CRITICAL",
        advisory_text="Heat stress custom advisory"
    )
    db_session.add(rule)
    db_session.commit()

    # 2. Set up daily weather forecast record in DB
    lat, lon = 20.0, 75.0
    forecast_dt = date(2026, 6, 2)
    cycle_dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    
    daily_weather = WeatherDaily(
        latitude=lat,
        longitude=lon,
        forecast_date=forecast_dt,
        cycle_time=cycle_dt,
        temp_min=25.0,
        temp_max=38.0,  # Exceeds the rule's 35.0 threshold!
        precipitation=0.0,
        humidity_avg=60.0,
        wind_speed_max=3.0,
        weather_code="CLEAR"
    )
    db_session.add(daily_weather)
    db_session.commit()

    # 3. Mock WeatherPipeline methods to simulate meteomatics custom response
    mock_custom_data = {
        "status": 200,
        "message": "weather data fetched successfully",
        "data": {
            "daily": [],
            "weekly": [],
            "common": []
        }
    }

    # Test case A: crop_stages is omitted
    with patch("backend.api.routes.WeatherPipeline.get_custom_forecast", return_value=mock_custom_data):
        payload_no_crops = {
            "latitude": lat,
            "longitude": lon
        }
        response = client.post("/weather/custom", json=payload_no_crops)
        
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == 200
    assert res_data["advisories"] is None

    # Test case B: crop_stages is populated
    with patch("backend.api.routes.WeatherPipeline.get_custom_forecast", return_value=mock_custom_data):
        payload_with_crops = {
            "latitude": lat,
            "longitude": lon,
            "crop_stages": [
                {"crop_id": crop.id, "stage_id": stage.id}
            ]
        }
        response = client.post("/weather/custom", json=payload_with_crops)
        
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == 200
    advisories = res_data["advisories"]
    assert len(advisories) > 0
    assert advisories[0]["crop_name"] == "Custom Weather Test Crop"
    assert advisories[0]["alerts"][0]["rule_name"] == "Heat Stress Custom"

