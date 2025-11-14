#!/bin/bash

# WZML-X Leech Bot Start Script

# Create downloads directory
mkdir -p /usr/src/app/downloads

# Kill any existing aria2 processes
pkill -f aria2c || true

# Set Python path
export PYTHONPATH="/usr/src/app:$PYTHONPATH"

# Check if credentials are set
if [ -z "$BOT_TOKEN" ] || [ -z "$TELEGRAM_API" ] || [ -z "$TELEGRAM_HASH" ]; then
    echo "❌ ERROR: BOT_TOKEN, TELEGRAM_API, or TELEGRAM_HASH not set!"
    exit 1
fi

# Start the bot
echo "🚀 Starting WZML-X Leech Bot v1.0..."
echo "📁 Download directory: $DOWNLOAD_DIR"
echo "👤 Owner ID: $OWNER_ID"

# Run bot
python3 bot.py
