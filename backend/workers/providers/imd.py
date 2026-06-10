import os
import shutil
import logging
from datetime import datetime, timezone, timedelta
import pandas as pd
from backend.workers.providers.base import BaseWeatherProvider
from backend.ingestion.downloader import ECMWFDownloader
from backend.ingestion.parser import GRIBParser

logger = logging.getLogger(__name__)

class IMDProvider(BaseWeatherProvider):
    """
    IMD Data Provider implementation.
    Simulates IMD data by reusing/adapting ECMWF GRIB data, allowing full pipeline integration
    and providing simulated meteorological differences (e.g. +0.3C temp bias).
    """

    def __init__(self, downloader: ECMWFDownloader = None):
        self.downloader = downloader or ECMWFDownloader()

    def get_latest_available_cycle(self, current_time: datetime) -> datetime:
        cycle_date, cycle_hour = self.downloader.get_latest_available_cycle(current_time)
        cycle_dt = datetime.combine(cycle_date, datetime.min.time()) + timedelta(hours=cycle_hour)
        return cycle_dt.replace(tzinfo=timezone.utc)

    def get_target_path(self, cycle_dt: datetime) -> str:
        date_str = cycle_dt.strftime("%Y%m%d")
        filename = f"imd_{date_str}_{cycle_dt.hour:02d}utc.nc"
        return os.path.join(self.downloader.storage_dir, filename)

    def download_cycle(self, cycle_dt: datetime) -> tuple[str, str, int]:
        target_path = self.get_target_path(cycle_dt)
        filename = os.path.basename(target_path)

        # 1. Check if the IMD file already exists locally
        if os.path.exists(target_path):
            file_size = os.path.getsize(target_path)
            if file_size > 1024 * 1024:
                checksum = self.downloader.compute_sha256(target_path)
                return target_path, checksum, file_size
            else:
                os.remove(target_path)

        # 2. Check if the IMD file exists in Supabase Storage
        if not self.downloader.use_local_storage:
            if self.downloader._download_from_supabase(filename, target_path):
                file_size = os.path.getsize(target_path)
                checksum = self.downloader.compute_sha256(target_path)
                return target_path, checksum, file_size

        # 3. Cache Miss: Download/Process ECMWF file first
        ecmwf_path, _, _ = self.downloader.download_cycle(cycle_dt.date(), cycle_dt.hour)

        # Copy the ECMWF NetCDF file to the IMD path
        logger.info(f"Simulating IMD file by copying ECMWF file from {ecmwf_path} to {target_path}")
        shutil.copy2(ecmwf_path, target_path)

        file_size = os.path.getsize(target_path)
        checksum = self.downloader.compute_sha256(target_path)

        # 4. Upload to Supabase Storage if local storage is disabled
        if not self.downloader.use_local_storage:
            self.downloader._upload_to_supabase(target_path, filename)

        return target_path, checksum, file_size

    def extract_and_interpolate(self, file_path: str, lat: float, lon: float) -> pd.DataFrame:
        parser = GRIBParser(file_path)
        try:
            raw_df = parser.extract_point_data(lat, lon)
            hourly_df = parser.interpolate_to_hourly(raw_df)

            # Introduce simulated IMD offsets to distinguish from ECMWF in test/UI views
            if not hourly_df.empty:
                logger.info("Applying simulated IMD weather parameter adjustments (+0.3C temp, -5% wind speed)")
                if "temperature" in hourly_df.columns:
                    hourly_df["temperature"] = hourly_df["temperature"] + 0.3
                if "wind_speed" in hourly_df.columns:
                    hourly_df["wind_speed"] = hourly_df["wind_speed"] * 0.95

            return hourly_df
        finally:
            parser.close()
