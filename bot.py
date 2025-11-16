# bot.py
import os
import logging
import asyncio
import re
import time
import shutil
import psutil
from typing import Dict

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

# --- Initial Setup ---

# Load credentials from .env file for local testing
load_dotenv()

# Set up logging to see bot's activity
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
LOGGER = logging.getLogger(__name__)

# --- Configuration ---

# Securely get credentials from environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

# Check if essential credentials are provided
if not all([BOT_TOKEN, API_ID, API_HASH]):
    LOGGER.critical("CRITICAL ERROR: BOT_TOKEN, API_ID, and API_HASH must be set in your environment or .env file.")
    exit(1)

# --- Pyrogram Client Initialization ---
app = Client(
    "telegram_leech_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Helper Functions ---

def get_readable_time(seconds: int) -> str:
    """Converts seconds into a human-readable format."""
    result = ''
    (days, remainder) = divmod(seconds, 86400)
    days = int(days)
    if days != 0:
        result += f'{days}d '
    (hours, remainder) = divmod(remainder, 3600)
    hours = int(hours)
    if hours != 0:
        result += f'{hours}h '
    (minutes, seconds) = divmod(remainder, 60)
    minutes = int(minutes)
    if minutes != 0:
        result += f'{minutes}m '
    seconds = int(seconds)
    result += f'{seconds}s'
    return result

def get_readable_bytes(size_in_bytes: int) -> str:
    """Converts bytes into a human-readable format."""
    if size_in_bytes is None:
        return '0B'
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size_in_bytes > power and n < len(power_labels) -1:
        size_in_bytes /= power
        n += 1
    return f"{round(size_in_bytes, 2)}{power_labels[n]}B"

# --- Bot Command Handlers ---

@app.on_message(filters.command("start"))
async def start_command(_, message: Message):
    """Handles the /start command."""
    await message.reply_text(
        "Hello! I am a high-speed Leech Bot.\n"
        "Send me a magnet link or a torrent file to start downloading.\n"
        "Usage: `/leech <magnet_link>`"
    )

@app.on_message(filters.command("leech"))
async def leech_command_handler(_, message: Message):
    """Handles magnet links and starts the download process."""
    if len(message.command) < 2:
        await message.reply_text("Please provide a magnet link after the `/leech` command.")
        return

    magnet_link = " ".join(message.command[1:])
    if not magnet_link.startswith("magnet:?xt=urn:btih:"):
        await message.reply_text("This does not appear to be a valid magnet link.")
        return
    
    await download_and_upload(message, magnet_link)

# --- Core Downloading & Uploading Logic ---

async def download_and_upload(message: Message, magnet_link: str):
    """The main function to handle downloading with aria2c and uploading to Telegram."""
    
    download_dir = f"downloads/{message.id}"
    os.makedirs(download_dir, exist_ok=True)
    
    status_message = await message.reply_text("`Parsing magnet link...`", quote=True)
    start_time = time.time()
    
    aria_command = (
        f'aria2c --console-log-level=warn --summary-interval=1 --seed-time=0 -x16 -j16 -k1M '
        f'--max-connection-per-server=16 --min-split-size=1M --max-tries=3 '
        f'-d "{download_dir}" "{magnet_link}"'
    )
    
    process = await asyncio.create_subprocess_shell(
        aria_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    last_update_time = 0
    
    async for line in process.stdout:
        line = line.decode('utf-8').strip()
        
        if "ETA" in line and time.time() - last_update_time > 5:
            match = re.search(r'\((\d+)%\).*?DL:\s*([\d\.]+\wB/s).*?ETA:\s*([\w\d]+)', line)
            if match:
                percentage, speed, eta = match.groups()
                progress_bar = "█" * int(int(percentage) / 10) + "░" * (10 - int(int(percentage) / 10))
                
                status_text = (
                    f"**Downloading...**\n\n"
                    f"**Progress:** `[{progress_bar}] {percentage}%`\n"
                    f"**Speed:** `{speed}`\n"
                    f"**ETA:** `{eta}`"
                )
                
                try:
                    await status_message.edit_text(status_text)
                    last_update_time = time.time()
                except FloodWait as e:
                    LOGGER.warning(f"FloodWait: Sleeping for {e.value} seconds.")
                    await asyncio.sleep(e.value)
                except Exception as e:
                    LOGGER.warning(f"Failed to edit message: {e}")

    await process.wait()
    
    if process.returncode != 0:
        stderr_output = (await process.stderr.read()).decode('utf-8')
        LOGGER.error(f"Aria2c failed with error: {stderr_output}")
        await status_message.edit_text(f"**Download failed!**\n\n`{stderr_output}`")
        shutil.rmtree(download_dir)
        return
        
    await status_message.edit_text("✅ **Download complete!**\n\n`Preparing to upload...`")
    
    files_to_upload = [os.path.join(root, file) for root, _, files in os.walk(download_dir) for file in files]
    
    for file_path in files_to_upload:
        file_name = os.path.basename(file_path)
        LOGGER.info(f"Uploading: {file_name}")

        try:
            await app.send_document(
                chat_id=message.chat.id,
                document=file_path,
                caption=f"`{file_name}`",
                reply_to_message_id=message.id
            )
        except Exception as e:
            LOGGER.error(f"Failed to upload {file_name}: {e}")
            await message.reply_text(f"Error uploading `{file_name}`: {e}")
            
    shutil.rmtree(download_dir)
    elapsed_time = get_readable_time(time.time() - start_time)
    await status_message.edit_text(f"**✅ Task Complete!**\n\nAll files uploaded in **{elapsed_time}**.")
    LOGGER.info("Task finished successfully.")


# --- Main Execution ---
if __name__ == "__main__":
    LOGGER.info("Bot is starting...")
    app.run()
