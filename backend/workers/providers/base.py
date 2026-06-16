from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

class BaseWeatherProvider(ABC):
    """
    Abstract Base Class that defines the standard interface for all weather data providers.
    """

    @abstractmethod
    def get_latest_available_cycle(self, current_time: datetime) -> datetime:
        """
        Determines the latest cycle datetime available on the provider's server.
        """
        pass

    @abstractmethod
    def get_target_path(self, cycle_dt: datetime) -> str:
        """
        Generates the storage file path (GRIB or NetCDF) for the cycle.
        """
        pass

    @abstractmethod
    def download_cycle(self, cycle_dt: datetime) -> tuple[str, str, int]:
        """
        Downloads the cycle data file.
        Returns:
            tuple: (target_path, checksum, file_size)
        """
        pass

    @abstractmethod
    def extract_and_interpolate(self, file_path: str, lat: float, lon: float) -> "pd.DataFrame":
        """
        Extracts grid point values and interpolates them to hourly step records.
        Returns:
            pd.DataFrame: A pandas DataFrame containing standardized hourly weather variables.
        """
        pass
