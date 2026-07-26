FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (libpq-dev for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create data directory (Render disk mount point — kept for local SQLite compat)
RUN mkdir -p /data

# Expose health check port
EXPOSE 8080

# Start bot
CMD ["sh", "-c", "python -m nodick run"]
