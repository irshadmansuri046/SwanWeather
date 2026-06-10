import os
import pytest
from datetime import datetime, date, timezone
from unittest.mock import MagicMock, patch
import pandas as pd

from backend.workers.providers.factory import WeatherProviderFactory
from backend.workers.providers.base import BaseWeatherProvider
from backend.workers.providers.ecmwf import ECMWFProvider
from backend.workers.providers.imd import IMDProvider
from backend.database.models import WeatherForecast, WeatherDaily, WeatherJob
from backend.workers.pipeline import WeatherPipeline

def test_provider_factory():
    # Test default
    p_default = WeatherProviderFactory.get_provider(None)
    assert isinstance(p_default, ECMWFProvider)
    
    # Test ecmwf explicit
    p_ecmwf = WeatherProviderFactory.get_provider("ecmwf")
    assert isinstance(p_ecmwf, ECMWFProvider)
    
    # Test imd explicit
    p_imd = WeatherProviderFactory.get_provider("imd")
    assert isinstance(p_imd, IMDProvider)
    
    # Test case insensitivity and whitespace stripping
    p_imd_caps = WeatherProviderFactory.get_provider("  IMD  ")
    assert isinstance(p_imd_caps, IMDProvider)

@patch("backend.workers.providers.ecmwf.ECMWFDownloader")
def test_ecmwf_provider_methods(mock_downloader_class):
    mock_downloader = MagicMock()
    mock_downloader_class.return_value = mock_downloader
    
    provider = ECMWFProvider(downloader=mock_downloader)
    
    # 1. Test get_latest_available_cycle
    mock_downloader.get_latest_available_cycle.return_value = (date(2026, 6, 9), 12)
    dt = provider.get_latest_available_cycle(datetime.now(timezone.utc))
    assert dt == datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    
    # 2. Test get_target_path
    mock_downloader.get_target_path.return_value = "/data/raw_grib/ecmwf_20260609_12utc.nc"
    path = provider.get_target_path(datetime(2026, 6, 9, 12, 0))
    assert path == "/data/raw_grib/ecmwf_20260609_12utc.nc"
    mock_downloader.get_target_path.assert_called_with(date(2026, 6, 9), 12)
    
    # 3. Test download_cycle
    mock_downloader.download_cycle.return_value = ("/data/raw_grib/ecmwf_20260609_12utc.nc", "hash123", 12345)
    res = provider.download_cycle(datetime(2026, 6, 9, 12, 0))
    assert res == ("/data/raw_grib/ecmwf_20260609_12utc.nc", "hash123", 12345)
    mock_downloader.download_cycle.assert_called_with(date(2026, 6, 9), 12)

@patch("backend.workers.providers.imd.ECMWFDownloader")
@patch("backend.workers.providers.imd.shutil")
@patch("backend.workers.providers.imd.os.path.exists")
@patch("backend.workers.providers.imd.os.path.getsize")
def test_imd_provider_download(mock_getsize, mock_exists, mock_shutil, mock_downloader_class):
    mock_downloader = MagicMock()
    mock_downloader_class.return_value = mock_downloader
    
    # Mock downloader storage dir
    mock_downloader.storage_dir = "/data/raw_grib"
    mock_downloader.use_local_storage = True
    
    provider = IMDProvider(downloader=mock_downloader)
    cycle_dt = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    
    # Mock check that file does NOT exist yet (forces download/copy)
    mock_exists.return_value = False
    mock_downloader.download_cycle.return_value = ("/data/raw_grib/ecmwf_20260609_12utc.nc", "hash123", 12345)
    mock_getsize.return_value = 12345
    mock_downloader.compute_sha256.return_value = "imd_hash_123"
    
    # Test get_target_path
    target_path = provider.get_target_path(cycle_dt)
    assert target_path == "/data/raw_grib/imd_20260609_12utc.nc"
    
    # Test download_cycle (will copy ecmwf and return info)
    res_path, checksum, size = provider.download_cycle(cycle_dt)
    
    assert res_path == "/data/raw_grib/imd_20260609_12utc.nc"
    assert checksum == "imd_hash_123"
    assert size == 12345
    
    # Verify copy was invoked
    mock_shutil.copy2.assert_called_with("/data/raw_grib/ecmwf_20260609_12utc.nc", "/data/raw_grib/imd_20260609_12utc.nc")

@patch("backend.workers.providers.imd.GRIBParser")
def test_imd_provider_extract_and_interpolate(mock_parser_class):
    mock_parser = MagicMock()
    mock_parser_class.return_value = mock_parser
    
    # Create test hourly dataframe
    test_df = pd.DataFrame([
        {
            "forecast_time": datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
            "temperature": 25.0,
            "dew_point": 15.0,
            "wind_speed": 10.0,
            "visibility": 10000.0,
            "precipitation": 0.0
        }
    ])
    
    mock_parser.extract_point_data.return_value = pd.DataFrame()
    mock_parser.interpolate_to_hourly.return_value = test_df.copy()
    
    provider = IMDProvider()
    res_df = provider.extract_and_interpolate("/data/raw_grib/imd_20260609_12utc.nc", 19.0, 73.0)
    
    # Verify the simulated IMD offsets were applied:
    # temp should be 25.0 + 0.3 = 25.3
    # wind_speed should be 10.0 * 0.95 = 9.5
    assert res_df.iloc[0]["temperature"] == 25.3
    assert res_df.iloc[0]["wind_speed"] == 9.5
    mock_parser.close.assert_called_once()
