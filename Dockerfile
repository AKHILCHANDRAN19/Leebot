FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    aria2 \
    unzip \
    unrar \
    p7zip-full \
    ffmpeg \
    libtorrent-rasterbar-dev \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY bot.py .

# Create necessary directories
RUN mkdir -p downloads extracted upload

# Run the bot
CMD ["python", "bot.py"]
