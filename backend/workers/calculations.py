import math
from datetime import datetime, date, timezone, timedelta

def calculate_absolute_humidity(temp_c: float, rh: float) -> float:
    """
    Computes absolute humidity in g/m^3 using standard Magnus-Tetens formula.
    AH = 216.7 * (e / (T_c + 273.15))
    where e = e_s * (RH/100) and e_s = 6.112 * exp((17.67 * T_c)/(T_c + 243.5))
    """
    # Clamp RH between 0 and 100
    rh = max(0.0, min(100.0, rh))
    
    # Saturated vapor pressure (hPa)
    e_s = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
    
    # Actual vapor pressure (hPa)
    e = e_s * (rh / 100.0)
    
    # Absolute humidity (g/m^3)
    ah = 216.7 * (e / (temp_c + 273.15))
    return round(ah, 1)

def calculate_soil_frost(temp_c: float) -> float:
    """
    Estimates soil frost percentage from temperature:
    - 100% if temp <= 0
    - Interpolates linearly from 100% to 0% between 0C and 5C
    - 0% if temp >= 5
    """
    if temp_c <= 0.0:
        return 100.0
    elif temp_c >= 5.0:
        return 0.0
    else:
        return round((5.0 - temp_c) / 5.0 * 100.0, 1)

def calculate_sunrise_sunset(lat: float, lon: float, d: date) -> tuple[int, int]:
    """
    Calculates sunrise and sunset Unix timestamps (in UTC) for a given date, latitude, and longitude.
    Implements standard NOAA Solar Geometry equations.
    """
    # Day of year
    N = d.timetuple().tm_yday
    
    # Convert longitude to hour value and estimate sunrise/sunset
    lngHour = lon / 15.0
    
    # Estimate sunrise/sunset time in days relative to UTC
    t_rise = N + ((6.0 - lngHour) / 24.0)
    t_set = N + ((18.0 - lngHour) / 24.0)
    
    def calc_time(t, is_sunrise):
        # Calculate solar mean anomaly
        M = (0.9856 * t) - 3.289
        
        # Calculate solar true longitude
        L = M + (1.916 * math.sin(math.radians(M))) + (0.020 * math.sin(math.radians(2 * M))) + 282.634
        L = L % 360
        
        # Calculate right ascension
        RA = math.degrees(math.atan(0.9174 * math.tan(math.radians(L))))
        RA = RA % 360
        
        # Adjust RA to be in same quadrant as L
        Lquad = (math.floor(L / 90)) * 90
        RAquad = (math.floor(RA / 90)) * 90
        RA = RA + (Lquad - RAquad)
        
        # Convert RA to hours
        RA = RA / 15.0
        
        # Calculate solar declination
        sinDec = 0.39782 * math.sin(math.radians(L))
        cosDec = math.cos(math.asin(sinDec))
        
        # Calculate local hour angle
        # Zenith = 90.8333 degrees for standard sunrise/sunset
        zenith = 90.8333
        cosH = (math.cos(math.radians(zenith)) - (sinDec * math.sin(math.radians(lat)))) / (cosDec * math.cos(math.radians(lat)))
        
        if cosH > 1.0:
            return None  # Always night (polar night)
        elif cosH < -1.0:
            return None  # Always day (polar day)
            
        # Calculate hour angle H
        if is_sunrise:
            H = 360.0 - math.degrees(math.acos(cosH))
        else:
            H = math.degrees(math.acos(cosH))
        H = H / 15.0
        
        # Calculate local mean time of rising/setting
        T = H + RA - (0.06571 * t) - 6.622
        
        # Convert to UT
        UT = T - lngHour
        UT = UT % 24
        
        # Convert to timezone-aware UTC datetime
        hours = math.floor(UT)
        minutes_decimal = (UT - hours) * 60
        minutes = math.floor(minutes_decimal)
        seconds = math.floor((minutes_decimal - minutes) * 60)
        
        try:
            dt_utc = datetime(d.year, d.month, d.day, hours, minutes, seconds, tzinfo=timezone.utc)
            return int(dt_utc.timestamp())
        except ValueError:
            dt_utc = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
            offset = -timedelta(hours=6) if is_sunrise else timedelta(hours=6)
            dt_utc += offset
            return int(dt_utc.timestamp())

    rise_ts = calc_time(t_rise, True)
    set_ts = calc_time(t_set, False)
    
    # Defaults if calculation returns polar exception (none)
    if rise_ts is None:
        rise_ts = int(datetime(d.year, d.month, d.day, 6, 0, 0, tzinfo=timezone.utc).timestamp())
    if set_ts is None:
        set_ts = int(datetime(d.year, d.month, d.day, 18, 0, 0, tzinfo=timezone.utc).timestamp())
        
    return rise_ts, set_ts

def calculate_moon_info(d: date) -> tuple[float, float]:
    """
    Calculates moon age in days and phase index (0.0 to 1.0) using a known
    New Moon reference: Jan 6, 2000 18:14 UTC.
    Synodic month = 29.530588853 days.
    """
    ref_dt = datetime(2000, 1, 6, 18, 14, 0, tzinfo=timezone.utc)
    target_dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    diff_days = (target_dt - ref_dt).total_seconds() / 86400.0
    
    synodic_month = 29.530588853
    age = diff_days % synodic_month
    phase_idx = age / synodic_month
    return round(age, 1), round(phase_idx, 2)

def calculate_moonrise_moonset(sunrise_ts: int, sunset_ts: int, phase_idx: float) -> tuple[int, int]:
    """
    Estimates moonrise and moonset Unix timestamps based on solar times and moon phase.
    At phase 0.0 (New Moon), moonrise is around sunrise, moonset is around sunset.
    At phase 0.5 (Full Moon), moonrise is around sunset, moonset is around sunrise.
    """
    # Shift moonrise/moonset timestamps based on phase
    # Full phase cycle shifts rise/set by 24 hours
    offset = int(phase_idx * 24.0 * 3600.0)
    moonrise_ts = sunrise_ts + offset
    moonset_ts = sunset_ts + offset
    return moonrise_ts, moonset_ts
