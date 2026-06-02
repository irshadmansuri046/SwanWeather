import pytest
import os
from datetime import datetime
from backend.ingestion.downloader import ECMWFDownloader

def test_get_latest_cycle_info():
    # Test morning check (e.g. 10:00 UTC) -> 06 UTC run should be ready
    dt_morning = datetime(2026, 6, 2, 10, 30)
    cycle_date, cycle_hour = ECMWFDownloader.get_latest_cycle_info(dt_morning)
    assert cycle_date == dt_morning.date()
    assert cycle_hour == 6
    
    # Test early morning check (e.g. 02:00 UTC) -> should lookback to 18 UTC of previous day
    dt_early = datetime(2026, 6, 2, 2, 15)
    cycle_date, cycle_hour = ECMWFDownloader.get_latest_cycle_info(dt_early)
    assert cycle_date == datetime(2026, 6, 1).date()
    assert cycle_hour == 18

def test_get_target_path():
    downloader = ECMWFDownloader(storage_dir="/tmp/test_grib")
    dt = datetime(2026, 6, 2, 12, 0)
    path = downloader.get_target_path(dt.date(), dt.hour)
    assert os.path.basename(path) == "ecmwf_20260602_12utc.grib2"

def test_compute_sha256(tmp_path):
    downloader = ECMWFDownloader()
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello ECMWF")
    
    checksum = downloader.compute_sha256(str(test_file))
    # Expected SHA256 of "Hello ECMWF"
    import hashlib
    expected = hashlib.sha256(b"Hello ECMWF").hexdigest()
    assert checksum == expected
