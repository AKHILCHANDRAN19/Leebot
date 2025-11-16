FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    aria2 \
    qbittorrent-nox \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Create download directory
RUN mkdir -p /tmp/downloads

# Start command (runs all three services)
CMD ["sh", "-c", "\
    aria2c --daemon --enable-rpc --rpc-listen-all --rpc-secret=$ARIA2_RPC_SECRET --rpc-max-request-size=256M --seed-time=0 & \
    qbittorrent-nox --webui-port=$QB_PORT & \
    sleep 5 && \
    python bot.py \
    "]
