import os
import time
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from sqlalchemy import select, and_, delete

from backend.database.connection import SessionLocal
from backend.database.init import init_db
from backend.database.models import WeatherJob, WeatherRawFile, WeatherForecast, WeatherDaily
from backend.ingestion.downloader import ECMWFDownloader
from backend.workers.providers.factory import WeatherProviderFactory
from backend import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("weather_scheduler")

# Global data retention configuration (defaults to 2 days)
RETENTION_DAYS = config.RETENTION_DAYS

class WeatherScheduler:
    """
    Periodic worker that checks for new forecast cycles for all integrated providers,
    downloads them, and prunes stale data and files.
    """
    
    def __init__(self, check_interval_seconds: int = 1800, retention_days: Optional[int] = None, concurrent: bool = True):
        self.check_interval_seconds = check_interval_seconds
        self.retention_days = retention_days if retention_days is not None else RETENTION_DAYS
        self.downloader = ECMWFDownloader()
        self.concurrent = concurrent

    def run_once(self):
        """Runs a single iteration of the scheduler checks for all providers concurrently or sequentially."""
        # Cleanup stale jobs first
        db_cleanup = SessionLocal()
        try:
            self._cleanup_stale_jobs(db_cleanup)
        except Exception as e:
            logger.error(f"Error cleaning up stale jobs: {e}", exc_info=True)
        finally:
            db_cleanup.close()

        providers = [
            WeatherProviderFactory.get_provider("ecmwf"),
            WeatherProviderFactory.get_provider("imd")
        ]
        
        def check_and_process_provider(provider):
            db = SessionLocal()
            try:
                # 1. Determine the latest expected cycle
                current_time = datetime.now(timezone.utc)
                cycle_dt = provider.get_latest_available_cycle(current_time)
                logger.info(f"[{provider.name.upper()}] Target forecast cycle to check: {cycle_dt.strftime('%Y-%m-%d %H:00')} UTC")
                
                # 2. Check if this cycle is already successfully processed or currently running
                stmt = select(WeatherJob).where(
                    and_(
                        WeatherJob.cycle_time == cycle_dt,
                        WeatherJob.status.in_(["SUCCESS", "RUNNING"]),
                        WeatherJob.provider == provider.name
                    )
                )
                existing_job = db.execute(stmt).scalars().first()
                
                if existing_job:
                    logger.info(f"[{provider.name.upper()}] Cycle {cycle_dt} is already handled (Status: {existing_job.status}). Skipping.")
                else:
                    logger.info(f"[{provider.name.upper()}] New cycle detected! Starting ingestion for cycle {cycle_dt}...")
                    self._process_cycle(db, cycle_dt, provider)
            except Exception as e:
                logger.error(f"Error in scheduler check for provider {provider.name}: {e}", exc_info=True)
            finally:
                db.close()

        if self.concurrent:
            # Run provider checks concurrently in parallel threads
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(providers)) as executor:
                executor.map(check_and_process_provider, providers)
        else:
            # Run provider checks sequentially (e.g. for testing)
            for provider in providers:
                check_and_process_provider(provider)
            
        # 3. Data Retention Pruning (cleans DB and local/Supabase storage files)
        db_prune = SessionLocal()
        try:
            self._prune_stale_data(db_prune)
        except Exception as e:
            logger.error(f"Error pruning stale data: {e}", exc_info=True)
        finally:
            db_prune.close()

    def _cleanup_stale_jobs(self, db):
        """Marks stuck RUNNING jobs (older than 15 minutes) as FAILED so they can be retried."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        stmt = select(WeatherJob).where(
            and_(
                WeatherJob.status == "RUNNING",
                WeatherJob.created_at < cutoff
            )
        )
        stale_jobs = db.execute(stmt).scalars().all()
        for job in stale_jobs:
            logger.info(f"Marking stale RUNNING job {job.id} for cycle {job.cycle_time} ({job.provider}) as FAILED (exceeded 15 min timeout).")
            job.status = "FAILED"
            job.error_message = "Job timed out or container crashed."
        if stale_jobs:
            db.commit()

    def _process_cycle(self, db, cycle_dt: datetime, provider):
        """Downloads and registers the raw file for the given cycle and provider."""
        # Create Job entry
        job = WeatherJob(cycle_time=cycle_dt, status="RUNNING", provider=provider.name)
        db.add(job)
        db.commit()
        
        try:
            # Download cycle GRIB/NetCDF
            target_path, checksum, file_size = provider.download_cycle(cycle_dt)
            
            # Register Raw File
            raw_file = WeatherRawFile(
                job_id=job.id,
                cycle_time=cycle_dt,
                file_path=target_path,
                file_size=file_size,
                checksum=checksum,
                provider=provider.name
            )
            db.add(raw_file)
            
            # Update Job status
            job.status = "SUCCESS"
            db.commit()
            logger.info(f"[{provider.name.upper()}] Cycle {cycle_dt} successfully ingested.")
            
            # Pre-populate active grid points in the background
            try:
                self._pre_populate_active_points(db, cycle_dt, provider)
            except Exception as prep_err:
                logger.error(f"[{provider.name.upper()}] Failed to pre-populate active grid points: {prep_err}", exc_info=True)
            
        except Exception as e:
            logger.error(f"[{provider.name.upper()}] Failed to process cycle {cycle_dt}: {e}")
            job.status = "FAILED"
            job.error_message = str(e)
            db.commit()

    def _pre_populate_active_points(self, db, cycle_dt: datetime, provider):
        """Pre-populates the forecast tables for all unique grid points that were previously queried."""
        # Find all unique grid points currently stored in the database
        stmt = select(WeatherForecast.latitude, WeatherForecast.longitude).distinct()
        active_points = db.execute(stmt).all()
        
        if not active_points:
            logger.info(f"[{provider.name.upper()}] No active grid points found in database to pre-populate.")
            return
            
        logger.info(f"[{provider.name.upper()}] Pre-populating {len(active_points)} active grid points for cycle {cycle_dt}...")
        
        is_sqlite = (db.bind.dialect.name == "sqlite") if db.bind else False
        
        if is_sqlite:
            # Sequential ingestion for SQLite in tests/development
            from backend.workers.pipeline import WeatherPipeline
            pipeline = WeatherPipeline(db, provider_name=provider.name)
            for pt in active_points:
                lat, lon = float(pt[0]), float(pt[1])
                try:
                    pipeline._ingest_grid_point(lat, lon, cycle_dt, provider)
                except Exception as e:
                    logger.error(f"[{provider.name.upper()}] Failed to pre-populate grid point ({lat}, {lon}) in background: {e}")
        else:
            # Ingest concurrently on PostgreSQL/production
            from concurrent.futures import ThreadPoolExecutor
            from backend.workers.pipeline import WeatherPipeline
            from backend.database.connection import SessionLocal
            
            def ingest_task(pt):
                lat, lon = float(pt[0]), float(pt[1])
                thread_db = SessionLocal()
                try:
                    pipeline = WeatherPipeline(thread_db, provider_name=provider.name)
                    pipeline._ingest_grid_point(lat, lon, cycle_dt, provider)
                except Exception as e:
                    logger.error(f"[{provider.name.upper()}] Failed to pre-populate grid point ({lat}, {lon}) in background: {e}")
                finally:
                    thread_db.close()
                    
            # Limit concurrency to 4 threads to avoid database connection exhaustion
            with ThreadPoolExecutor(max_workers=min(4, len(active_points))) as executor:
                executor.map(ingest_task, active_points)
            
        logger.info(f"[{provider.name.upper()}] Finished pre-populating active grid points for cycle {cycle_dt}.")

    def _prune_stale_data(self, db):
        """Deletes database entries and physical files older than retention period."""
        # Calculate cutoff based on calendar days to keep exactly T and T-1 days of data
        today = datetime.now(timezone.utc).date()
        cutoff_date = today - timedelta(days=self.retention_days - 1)
        cutoff_dt = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=timezone.utc)
        logger.info(f"Pruning stale data and files older than {self.retention_days} calendar days (cutoff: {cutoff_dt})...")
        
        try:
            # 1. Query raw files metadata older than cutoff
            stmt = select(WeatherRawFile).where(WeatherRawFile.cycle_time < cutoff_dt)
            old_raw_files = db.execute(stmt).scalars().all()
            
            # 2. Physically delete the files locally and from Supabase
            for raw_file in old_raw_files:
                filename = os.path.basename(raw_file.file_path)
                
                # Delete local file if it exists
                if os.path.exists(raw_file.file_path):
                    try:
                        os.remove(raw_file.file_path)
                        logger.info(f"Deleted old local raw file: {raw_file.file_path}")
                    except Exception as e:
                        logger.error(f"Failed to delete local raw file {raw_file.file_path}: {e}")
                
                # Delete from Supabase Storage
                try:
                    self.downloader._delete_from_supabase(filename)
                except Exception as e:
                    logger.error(f"Failed to delete {filename} from Supabase: {e}")
            
            # 3. Database deletions
            # Delete stale hourly forecasts
            forecast_deleted = db.execute(
                delete(WeatherForecast).where(WeatherForecast.forecast_time < cutoff_dt)
                .execution_options(synchronize_session=False)
            ).rowcount
            
            # Delete stale daily forecasts
            daily_deleted = db.execute(
                delete(WeatherDaily).where(WeatherDaily.forecast_date < cutoff_date)
                .execution_options(synchronize_session=False)
            ).rowcount
            
            # Delete stale raw file entries
            raw_files_deleted = db.execute(
                delete(WeatherRawFile).where(WeatherRawFile.cycle_time < cutoff_dt)
                .execution_options(synchronize_session=False)
            ).rowcount
            
            # Delete old jobs
            jobs_deleted = db.execute(
                delete(WeatherJob).where(WeatherJob.cycle_time < cutoff_dt)
                .execution_options(synchronize_session=False)
            ).rowcount
            
            db.commit()
            if forecast_deleted > 0 or daily_deleted > 0 or raw_files_deleted > 0 or jobs_deleted > 0:
                logger.info(
                    f"Pruned DB: {forecast_deleted} hourly forecasts, {daily_deleted} daily forecasts, "
                    f"{raw_files_deleted} raw file records, {jobs_deleted} jobs."
                )
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to prune stale data/files: {e}")

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
