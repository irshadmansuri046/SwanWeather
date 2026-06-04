from pydantic import BaseModel, Field
from datetime import date
from typing import List

class WeatherCurrentDTO(BaseModel):
    time: str = Field(..., description="Datetime of the forecast point in IST")
    temperature_2m: float = Field(..., description="Current temperature in Celsius")
    apparent_temperature: float = Field(..., description="Feels like temperature in Celsius")
    relative_humidity_2m: float = Field(..., description="Relative humidity in percentage")
    surface_pressure: float = Field(..., description="Atmospheric pressure in hPa")
    cloud_cover: float = Field(..., description="Cloud cover in percentage")
    wind_speed_10m: float = Field(..., description="Wind speed in m/s")
    wind_direction_10m: float = Field(..., description="Wind direction in degrees")
    dew_point_2m: float = Field(..., description="Dew point temperature in Celsius")
    uv_index: float = Field(..., description="Current estimated UV index (0-15)")
    visibility: float = Field(..., description="Visibility in meters")
    precipitation: float = Field(..., description="Precipitation in mm for this hour")
    weather_code: str = Field(..., description="Weather condition code (CLEAR, RAIN, etc.)")

class WeatherHourlyResponse(BaseModel):
    time: List[str] = Field(..., description="Array of ISO 8601 / IST timestamps")
    temperature_2m: List[float] = Field(..., description="Array of temperatures at 2m")
    apparent_temperature: List[float] = Field(..., description="Array of apparent temperatures")
    precipitation: List[float] = Field(..., description="Array of precipitation in mm")
    relative_humidity_2m: List[float] = Field(..., description="Array of relative humidity percentages")
    surface_pressure: List[float] = Field(..., description="Array of surface pressures in hPa")
    cloud_cover: List[float] = Field(..., description="Array of cloud covers in percentage")
    wind_speed_10m: List[float] = Field(..., description="Array of wind speeds at 10m in m/s")
    wind_direction_10m: List[float] = Field(..., description="Array of wind directions at 10m in degrees")
    weather_code: List[str] = Field(..., description="Array of weather condition codes")

class WeatherDailyResponse(BaseModel):
    time: List[date] = Field(..., description="Array of dates")
    temperature_2m_max: List[float] = Field(..., description="Array of daily max temperatures")
    temperature_2m_min: List[float] = Field(..., description="Array of daily min temperatures")
    precipitation_sum: List[float] = Field(..., description="Array of total daily precipitation sums")
    humidity_avg: List[float] = Field(..., description="Array of average daily humidity percentages")
    wind_speed_10m_max: List[float] = Field(..., description="Array of maximum daily wind speeds")
    weather_code: List[str] = Field(..., description="Array of daily dominant weather codes")

class WeatherFullResponse(BaseModel):
    latitude: float
    longitude: float
    cycle_time: str = Field(..., description="The ECMWF cycle run time used for this forecast in IST")
    current: WeatherCurrentDTO
    hourly: WeatherHourlyResponse
    daily: WeatherDailyResponse

class CustomDateValue(BaseModel):
    date: str
    value: float

class CustomCoordinateData(BaseModel):
    lat: float
    lon: float
    dates: List[CustomDateValue]

class CustomParameterData(BaseModel):
    parameter: str
    raw_key: str
    height: str
    unit: str
    label: str
    coordinates: List[CustomCoordinateData]

class WeatherCustomData(BaseModel):
    daily: List[CustomParameterData]
    weekly: List[CustomParameterData]
    common: List[CustomParameterData]

class WeatherCustomResponse(BaseModel):
    status: int
    message: str
    data: WeatherCustomData
