import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

from backend.database.connection import get_db
from backend.database.init import init_db
from backend.api.routes import router as weather_router

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Handles startup and shutdown events.
    Verifies and initializes the database schema.
    """
    logger.info("Starting up ECMWF Weather Platform FastAPI server...")
    try:
        init_db()
    except Exception as e:
        logger.error(f"Failed to initialize database during startup: {e}")
    yield
    logger.info("Shutting down ECMWF Weather Platform FastAPI server...")

app = FastAPI(
    title="ECMWF Weather Platform API",
    description="Optimized, production-ready APIs serving ECMWF weather forecasts.",
    version="1.0.0",
    lifespan=lifespan
)

# 1. Enable Gzip Compression (Mobile optimization)
# Compresses responses larger than 500 bytes
app.add_middleware(GZipMiddleware, minimum_size=500)

# 2. CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register endpoints
app.include_router(weather_router)

@app.get("/health", tags=["health"])
def health_check(db: Session = Depends(get_db)):
    """
    Standard health check endpoint.
    Verifies that the API service is up and database connection is functional.
    """
    try:
        # Perform lightweight check
        db.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "database": "connected",
            "environment": os.getenv("ENVIRONMENT", "production")
        }
    except Exception as e:
        logger.error(f"Healthcheck failed: {e}")
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e)
        }
