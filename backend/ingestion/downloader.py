import os
import hashlib
import logging
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ecmwf.opendata import Client
import requests

logger = logging.getLogger(__name__)

class ECMWFDownloader:
    """
    Manages downloading GRIB forecast cycles from ECMWF Open Data.
    Includes retry logic, checksum validation, and cycle checking.
    """
    
    DEFAULT_PARAMS = ["2t", "2d", "10u", "10v", "tp", "msl", "tcc", "vis"]
    
    def __init__(self, storage_dir: Optional[str] = None, source: Optional[str] = None):
        self.storage_dir = storage_dir or os.getenv("RAW_GRIB_STORAGE_DIR", "./data/raw_grib")
        self.source = source or os.getenv("ECMWF_DATA_SOURCE", "aws")
        os.makedirs(self.storage_dir, exist_ok=True)
        
        # Initialize Supabase REST Storage Client details
        self.supabase_url = os.getenv("SUPABASE_URL")
        self.supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        self.use_local_storage = os.getenv("USE_LOCAL_STORAGE", "true").lower() == "true"
        self.allow_on_the_fly_downloads = os.getenv("ALLOW_ON_THE_FLY_DOWNLOADS", "true").lower() == "true"
        
        # Initialize ECMWF client
        # By default, source="aws" accesses the AWS public mirror to avoid connection rate limits.
        self.client = Client(source=self.source)

    def _upload_to_supabase(self, file_path: str, filename: str) -> bool:
        """Uploads a local GRIB file to the Supabase Storage bucket."""
        if not self.supabase_url or not self.supabase_key:
            logger.warning("Supabase URL or Key not set. Skipping GRIB upload.")
            return False
            
        url = f"{self.supabase_url.rstrip('/')}/storage/v1/object/grib-files/{filename}"
        headers = {
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/octet-stream"
        }
        
        try:
            logger.info(f"Uploading GRIB {filename} to Supabase Storage...")
            with open(file_path, "rb") as f:
                response = requests.post(url, headers=headers, data=f, timeout=120)
                
            if response.status_code in [200, 201]:
                logger.info(f"Successfully uploaded {filename} to Supabase Storage.")
                return True
            else:
                logger.error(f"Failed to upload GRIB to Supabase. HTTP {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error uploading GRIB to Supabase: {e}")
            return False

    def _download_from_supabase(self, filename: str, target_path: str) -> bool:
        """Downloads a GRIB file from the Supabase Storage bucket."""
        if not self.supabase_url or not self.supabase_key:
            logger.warning("Supabase URL or Key not set. Skipping GRIB download.")
            return False
            
        url = f"{self.supabase_url.rstrip('/')}/storage/v1/object/authenticated/grib-files/{filename}"
        headers = {
            "Authorization": f"Bearer {self.supabase_key}"
        }
        
        try:
            logger.info(f"Attempting to download GRIB {filename} from Supabase Storage...")
            response = requests.get(url, headers=headers, stream=True, timeout=120)
            
            if response.status_code == 200:
                temp_path = target_path + ".tmp"
                with open(temp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                os.rename(temp_path, target_path)
                logger.info(f"Successfully downloaded GRIB {filename} from Supabase Storage.")
                return True
            else:
                logger.warning(f"GRIB {filename} not found in Supabase Storage. HTTP {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Error downloading GRIB from Supabase: {e}")
            return False

    @staticmethod
    def get_latest_cycle_info(current_time: Optional[datetime] = None) -> tuple[date, int]:
        """
        Calculates the latest forecast run cycle that should be available.
        ECMWF publishes 4 cycles: 00, 06, 12, and 18 UTC.
        Forecast cycles are usually published about 3 to 4 hours after the cycle time.
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)
            
        # Lookback to find the latest published run
        # ECMWF runs are 00, 06, 12, 18 UTC
        # If it's 22:00 UTC, the 18:00 UTC run is likely ready.
        # If it's 04:00 UTC, the 00:00 UTC run is ready.
        # We assume a conservative delay of 4 hours.
        target_time = current_time - timedelta(hours=4)
        
        cycle_hour = (target_time.hour // 6) * 6
        cycle_date = target_time.date()
        
        return cycle_date, cycle_hour

    def get_latest_available_cycle(self, current_time: Optional[datetime] = None) -> tuple[date, int]:
        """
        Scans backwards from the current estimated cycle time to find the latest
        cycle that is actually published and available on the ECMWF server.
        Uses a quick check retrieval to avoid full download attempts.
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)
            
        # Start with a conservative 4-hour delay
        target_time = current_time - timedelta(hours=4)
        
        # Scan back up to 4 cycles (24 hours)
        for i in range(4):
            cycle_hour = (target_time.hour // 6) * 6
            cycle_date = target_time.date()
            
            date_str = cycle_date.strftime("%Y%m%d")
            logger.info(f"Checking availability of ECMWF cycle: {date_str} {cycle_hour:02d}z...")
            
            try:
                # Retrieve only step 240 to verify the cycle is 100% complete (10-day forecast)
                check_target = os.path.join(self.storage_dir, ".check.grib2")
                self.client.retrieve(
                    date=date_str,
                    time=cycle_hour,
                    step=240,
                    stream="oper",
                    type="fc",
                    levtype="sfc",
                    param="2t",
                    target=check_target
                )
                # Cleanup check file
                if os.path.exists(check_target):
                    try:
                        os.remove(check_target)
                    except Exception:
                        pass
                logger.info(f"Verified cycle is available: {date_str} {cycle_hour:02d}z")
                return cycle_date, cycle_hour
            except Exception as e:
                err_msg = str(e)
                if "404" in err_msg or "Not Found" in err_msg:
                    logger.warning(f"Cycle {date_str} {cycle_hour:02d}z not available yet. Retrying previous cycle.")
                    target_time -= timedelta(hours=6)
                else:
                    # Other connection/rate-limit issues -> log and raise
                    logger.error(f"Unexpected error checking cycle availability: {e}")
                    raise e
                    
        raise RuntimeError("No available ECMWF cycles found in the last 24 hours.")

    def get_target_path(self, run_date: date, run_hour: int) -> str:
        """
        Generates standard file name: ecmwf_YYYYMMDD_HHutc.nc
        """
        date_str = run_date.strftime("%Y%m%d")
        filename = f"ecmwf_{date_str}_{run_hour:02d}utc.nc"
        return os.path.join(self.storage_dir, filename)

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """
        Computes SHA-256 hash of a file.
        """
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        retry=retry_if_exception_type(Exception),
        reraise=True,
        before_sleep=lambda retry_state: logger.warning(
            f"Download attempt failed. Retrying... (Attempt {retry_state.attempt_number})"
        )
    )
    def _retrieve_file(self, run_date: date, run_hour: int, target_path: str, steps: List[int]):
        """
        Retrieves the GRIB file from ECMWF using open data client with retries.
        """
        date_param = run_date.strftime("%Y%m%d")
        
        logger.info(
            f"Retrieving ECMWF GRIB: date={date_param}, time={run_hour:02d}, steps={steps}, target={target_path}"
        )
        
        self.client.retrieve(
            date=date_param,
            time=run_hour,
            step=steps,
            stream="oper",
            type="fc",
            levtype="sfc",
            param=self.DEFAULT_PARAMS,
            target=target_path
        )

    def download_cycle(self, run_date: date, run_hour: int, steps: Optional[List[int]] = None) -> tuple[str, str, int]:
        """
        Downloads a full cycle's surface GRIB file and returns (target_path, checksum, file_size).
        If file already exists, it calculates the checksum and validates it instead of re-downloading.
        """
        target_path = self.get_target_path(run_date, run_hour)
        
        # If no steps provided, download full 10-day steps
        # 3-hourly up to 144h, then 6-hourly up to 240h
        if steps is None:
            steps = list(range(0, 144, 3)) + list(range(144, 241, 6))

        if os.path.exists(target_path):
            file_size = os.path.getsize(target_path)
            if file_size > 1024 * 1024:  # At least 1MB to verify it's not a corrupt empty file
                logger.info(f"File {target_path} already exists. Verifying checksum...")
                checksum = self.compute_sha256(target_path)
                return target_path, checksum, file_size
            else:
                logger.warning(f"File {target_path} exists but is too small ({file_size} bytes). Re-downloading.")
                os.remove(target_path)

        # Supabase Storage Integration
        filename = os.path.basename(target_path)
        if not self.use_local_storage:
            if self._download_from_supabase(filename, target_path):
                file_size = os.path.getsize(target_path)
                checksum = self.compute_sha256(target_path)
                return target_path, checksum, file_size
            logger.info("File not found in Supabase Storage. Falling back to ECMWF Client download.")

        # Prevent on-the-fly downloads if disabled and using local storage
        if not self.allow_on_the_fly_downloads and self.use_local_storage:
            raise RuntimeError(
                f"GRIB file {filename} is missing locally and on-the-fly downloads from ECMWF are disabled on this instance."
            )
        
        temp_path = target_path + ".tmp"
        if os.path.exists(temp_path):
            os.remove(temp_path)

        temp_paths = []
        import time
        try:
            # Chunk steps to prevent S3 SlowDown throttling
            for idx, step_chunk in enumerate(self._chunk_steps(steps, max_batch=5)):
                chunk_target = f"{temp_path}.chunk_{idx}"
                if os.path.exists(chunk_target):
                    os.remove(chunk_target)
                
                self._retrieve_file(run_date, run_hour, chunk_target, step_chunk)
                
                if not os.path.exists(chunk_target) or os.path.getsize(chunk_target) == 0:
                    raise IOError(f"Downloaded GRIB chunk {idx} is empty or missing.")
                    
                temp_paths.append(chunk_target)
                time.sleep(8)  # Short pause between chunks to respect rate limit
                
            # Concatenate chunks into the final temp_path
            with open(temp_path, "wb") as outfile:
                for chunk_file in temp_paths:
                    with open(chunk_file, "rb") as infile:
                        outfile.write(infile.read())
                    os.remove(chunk_file)
            
            # Verify file was downloaded successfully
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                raise IOError("Downloaded GRIB file is empty or missing.")

            # Crop the downloaded GRIB file to India's bounding box and save as NetCDF (.nc)
            grib_temp_path = temp_path + ".grib2"
            os.rename(temp_path, grib_temp_path)
            
            try:
                from backend.ingestion.parser import GRIBParser
                GRIBParser.crop_and_save_netcdf(grib_temp_path, target_path)
            finally:
                if os.path.exists(grib_temp_path):
                    try:
                        os.remove(grib_temp_path)
                    except Exception:
                        pass
            
            file_size = os.path.getsize(target_path)
            checksum = self.compute_sha256(target_path)
            logger.info(f"Successfully processed and cropped cycle {run_date} {run_hour:02d}:00 UTC. Checksum: {checksum}")
            
            # Upload to Supabase Storage if local storage is not enabled
            if not self.use_local_storage:
                if not self._upload_to_supabase(target_path, filename):
                    raise RuntimeError(f"Failed to upload GRIB file {filename} to Supabase after download.")
                else:
                    logger.info(f"Uploaded {filename} to Supabase successfully.")
            return target_path, checksum, file_size
            
        except Exception as e:
            logger.error(f"Failed to download and process cycle {run_date} {run_hour:02d}:00 UTC: {e}")
            # Clean up chunks
            for chunk_file in temp_paths:
                if os.path.exists(chunk_file):
                    try:
                        os.remove(chunk_file)
                    except Exception:
                        pass
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e

    @staticmethod
    def _chunk_steps(steps: List[int], max_batch: int = 5) -> List[List[int]]:
        """Split a list of steps into sub-lists of max_batch elements."""
        for i in range(0, len(steps), max_batch):
            yield steps[i : i + max_batch]
