from pydantic import BaseModel, Field
from datetime import datetime, date
from typing import List, Optional

class WeatherCurrentDTO(BaseModel):
    temperature: float = Field(..., description="Current temperature in Celsius")
    feels_like: float = Field(..., description="Feels like temperature in Celsius")
    humidity: float = Field(..., description="Relative humidity in percentage")
    pressure: float = Field(..., description="Atmospheric pressure in hPa")
    cloud_cover: float = Field(..., description="Cloud cover in percentage")
    wind_speed: float = Field(..., description="Wind speed in m/s")
    wind_direction: float = Field(..., description="Wind direction in degrees")
    dew_point: float = Field(..., description="Dew point temperature in Celsius")
    uv_index: float = Field(..., description="Current estimated UV index (0-15)")
    visibility: float = Field(..., description="Visibility in meters")
    weather_code: str = Field(..., description="Weather condition code (CLEAR, RAIN, etc.)")
    valid_time: datetime = Field(..., description="Datetime of the forecast point")

class WeatherHourlyDTO(BaseModel):
    temperature: float
    feels_like: float
    precipitation: float = Field(..., description="Precipitation in mm for this hour")
    humidity: float
    pressure: float
    cloud_cover: float
    wind_speed: float
    wind_direction: float
    weather_code: str
    forecast_time: datetime

class WeatherDailyDTO(BaseModel):
    forecast_date: date
    temp_min: float = Field(..., description="Minimum temperature in Celsius")
    temp_max: float = Field(..., description="Maximum temperature in Celsius")
    precipitation: float = Field(..., description="Total accumulated precipitation for the day in mm")
    humidity_avg: float = Field(..., description="Average humidity for the day in percentage")
    wind_speed_max: float = Field(..., description="Maximum wind speed for the day in m/s")
    weather_code: str = Field(..., description="Dominant weather condition code for the day")

class WeatherFullResponse(BaseModel):
    latitude: float
    longitude: float
    cycle_time: datetime = Field(..., description="The ECMWF cycle run time used for this forecast")
    current: WeatherCurrentDTO
    hourly: List[WeatherHourlyDTO]
    daily: List[WeatherDailyDTO]
