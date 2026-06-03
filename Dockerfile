FROM python:3.12-slim-bookworm

# Install system dependencies for eccodes (required by cfgrib)
RUN apt-get update && apt-get install -y \
    build-essential \
    libeccodes-dev \
    libeccodes-tools \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY backend /app/backend

# Environment variables default
ENV PYTHONPATH=/app
ENV PORT=8000

# Run FastAPI by default (Railway will pass PORT dynamically or we bind to 8000)
CMD ["sh", "-c", "uvicorn backend.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
