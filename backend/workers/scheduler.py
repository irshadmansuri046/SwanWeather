import os
import time
import logging
from datetime import datetime, date, timedelta, timezone
from sqlalchemy import select, and_, delete

from backend.database.connection import SessionLocal
from backend.database.init import init_db
from backend.database.models import WeatherJob, WeatherRawFile, WeatherForecast, WeatherDaily
from backend.ingestion.downloader import ECMWFDownloader
from backend.workers.providers.factory import WeatherProviderFactory

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("weather_scheduler")

class WeatherScheduler:
    """
    Periodic worker that checks for new ECMWF forecast cycles,
    downloads them, and prunes stale data (> 90 days).
    """
    
    def __init__(self, check_interval_seconds: int = 1800, retention_days: int = 90):
        self.check_interval_seconds = check_interval_seconds
        self.retention_days = retention_days
        self.downloader = ECMWFDownloader()

    def run_once(self):
        """Runs a single iteration of the scheduler checks."""
        db = SessionLocal()
        try:
            # Get active provider
            provider = WeatherProviderFactory.get_provider(os.getenv("WEATHER_DATA_PROVIDER", "ecmwf"))
            
            # 1. Determine the latest expected cycle
            current_time = datetime.now(timezone.utc)
            cycle_dt = provider.get_latest_available_cycle(current_time)
            logger.info(f"Target forecast cycle to check: {cycle_dt.strftime('%Y-%m-%d %H:00')} UTC")
            
            # 2. Check if this cycle is already successfully processed or currently running
            stmt = select(WeatherJob).where(
                and_(
                    WeatherJob.cycle_time == cycle_dt,
                    WeatherJob.status.in_(["SUCCESS", "RUNNING"])
                )
            )
            existing_job = db.execute(stmt).scalars().first()
            
            if existing_job:
                logger.info(f"Cycle {cycle_dt} is already handled (Status: {existing_job.status}). Skipping.")
            else:
                logger.info(f"New cycle detected! Starting ingestion for cycle {cycle_dt}...")
                self._process_cycle(db, cycle_dt)
                
            # 3. Data Retention Pruning
            self._prune_stale_data(db)
            
        except Exception as e:
            logger.error(f"Error in scheduler iteration: {e}", exc_info=True)
        finally:
            db.close()

    def _process_cycle(self, db, cycle_dt: datetime):
        """Downloads and registers the raw file for the given cycle."""
        # Create Job entry
        job = WeatherJob(cycle_time=cycle_dt, status="RUNNING")
        db.add(job)
        db.commit()
        
        try:
            # Get active provider
            provider = WeatherProviderFactory.get_provider(os.getenv("WEATHER_DATA_PROVIDER", "ecmwf"))
            
            # Download cycle GRIB/NetCDF
            target_path, checksum, file_size = provider.download_cycle(cycle_dt)
            
            # Register Raw File
            raw_file = WeatherRawFile(
                job_id=job.id,
                cycle_time=cycle_dt,
                file_path=target_path,
                file_size=file_size,
                checksum=checksum
            )
            db.add(raw_file)
            
            # Update Job status
            job.status = "SUCCESS"
            db.commit()
            logger.info(f"Cycle {cycle_dt} successfully ingested.")
            
        except Exception as e:
            logger.error(f"Failed to process cycle {cycle_dt}: {e}")
            job.status = "FAILED"
            job.error_message = str(e)
            db.commit()

    def _prune_stale_data(self, db):
        """Deletes database entries older than retention period (90 days)."""
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_date = cutoff_dt.date()
        logger.info(f"Pruning stale data older than {cutoff_dt}...")
        
        try:
            # Delete stale hourly forecasts
            forecast_deleted = db.execute(
                delete(WeatherForecast).where(WeatherForecast.forecast_time < cutoff_dt)
            ).rowcount
            
            # Delete stale daily forecasts
            daily_deleted = db.execute(
                delete(WeatherDaily).where(WeatherDaily.forecast_date < cutoff_date)
            ).rowcount
            
            # Delete old jobs
            jobs_deleted = db.execute(
                delete(WeatherJob).where(WeatherJob.cycle_time < cutoff_dt)
            ).rowcount
            
            db.commit()
            if forecast_deleted > 0 or daily_deleted > 0:
                logger.info(
                    f"Pruned: {forecast_deleted} hourly forecasts, {daily_deleted} daily forecasts, {jobs_deleted} jobs."
                )
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to prune stale data: {e}")

    def start(self):
        """Starts the scheduler loop daemon."""
        logger.info("Initializing database schema on scheduler startup...")
        try:
            init_db()
        except Exception as e:
            logger.error(f"Database setup failed on scheduler startup: {e}")
            
        logger.info(f"Starting scheduler loop. Checking every {self.check_interval_seconds} seconds.")
        while True:
            self.run_once()
            time.sleep(self.check_interval_seconds)

if __name__ == "__main__":
    scheduler = WeatherScheduler()
    scheduler.start()
