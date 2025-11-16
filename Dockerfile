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
    curl \
    wget \
    jq \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install unrar by compiling it from source
RUN wget --no-check-certificate https://www.rarlab.com/rar/unrarsrc-6.2.10.tar.gz && \
    tar -xzvf unrarsrc-6.2.10.tar.gz && \
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

# Set permissions
RUN chmod +x bot.py

# Run the bot
CMD ["python", "bot.py"]
