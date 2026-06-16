from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING
from backend.workers.providers.base import BaseWeatherProvider
from backend.ingestion.downloader import ECMWFDownloader

if TYPE_CHECKING:
    import pandas as pd

class ECMWFProvider(BaseWeatherProvider):
    """
    ECMWF Data Provider implementation using ECMWFDownloader and GRIBParser.
    """

    def __init__(self, downloader: ECMWFDownloader = None):
        self.downloader = downloader or ECMWFDownloader()
        self.name = "ecmwf"

    def get_latest_available_cycle(self, current_time: datetime) -> datetime:
        cycle_date, cycle_hour = self.downloader.get_latest_available_cycle(current_time)
        cycle_dt = datetime.combine(cycle_date, datetime.min.time()) + timedelta(hours=cycle_hour)
        return cycle_dt.replace(tzinfo=timezone.utc)

    def get_target_path(self, cycle_dt: datetime) -> str:
        return self.downloader.get_target_path(cycle_dt.date(), cycle_dt.hour)

    def download_cycle(self, cycle_dt: datetime) -> tuple[str, str, int]:
        return self.downloader.download_cycle(cycle_dt.date(), cycle_dt.hour)

    def extract_and_interpolate(self, file_path: str, lat: float, lon: float) -> "pd.DataFrame":
        from backend.ingestion.parser import GRIBParser
        parser = GRIBParser(file_path)
        try:
            raw_df = parser.extract_point_data(lat, lon)
            hourly_df = parser.interpolate_to_hourly(raw_df)
            return hourly_df
        finally:
            parser.close()
