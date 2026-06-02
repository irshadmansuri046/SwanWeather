import logging
import numpy as np
import pandas as pd
from datetime import datetime, date
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class WeatherComputation:
    """
    Computes derived weather parameters (Feels Like, UV Index, Weather Codes)
    from basic parsed ECMWF parameters.
    """

    @staticmethod
    def calculate_feels_like(temp: float, humidity: float, wind_speed_ms: float) -> float:
        """
        Calculates Feels Like temperature using standard formulas:
        - Wind Chill for cold temperatures (<= 10C) and wind (> 1.3 m/s)
        - Heat Index for warm temperatures (>= 26.7C) and high humidity
        - Standard air temperature otherwise
        """
        # Wind Chill (T <= 10 C and wind_speed > 4.8 km/h or ~1.33 m/s)
        if temp <= 10.0 and wind_speed_ms > 1.33:
            # wind_speed in km/h
            v_kmh = wind_speed_ms * 3.6
            feels = 13.12 + 0.6215 * temp - 11.37 * (v_kmh ** 0.16) + 0.3965 * temp * (v_kmh ** 0.16)
            return float(feels)
            
        # Heat Index (T >= 26.7 C or 80 F)
        elif temp >= 26.7:
            # Heat index formula requires Fahrenheit
            tf = (temp * 9/5) + 32
            rh = humidity
            
            # Simple formula first
            hi_f = 0.5 * (tf + 61.0 + ((tf - 68.0) * 1.2) + (rh * 0.094))
            
            # If simple formula is >= 80F, use full Rothfusz regression
            if hi_f >= 80.0:
                hi_f = (-42.379 + 2.04901523 * tf + 10.14333127 * rh - 0.22475541 * tf * rh -
                        0.00683783 * tf * tf - 0.05481717 * rh * rh + 0.00122874 * tf * tf * rh +
                        0.00085282 * tf * rh * rh - 0.00000199 * tf * tf * rh * rh)
                
                # Adjustments
                if rh < 13 and (tf >= 80 and tf <= 112):
                    adj = ((13 - rh) / 4) * np.sqrt((17 - np.abs(tf - 95.0)) / 17)
                    hi_f -= adj
                elif rh > 85 and (tf >= 80 and tf <= 87):
                    adj = ((rh - 85) / 10) * ((87 - tf) / 5)
                    hi_f += adj
            
            # Convert back to Celsius
            feels = (hi_f - 32) * 5/9
            return float(feels)
            
        # Default
        return float(temp)

    @staticmethod
    def estimate_uv_index(lat: float, lon: float, dt: datetime, cloud_cover: float) -> float:
        """
        Estimates the UV index based on coordinate location, time of day, day of year,
        and cloud cover. Uses solar zenith angle calculations.
        """
        # Day of year
        day_of_year = dt.timetuple().tm_yday
        
        # Solar declination angle (approximate)
        # delta = 23.45 * sin(360/365 * (284 + day_of_year))
        delta = np.radians(23.45 * np.sin(np.radians(360.0 / 365.0 * (284 + day_of_year))))
        
        # Hour angle (approximate local solar time)
        # Solar time approx = UTC hour + longitude / 15
        local_solar_hour = dt.hour + dt.minute / 60.0 + (lon / 15.0)
        # Hour angle H: 15 degrees per hour relative to solar noon
        hour_angle = np.radians(15.0 * (local_solar_hour - 12.0))
        
        lat_rad = np.radians(lat)
        
        # Cosine of Solar Zenith Angle (cos_sza)
        # cos(SZA) = sin(lat)*sin(delta) + cos(lat)*cos(delta)*cos(hour_angle)
        cos_sza = (np.sin(lat_rad) * np.sin(delta) + 
                   np.cos(lat_rad) * np.cos(delta) * np.cos(hour_angle))
        
        if cos_sza <= 0:
            # Sun is below the horizon
            return 0.0
            
        # SZA in degrees
        sza = np.degrees(np.arccos(cos_sza))
        
        # Clear-sky UV Index estimation:
        # standard formula: UVI_clear = 12.5 * (cos(SZA))^1.25
        uvi_clear = 12.5 * (cos_sza ** 1.25)
        
        # Cloud cover attenuation factor:
        # UV Index is reduced by cloud cover.
        # An empirical formula: UV_cloudy = UV_clear * (1 - 0.75 * (cloud_cover/100)^3)
        cc_fraction = cloud_cover / 100.0
        attenuation = 1.0 - 0.75 * (cc_fraction ** 3)
        uvi = uvi_clear * attenuation
        
        # Cap UV Index between 0 and 15
        return float(np.clip(uvi, 0.0, 15.0))

    @staticmethod
    def get_weather_code(temp: float, precip: float, cloud_cover: float, visibility_m: float, wind_speed: float) -> str:
        """
        Determines the weather condition code based on meteorological parameters.
        Standardized codes: CLEAR, PARTLY_CLOUDY, CLOUDY, RAIN, THUNDER, SNOW, FOG, WINDY.
        """
        # 1. Check precipitation
        if precip > 0.1:  # Non-trivial precipitation
            if temp <= 0.0:
                return "SNOW"
            elif precip > 3.0:  # Heavy precipitation
                return "THUNDER"  # Approximate convection/heavy rain as THUNDER
            else:
                return "RAIN"
                
        # 2. Check visibility for FOG
        if visibility_m < 1000.0:  # Under 1km visibility
            return "FOG"
            
        # 3. Check wind speed
        if wind_speed > 12.0:  # Storm/high wind speed threshold (~43 km/h)
            return "WINDY"
            
        # 4. Check cloud cover
        if cloud_cover > 75.0:
            return "CLOUDY"
        elif cloud_cover > 25.0:
            return "PARTLY_CLOUDY"
            
        return "CLEAR"

    @classmethod
    def compute_relative_humidity(cls, temp: float, dew_point: float) -> float:
        """
        Computes Relative Humidity (%) from Temperature and Dew Point (in Celsius)
        using the August-Roche-Magnus approximation:
        RH = 100 * (exp((17.625 * Td) / (243.04 + Td)) / exp((17.625 * T) / (243.04 + T)))
        """
        # Ensure we don't have math domain errors (dew point cannot be higher than temperature)
        dp = min(dew_point, temp)
        
        term_dp = (17.625 * dp) / (243.04 + dp)
        term_temp = (17.625 * temp) / (243.04 + temp)
        
        rh = 100.0 * (np.exp(term_dp) / np.exp(term_temp))
        return float(np.clip(rh, 0.0, 100.0))
