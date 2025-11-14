FROM python:3.11-slim

# Install system dependencies (CRITICAL for qBittorrent, ffmpeg, unrar)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    unrar \
    p7zip-full \
    wget \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Create download directory
RUN mkdir -p /tmp/downloads

# Expose Render port
EXPOSE 10000

# Start bot
CMD ["python", "bot.py"]
