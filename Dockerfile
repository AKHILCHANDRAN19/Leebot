FROM python:3.11-slim  # Use 3.11 instead of 3.13 for stability

# Install system dependencies with error handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    aria2 \
    qbittorrent-nox \
    ffmpeg \  # For video thumbnail generation
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies with proper build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Create download directory with proper permissions
RUN mkdir -p /tmp/downloads && chmod 777 /tmp/downloads

# Expose ports (if needed for debugging)
EXPOSE 8080 6800

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:6800')"

# Start script that handles all services
COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]
