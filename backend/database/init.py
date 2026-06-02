import os
import logging
from sqlalchemy import text
from .connection import engine, SessionLocal

logger = logging.getLogger(__name__)

def init_db():
    """Reads init_db.sql and executes it on the database connection."""
    sql_file_path = os.path.join(os.path.dirname(__file__), "init_db.sql")
    
    if not os.path.exists(sql_file_path):
        logger.error(f"init_db.sql not found at {sql_file_path}")
        return
        
    logger.info("Initializing database schema from init_db.sql...")
    
    try:
        with open(sql_file_path, "r") as f:
            sql_script = f.read()
            
        # Execute migration script
        with engine.begin() as conn:
            # Postgres doesn't easily run multiple statements separated by semicolons
            # unless execute() runs the block. SQLAlchemy raw connections handle this well.
            conn.execute(text(sql_script))
            
        logger.info("Database schema initialized successfully.")
    except Exception as e:
        logger.critical(f"Database initialization failed: {e}")
        raise e

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
