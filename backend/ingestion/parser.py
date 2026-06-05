import os
import logging
import numpy as np
import pandas as pd
import xarray as xr
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class GRIBParser:
    """
    Parses ECMWF GRIB2 files using xarray and cfgrib.
    Extracts weather variables, converts units, and extracts spatial coordinates.
    """
    
    # Mapping of ECMWF variable names to friendly names
    VAR_MAPPING = {
        "t2m": "temperature",
        "d2m": "dew_point",
        "u10": "wind_u",
        "v10": "wind_v",
        "tp": "precipitation_accum",
        "msl": "pressure",
        "tcc": "cloud_cover",
        "vis": "visibility"
    }

    def __init__(self, file_path: str, crop_bbox: Optional[Dict[str, Any]] = None):
        self.file_path = file_path
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"GRIB file not found: {file_path}")
        
        self.ds = None
        self._datasets = []
        
        # Native NetCDF file check
        if file_path.endswith(".nc"):
            self.ds = xr.open_dataset(file_path)
            return
            
        def load_and_crop(filter_keys):
            ds = xr.open_dataset(file_path, engine="cfgrib", backend_kwargs={"filter_by_keys": filter_keys})
            if crop_bbox:
                # Determine latitude direction (GRIB can be descending or ascending)
                is_lat_descending = float(ds.latitude[0]) > float(ds.latitude[-1])
                lats = crop_bbox["latitude"]
                lons = crop_bbox["longitude"]
                lat_slice = slice(lats[1], lats[0]) if is_lat_descending else slice(lats[0], lats[1])
                lon_slice = slice(lons[0], lons[1])
                ds = ds.sel(latitude=lat_slice, longitude=lon_slice)
            return ds

        # Open dataset levels with cfgrib engine
        
        # 1. Temperature & Dew Point (heightAboveGround, level 2)
        try:
            ds_2m = load_and_crop({"typeOfLevel": "heightAboveGround", "level": 2})
            self._datasets.append(ds_2m)
        except Exception as e:
            logger.warning(f"Could not open 2m heightAboveGround variables (t2m, d2m): {e}")

        # 2. Wind U & V (heightAboveGround, level 10)
        try:
            ds_10m = load_and_crop({"typeOfLevel": "heightAboveGround", "level": 10})
            self._datasets.append(ds_10m)
        except Exception as e:
            logger.warning(f"Could not open 10m heightAboveGround variables (u10, v10): {e}")

        # 3. Total Precipitation (surface)
        try:
            ds_sfc = load_and_crop({"typeOfLevel": "surface"})
            self._datasets.append(ds_sfc)
        except Exception as e:
            logger.warning(f"Could not open surface variables (tp): {e}")

        # 4. Mean Sea Level Pressure (meanSea)
        try:
            ds_msl = load_and_crop({"typeOfLevel": "meanSea"})
            self._datasets.append(ds_msl)
        except Exception as e:
            logger.warning(f"Could not open meanSea variables (msl): {e}")

        # 5. Total Cloud Cover (entireAtmosphere)
        try:
            ds_atm = load_and_crop({"typeOfLevel": "entireAtmosphere"})
            self._datasets.append(ds_atm)
        except Exception as e:
            logger.warning(f"Could not open entireAtmosphere variables (tcc): {e}")

        if not self._datasets:
            raise RuntimeError(f"GRIB file {file_path} did not contain any valid weather variables.")

        try:
            # Drop coordinates that can cause merge issues (e.g. heightAboveGround)
            cleaned_datasets = []
            for ds in self._datasets:
                to_drop = [c for c in ds.coords if c not in ["latitude", "longitude", "step", "valid_time", "time"]]
                cleaned_datasets.append(ds.drop_vars(to_drop))

            self.ds = xr.merge(cleaned_datasets)
        except Exception as e:
            logger.error(f"Error merging datasets for GRIB file {file_path}: {e}")
            raise e

    def close(self):
        """Closes the datasets and releases resources."""
        if hasattr(self, "_datasets") and self._datasets:
            for ds in self._datasets:
                try:
                    ds.close()
                except Exception:
                    pass
        if hasattr(self, 'ds') and self.ds is not None:
            try:
                self.ds.close()
            except Exception:
                pass

    def get_coordinates(self) -> tuple[np.ndarray, np.ndarray]:
        """Returns the grid latitudes and longitudes."""
        return self.ds.latitude.values, self.ds.longitude.values

    def extract_point_data(self, lat: float, lon: float) -> pd.DataFrame:
        """
        Extracts raw time-series data using bilinear interpolation for the given lat/lon.
        Returns a Pandas DataFrame indexed by valid_time.
        """
        # xarray bilinear interpolation
        # Coordinates in the ds longitude range are already mapped to -180 to 180 by cfgrib.
        # We can directly interpolate using the query lat and lon coordinates.
        point_ds = self.ds.interp(latitude=lat, longitude=lon, method="linear")
        
        # Convert selected point to a pandas DataFrame
        df = point_ds.to_dataframe()
        
        # Rename columns to friendly names
        df = df.rename(columns=self.VAR_MAPPING)
        
        # Fill standard defaults for columns that are missing from GRIB (e.g. visibility)
        # Using raw GRIB units: temperature/dew_point in Kelvin, pressure in Pa, etc.
        defaults = {
            "temperature": 288.15,        # 15 deg C
            "dew_point": 286.15,          # 13 deg C
            "wind_u": 0.0,
            "wind_v": 0.0,
            "precipitation_accum": 0.0,
            "pressure": 101325.0,         # 1013.25 hPa
            "cloud_cover": 0.0,
            "visibility": 24135.0         # 15 miles / 24km (clear visibility)
        }
        for col, val in defaults.items():
            if col not in df.columns:
                df[col] = val
        
        # Reset index to make coordinates normal columns
        df = df.reset_index()
        
        # Filter only columns we want
        keep_cols = [
            "time", "step", "valid_time", "latitude", "longitude",
            "temperature", "dew_point", "wind_u", "wind_v",
            "precipitation_accum", "pressure", "cloud_cover", "visibility"
        ]
        
        # Only keep columns that actually exist in the DataFrame
        existing_cols = [c for c in keep_cols if c in df.columns]
        df = df[existing_cols]
        
        # Normalize longitude back to -180 to 180
        if "longitude" in df.columns:
            df["longitude"] = df["longitude"].apply(lambda x: x - 360 if x > 180 else x)
            
        # Set valid_time as index
        df = df.set_index("valid_time")
        df.index = pd.to_datetime(df.index)
        
        # Perform conversions
        self._normalize_units(df)
        
        return df

    def _normalize_units(self, df: pd.DataFrame):
        """
        In-place normalization of meteorological units:
        - Temperature: Kelvin to Celsius
        - Dew Point: Kelvin to Celsius
        - Pressure: Pascals to hPa (hectopascals/millibars)
        - Precipitation: cumulative meters to mm
        - Cloud Cover: 0-1 fraction to 0-100 percentage
        - Wind: U/V vectors to speed (m/s) and direction (degrees)
        """
        # Temperature (Kelvin to Celsius)
        if "temperature" in df.columns:
            df["temperature"] = df["temperature"] - 273.15
            
        if "dew_point" in df.columns:
            df["dew_point"] = df["dew_point"] - 273.15
            
        # Pressure (Pa to hPa)
        if "pressure" in df.columns:
            df["pressure"] = df["pressure"] / 100.0
            
        # Cloud cover (fraction to percentage)
        if "cloud_cover" in df.columns:
            df["cloud_cover"] = (df["cloud_cover"] * 100.0).clip(0.0, 100.0)
            
        # Wind components to Wind Speed & Wind Direction
        if "wind_u" in df.columns and "wind_v" in df.columns:
            u = df["wind_u"]
            v = df["wind_v"]
            
            # Wind speed: sqrt(u^2 + v^2)
            df["wind_speed"] = np.sqrt(u**2 + v**2)
            
            # Wind direction in degrees: from which direction the wind is blowing.
            # Meteorological convention: 0 is North, 90 is East, etc.
            # math.atan2(u, v) gives angle in radians.
            # direction = (180 + arctan2(u, v) * 180/pi) % 360
            df["wind_direction"] = (np.degrees(np.arctan2(u, v)) + 180) % 360
            
            # Drop raw vectors to keep payload small
            df.drop(columns=["wind_u", "wind_v"], inplace=True)
            
        # Handle Precipitation: GRIB total precipitation is accumulated from step 0.
        # We need to compute interval precipitation.
        if "precipitation_accum" in df.columns:
            # Convert meters to mm
            df["precipitation_accum"] = df["precipitation_accum"] * 1000.0
            
            # Sort by index to ensure chronological order
            df.sort_index(inplace=True)
            
            # Diff to get precipitation per step interval
            # The first step (step 0) has 0 accumulated precipitation.
            df["precipitation"] = df["precipitation_accum"].diff().fillna(df["precipitation_accum"])
            
            # Ensure precipitation is non-negative (can sometimes have floating point artifacts)
            df["precipitation"] = df["precipitation"].clip(lower=0.0)
            
            # Keep the raw accumulated column or drop it? Let's drop it to save space
            df.drop(columns=["precipitation_accum"], inplace=True)

    @classmethod
    def interpolate_to_hourly(cls, df: pd.DataFrame) -> pd.DataFrame:
        """
        Interpolates 3-hourly/6-hourly weather data to smooth 1-hourly weather data.
        Distributes accumulated precipitation across the hourly steps.
        """
        if df.empty:
            return df
            
        # Sort index
        df = df.sort_index()
        
        # Get start and end times
        start_time = df.index.min()
        end_time = df.index.max()
        
        # Generate target hourly index
        hourly_index = pd.date_range(start=start_time, end=end_time, freq="h")
        
        # Save constant columns to re-populate
        lat = df["latitude"].iloc[0] if "latitude" in df.columns else None
        lon = df["longitude"].iloc[0] if "longitude" in df.columns else None
        time_cycle = df["time"].iloc[0] if "time" in df.columns else None
        
        # Reindex to hourly
        df_hourly = df.reindex(hourly_index)
        
        # For precipitation, we distribute the interval precipitation evenly.
        # Let's find the step intervals and distribute.
        # If we have 3mm precipitation at valid_time 03:00, and previous was 00:00,
        # we distribute 1mm to 01:00, 1mm to 02:00, 1mm to 03:00.
        
        # To do this cleanly:
        # We can find the step size. For each raw step, we count how many hours are in it.
        # We can use forward fill to find which interval each hour belongs to,
        # and then divide the precipitation by the size of the interval.
        
        # First, copy precipitation to align with interpolation
        precip_series = df["precipitation"].copy()
        
        # Interpolate numeric columns linearly (excluding precipitation, lat, lon, time)
        skip_cols = ["precipitation", "latitude", "longitude", "time", "step"]
        cols_to_interpolate = [c for c in df.columns if c not in skip_cols]
        
        for col in cols_to_interpolate:
            df_hourly[col] = df_hourly[col].interpolate(method="linear")
            
        # For precipitation:
        # We find the original indices where we had readings
        original_times = df.index
        
        # For each hour, find the next original timestamp
        # e.g., if hour is 01:00, the next original timestamp is 03:00.
        # We can use bfill (backward fill) to map each hour to its target interval.
        mapping_df = pd.DataFrame(index=hourly_index)
        mapping_df["target_time"] = pd.Series(index=original_times, data=original_times).reindex(hourly_index, method="bfill")
        
        times_series = pd.Series(original_times, index=original_times)
        first_diff = original_times[1] - original_times[0] if len(original_times) > 1 else pd.Timedelta(hours=3)
        interval_sizes = times_series.diff().fillna(first_diff).apply(lambda x: max(1.0, x.total_seconds() / 3600.0))
        
        # Map precipitation values and interval sizes to the hourly dataframe
        df_hourly["target_time"] = mapping_df["target_time"]
        
        # Merge precipitation values from the original index
        df_hourly["raw_precip"] = df_hourly["target_time"].map(precip_series)
        df_hourly["interval_hours"] = df_hourly["target_time"].map(interval_sizes)
        
        # Calculate distributed hourly precipitation
        # If raw_precip is NaN, precipitation is 0
        df_hourly["precipitation"] = (df_hourly["raw_precip"] / df_hourly["interval_hours"]).fillna(0.0)
        
        # Restore coordinates and original run cycle time
        if lat is not None:
            df_hourly["latitude"] = lat
        if lon is not None:
            df_hourly["longitude"] = lon
        if time_cycle is not None:
            df_hourly["time"] = time_cycle
            
        # Clean up temporary columns
        df_hourly.drop(columns=["target_time", "raw_precip", "interval_hours", "step"], errors="ignore", inplace=True)
        
        # Set step as hourly index offset
        df_hourly = df_hourly.reset_index().rename(columns={"index": "forecast_time"})
        df_hourly["step"] = (df_hourly["forecast_time"] - df_hourly["time"]).dt.total_seconds() / 3600.0
        
        # Filter out NaN values (e.g. if the interpolation left a few border values as NaN)
        df_hourly.dropna(subset=["temperature"], inplace=True)
        
        return df_hourly

    @classmethod
    def crop_and_save_netcdf(
        cls,
        grib_path: str,
        netcdf_path: str,
        lat_range: tuple[float, float] = (8.0, 38.0),
        lon_range: tuple[float, float] = (68.0, 98.0)
    ) -> None:
        """
        Parses a global GRIB file, crops it to the specified bounding box (e.g. India)
        sequentially layer-by-layer to minimize memory usage, and saves it as a NetCDF file.
        """
        logger.info(f"Cropping GRIB {grib_path} sequentially to lat={lat_range}, lon={lon_range}...")
        
        filter_keys_list = [
            {"typeOfLevel": "heightAboveGround", "level": 2},
            {"typeOfLevel": "heightAboveGround", "level": 10},
            {"typeOfLevel": "surface"},
            {"typeOfLevel": "meanSea"},
            {"typeOfLevel": "entireAtmosphere"}
        ]
        
        temp_ncs = []
        try:
            for idx, filter_keys in enumerate(filter_keys_list):
                try:
                    logger.info(f"Processing GRIB layer {idx+1}/{len(filter_keys_list)}: {filter_keys}")
                    ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs={"filter_by_keys": filter_keys})
                    
                    # Crop immediately
                    is_lat_descending = float(ds.latitude[0]) > float(ds.latitude[-1])
                    lat_slice = slice(lat_range[1], lat_range[0]) if is_lat_descending else slice(lat_range[0], lat_range[1])
                    lon_slice = slice(lon_range[0], lon_range[1])
                    cropped_layer = ds.sel(latitude=lat_slice, longitude=lon_slice)
                    
                    # Clean coordinates to prevent merge issues later
                    to_drop = [c for c in cropped_layer.coords if c not in ["latitude", "longitude", "step", "valid_time", "time"]]
                    cleaned_layer = cropped_layer.drop_vars(to_drop)
                    
                    # Save layer to a temporary NetCDF file
                    layer_nc_path = f"{netcdf_path}.layer_{idx}.nc"
                    cleaned_layer.to_netcdf(layer_nc_path, engine="scipy")
                    
                    # Close resources immediately
                    ds.close()
                    cleaned_layer.close()
                    cropped_layer.close()
                    
                    temp_ncs.append(layer_nc_path)
                except Exception as layer_err:
                    logger.warning(f"Could not process GRIB layer {filter_keys}: {layer_err}")
            
            if not temp_ncs:
                raise RuntimeError("No GRIB layers could be processed.")
                
            # Merge the cropped NetCDF layers (which are tiny and in memory)
            logger.info("Merging cropped layers...")
            opened_ds_list = [xr.open_dataset(p) for p in temp_ncs]
            merged_ds = xr.merge(opened_ds_list)
            
            # Save the final merged NetCDF file
            merged_ds.to_netcdf(netcdf_path, engine="scipy")
            
            # Close all opened NetCDF datasets
            merged_ds.close()
            for ds_nc in opened_ds_list:
                ds_nc.close()
                
            logger.info(f"Successfully created cropped NetCDF file: {netcdf_path}")
            
        finally:
            # Clean up the temporary NetCDF files
            for p in temp_ncs:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
