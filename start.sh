#!/bin/bash
set -e  # Exit on error

echo "🚀 Starting all services..."

# Create aria2 config
mkdir -p /app/config
cat > /app/config/aria2.conf <<EOF
# Aria2 Configuration
enable-rpc=true
rpc-listen-all=true
rpc-secret=${ARIA2_RPC_SECRET:-default_secret}
rpc-max-request-size=256M
seed-time=0
max-connection-per-server=16
split=10
min-split-size=10M
max-concurrent-downloads=${MAX_PARALLEL:-3}
dir=/tmp/downloads
file-allocation=falloc
EOF

# Start aria2c daemon
echo "Starting aria2c..."
aria2c --daemon --conf-path=/app/config/aria2.conf

# Wait for aria2
sleep 3

# Start qBittorrent daemon (headless)
echo "Starting qBittorrent..."
qbittorrent-nox --daemon \
    --webui-port=${QB_PORT:-8080} \
    --profile=/app/config

# Wait for qBittorrent
sleep 5

# Verify services are running
if ! curl -s "http://localhost:${ARIA2_RPC_PORT:-6800}/jsonrpc" > /dev/null; then
    echo "❌ Aria2c failed to start"
    exit 1
fi

echo "✅ All services started successfully"
echo "📡 Starting Telegram Bot..."

# Start bot
python bot.py
