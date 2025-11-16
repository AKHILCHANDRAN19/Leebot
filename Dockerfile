FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    aria2 \
    unzip \
    p7zip-full \
    ffmpeg \
    libtorrent-rasterbar-dev \
    python3-dev \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install unrar by compiling it from the official source.
# This is the most reliable method and bypasses repository issues.
RUN wget --no-check-certificate https://www.rarlab.com/rar/unrar-6.2.12.tar.gz && \
    tar -xzvf unrar-6.2.12.tar.gz && \
    cd unrar && \
    make && \
    install -m 755 unrar /usr/bin

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
