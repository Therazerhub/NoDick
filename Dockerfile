FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Create data directory (Render disk mount point)
RUN mkdir -p /data

# Expose health check port
EXPOSE 8080

# Start bot
CMD ["sh", "-c", "python -m nodick run"]
