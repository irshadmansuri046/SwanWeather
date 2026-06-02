import pytest
from datetime import datetime
from backend.workers.transform import WeatherComputation

def test_relative_humidity():
    # standard air temp 20C, dew point 10C
    rh = WeatherComputation.compute_relative_humidity(20.0, 10.0)
    assert 50.0 <= rh <= 55.0
    
    # 100% humidity when dew point = temperature
    rh_100 = WeatherComputation.compute_relative_humidity(15.0, 15.0)
    assert rh_100 == 100.0
    
    # dew point higher than temp is capped to 100%
    rh_cap = WeatherComputation.compute_relative_humidity(10.0, 15.0)
    assert rh_cap == 100.0

def test_feels_like_wind_chill():
    # Temp 5C, wind 10 m/s (~36 km/h) -> should trigger wind chill
    temp = 5.0
    wind_speed = 10.0 # m/s
    humidity = 60.0
    
    feels = WeatherComputation.calculate_feels_like(temp, humidity, wind_speed)
    assert feels < temp  # Cold wind should feel colder
    
    # Temp 20C (standard room temperature) -> feels like = air temp
    feels_normal = WeatherComputation.calculate_feels_like(20.0, 60.0, 5.0)
    assert feels_normal == 20.0

def test_feels_like_heat_index():
    # Temp 32C, high humidity (80%) -> should trigger heat index
    feels = WeatherComputation.calculate_feels_like(32.0, 80.0, 1.0)
    assert feels > 32.0  # Humid heat should feel hotter

def test_estimate_uv_index():
    # Test solar zenith calculation
    # London midday (12:00) in summer
    dt_midday = datetime(2026, 6, 21, 12, 0)
    uv_midday = WeatherComputation.estimate_uv_index(51.5, -0.1, dt_midday, cloud_cover=0.0)
    assert uv_midday > 5.0  # Clear sky summer sun should be high
    
    # Summer night -> should be 0.0
    dt_night = datetime(2026, 6, 21, 23, 0)
    uv_night = WeatherComputation.estimate_uv_index(51.5, -0.1, dt_night, cloud_cover=0.0)
    assert uv_night == 0.0
    
    # Summer midday with 100% cloud cover -> should be significantly reduced
    uv_cloudy = WeatherComputation.estimate_uv_index(51.5, -0.1, dt_midday, cloud_cover=100.0)
    assert uv_cloudy < uv_midday * 0.3

def test_weather_code():
    # Rain conditions
    assert WeatherComputation.get_weather_code(temp=15.0, precip=1.5, cloud_cover=90.0, visibility_m=5000.0, wind_speed=2.0) == "RAIN"
    
    # Snow conditions
    assert WeatherComputation.get_weather_code(temp=-2.0, precip=1.2, cloud_cover=100.0, visibility_m=2000.0, wind_speed=5.0) == "SNOW"
    
    # Fog conditions
    assert WeatherComputation.get_weather_code(temp=10.0, precip=0.0, cloud_cover=50.0, visibility_m=500.0, wind_speed=1.0) == "FOG"
    
    # Clear conditions
    assert WeatherComputation.get_weather_code(temp=20.0, precip=0.0, cloud_cover=10.0, visibility_m=10000.0, wind_speed=3.0) == "CLEAR"
