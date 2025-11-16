import os
import threading
import subprocess
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message

# --- Configuration ---
API_ID = os.environ.get("API_ID", "2819362")
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0")
DOWNLOAD_DIR = "./downloads/"

# --- Flask App for Render Health Checks ---
app = Flask(__name__)

@app.route('/')
def hello_world():
    return 'Bot is running!'

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

# --- Pyrogram Bot ---
bot = Client("leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Helper Functions ---
def is_valid_link(link):
    """Checks if a link is a magnet link or a torrent file link."""
    return link.startswith("magnet:") or link.endswith(".torrent")

def download_file(url: str, message: Message):
    """Downloads a file using aria2c."""
    try:
        if not os.path.exists(DOWNLOAD_DIR):
            os.makedirs(DOWNLOAD_DIR)

        command = [
            "aria2c",
            "--dir", DOWNLOAD_DIR,
            "--max-connection-per-server", "16",
            "--min-split-size", "1M",
            "--split", "16",
            url
        ]

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()

        if process.returncode == 0:
            filename = stdout.decode().strip().split("complete: ")[-1]
            return os.path.join(DOWNLOAD_DIR, filename)
        else:
            message.reply_text(f"Error downloading file: {stderr.decode()}")
            return None
    except Exception as e:
        message.reply_text(f"An error occurred: {e}")
        return None

# --- Command Handlers ---
@bot.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text("Hi, I'm a leech bot! Send me a torrent or magnet link to start downloading.")

@bot.on_message(filters.command("leech"))
async def leech(client, message):
    if len(message.command) > 1:
        link = message.command[1]
        if is_valid_link(link):
            await message.reply_text("Downloading...")
            filepath = download_file(link, message)
            if filepath:
                await message.reply_document(filepath)
                os.remove(filepath)  # Clean up the file after uploading
            else:
                await message.reply_text("Download failed.")
        else:
            await message.reply_text("Please provide a valid magnet or torrent link.")
    else:
        await message.reply_text("Usage: /leech <magnet/torrent link>")

# --- Main ---
if __name__ == "__main__":
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    # Start the Pyrogram bot
    bot.run()
