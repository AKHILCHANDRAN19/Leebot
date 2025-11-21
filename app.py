import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import Message
import aioaria2

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID", "2819362")
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 8080))

# WZML-X Custom Binary Names
ARIA2_BIN = "blitzfetcher" # In standard systems this is 'aria2c'
DOWNLOAD_DIR = "/app/downloads/"

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WZML-Leech")

# --- WEB SERVER (HEALTH CHECK) ---
async def health_check(request):
    return web.Response(text="Bot is Running Successfully!")

def run_web_server():
    """Runs aiohttp web server in a separate thread."""
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    logger.info(f"Web Server started on port {PORT}")
    loop.run_forever()

# --- ARIA2 MANAGER ---
async def start_aria2():
    """Starts the Aria2 (Blitzfetcher) background process."""
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    # Check if custom binary exists, else fall back to standard
    cmd = [ARIA2_BIN] if shutil.which(ARIA2_BIN) else ["aria2c"]
    
    cmd.extend([
        "--enable-rpc",
        "--rpc-listen-all=false",
        "--rpc-listen-port=6800",
        "--max-connection-per-server=10",
        "--rpc-max-request-size=1024M",
        "--seed-time=0.01",
        "--min-split-size=10M",
        "--follow-torrent=mem",
        "--split=10",
        f"--dir={DOWNLOAD_DIR}",
        "--daemon=true"
    ])
    
    try:
        subprocess.Popen(cmd)
        logger.info(f"{cmd[0]} daemon started.")
        await asyncio.sleep(2) # Wait for it to initialize
    except Exception as e:
        logger.error(f"Failed to start Aria2: {e}")

# --- BOT LOGIC ---
app = Client(
    "wzml_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

async def download_monitor(aria2_client, gid, message: Message):
    """Monitors download progress and triggers upload on completion."""
    last_status = ""
    status_msg = await message.reply_text("⬇️ **Download Started...**")
    
    while True:
        try:
            status = await aria2_client.tellStatus(gid)
            curr_status = status.get('status')
            
            if curr_status == 'active':
                completed = int(status.get('completedLength', 0))
                total = int(status.get('totalLength', 1))
                speed = int(status.get('downloadSpeed', 0)) / 1024 / 1024 # MB/s
                prog = (completed / total) * 100 if total > 0 else 0
                
                # Update message every 5 seconds to avoid flood wait
                text = f"⬇️ **Downloading...**\nProgress: {prog:.2f}%\nSpeed: {speed:.2f} MB/s"
                if text != last_status:
                    try:
                        await status_msg.edit_text(text)
                        last_status = text
                    except:
                        pass
            
            elif curr_status == 'complete':
                await status_msg.edit_text("✅ **Download Complete! Uploading...**")
                
                # Get file path
                files = await aria2_client.getFiles(gid)
                filepath = files[0]['path']
                
                # Upload to Telegram
                try:
                    await app.send_document(
                        chat_id=message.chat.id,
                        document=filepath,
                        caption="✅ Uploaded via WZML-X Logic",
                        progress=progress_callback,
                        progress_args=(status_msg,)
                    )
                    await status_msg.delete()
                    # Cleanup
                    os.remove(filepath)
                except Exception as up_e:
                    await status_msg.edit_text(f"❌ Upload Failed: {up_e}")
                break
                
            elif curr_status == 'error':
                await status_msg.edit_text("❌ Download Error occured.")
                break
                
            await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"Monitor Error: {e}")
            break

async def progress_callback(current, total, msg):
    """Upload Progress."""
    if total > 0:
        percentage = current * 100 / total
        if int(percentage) % 10 == 0: # Update every 10%
            try:
                await msg.edit_text(f"⬆️ **Uploading:** {percentage:.1f}%")
            except:
                pass

@app.on_message(filters.command("start"))
async def start_handler(client, message):
    await message.reply_text(
        "**WZML-X Leech Bot is Ready!** 🚀\n\n"
        "Send me a **Magnet Link**, **Torrent File**, or **Direct Link**.\n"
        "I will download and upload it back to you."
    )

@app.on_message(filters.text | filters.document)
async def leech_handler(client, message):
    link = None
    
    # Check if text link (Magnet or HTTP)
    if message.text:
        if message.text.startswith(("http", "magnet:")):
            link = message.text.strip()

    # Check if Torrent file
    elif message.document and message.document.file_name.endswith(".torrent"):
        status = await message.reply_text("📥 **Downloading .torrent file...**")
        file_path = await message.download(file_name=f"{DOWNLOAD_DIR}temp.torrent")
        link = file_path
        await status.delete()

    if not link:
        return # Ignore non-link messages

    try:
        # Connect to Aria2
        async with aioaria2.Aria2HttpClient("http://localhost:6800/jsonrpc") as aria2:
            if link.endswith(".torrent") or os.path.exists(link):
                # Add Torrent File
                gid = await aria2.addTorrent(link)
            else:
                # Add URL/Magnet
                gid = await aria2.addUri([link])
            
            # Start Monitoring
            asyncio.create_task(download_monitor(aria2, gid, message))
            
    except Exception as e:
        await message.reply_text(f"❌ Error adding task: {str(e)}")

# --- MAIN EXECUTION ---
def main():
    # 1. Start Web Server in Background Thread (The Fix)
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    # 2. Initialize Aria2 in the Event Loop
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_aria2())
    
    # 3. Start Bot
    logger.info("Starting Pyrogram Client...")
    app.run()

if __name__ == "__main__":
    main()
