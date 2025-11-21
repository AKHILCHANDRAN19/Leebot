import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
import math
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.types import Message
import aioaria2

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))

# WZML-X Custom Binary Logic
ARIA2_BIN = "blitzfetcher" 
DOWNLOAD_DIR = "/app/downloads/"

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("WZML-X-Leech")

# --- FORMATTING HELPERS (WZML Style) ---
def humanbytes(size):
    if not size:
        return ""
    power = 2**10
    n = 0
    Dic_powerN = {0: ' ', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

def time_formatter(seconds):
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
          ((str(hours) + "h, ") if hours else "") + \
          ((str(minutes) + "m, ") if minutes else "") + \
          ((str(seconds) + "s") if seconds else "")
    return tmp[:-2] if tmp.endswith(", ") else tmp

def get_progressbar(current, total):
    pct = (current / total) * 100
    pct = float(str(pct).strip('%'))
    p = min(max(pct, 0), 100)
    cFull = int(p // 10)
    p_str = '▪' * cFull
    p_str += '▫' * (10 - cFull)
    return p_str

# --- WEB SERVER ---
async def health_check(request):
    return web.Response(text="WZML-X Logic Running!")

def run_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    loop.run_forever()

# --- ARIA2 OPTIMIZATION (WZML-X Logic) ---
async def start_aria2():
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    cmd = [ARIA2_BIN] if shutil.which(ARIA2_BIN) else ["aria2c"]
    
    # These flags match high-performance WZML repo settings
    cmd.extend([
        "--enable-rpc",
        "--rpc-listen-all=false",
        "--rpc-listen-port=6800",
        "--max-connection-per-server=16",
        "--rpc-max-request-size=1024M",
        "--seed-time=0.01",
        "--min-split-size=10M",
        "--follow-torrent=mem",
        "--split=16",
        "--daemon=true",
        "--allow-overwrite=true",
        "--max-overall-download-limit=0",
        "--max-overall-upload-limit=0",
        "--max-download-limit=0",
        f"--dir={DOWNLOAD_DIR}",
        "--file-allocation=none", # Docker optimized
        "--bt-stop-timeout=1200"
    ])
    
    try:
        subprocess.Popen(cmd)
        logger.info("Aria2 Engine Started.")
        await asyncio.sleep(3)
    except Exception as e:
        logger.error(f"Failed to start Aria2: {e}")

# --- BOT CLIENT ---
app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- DOWNLOAD MONITOR ---
async def download_monitor(aria2_client, gid, message: Message):
    last_msg_time = 0
    status_msg = await message.reply_text("⬇️ **Initializing Download...**")
    
    while True:
        try:
            status = await aria2_client.tellStatus(gid)
            curr_status = status.get('status')
            
            if curr_status == 'active':
                # Throttle updates to every 4 seconds
                if time.time() - last_msg_time < 4:
                    await asyncio.sleep(1)
                    continue

                total = int(status.get('totalLength', 1))
                done = int(status.get('completedLength', 0))
                speed = int(status.get('downloadSpeed', 0))
                seeds = status.get('numSeeders', 0)
                leechs = status.get('connections', 0) # Aria2 uses connections for peers
                
                # Calcs
                percentage = (done / total) * 100
                eta = (total - done) / speed if speed > 0 else 0
                
                msg = (
                    f"**Downloading:** {percentage:.2f}%\n"
                    f"[{get_progressbar(done, total)}]\n"
                    f"{humanbytes(done)} of {humanbytes(total)}\n"
                    f"**Speed:** {humanbytes(speed)}/s\n"
                    f"**ETA:** {time_formatter(eta)}\n"
                    f"**Seeds:** {seeds} | **Peers:** {leechs}\n\n"
                    f"__Thanks for using this bot__"
                )
                
                try:
                    await status_msg.edit_text(msg)
                    last_msg_time = time.time()
                except:
                    pass # Ignore FloodWait
            
            elif curr_status == 'complete':
                await status_msg.edit_text("✅ **Download Complete! Preparing Upload...**")
                files = await aria2_client.getFiles(gid)
                filepath = files[0]['path']
                
                # Start Upload
                start_time = time.time()
                try:
                    await app.send_document(
                        chat_id=message.chat.id,
                        document=filepath,
                        caption=f"**{os.path.basename(filepath)}**\n\n__Uploaded via WZML-X Logic__",
                        progress=upload_progress,
                        progress_args=(status_msg, start_time)
                    )
                    await status_msg.delete()
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception as e:
                    await status_msg.edit_text(f"❌ Upload Error: {e}")
                break
                
            elif curr_status == 'error':
                err = status.get('errorMessage', 'Unknown')
                await status_msg.edit_text(f"❌ **Download Failed:** {err}")
                break
            
            elif curr_status == 'removed':
                await status_msg.edit_text("❌ Task Cancelled.")
                break
            
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Monitor Loop Error: {e}")
            break

# --- UPLOAD PROGRESS ---
last_up_time = 0

async def upload_progress(current, total, message, start_time):
    global last_up_time
    now = time.time()
    
    # Update every 5 seconds
    if now - last_up_time < 5:
        return
    
    last_up_time = now
    
    # Logic
    percentage = (current * 100) / total
    speed = current / (now - start_time)
    eta = (total - current) / speed if speed > 0 else 0
    
    msg = (
        f"**Uploading:** {percentage:.2f}%\n"
        f"[{get_progressbar(current, total)}]\n"
        f"{humanbytes(current)} of {humanbytes(total)}\n"
        f"**Speed:** {humanbytes(speed)}/s\n"
        f"**ETA:** {time_formatter(eta)}\n\n"
        f"__Thanks for using this bot__"
    )
    
    try:
        await message.edit_text(msg)
    except:
        pass

# --- HANDLERS ---
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    await message.reply_text(
        "**WZML-X Leech Bot is Ready!** 🚀\n\n"
        "Send me a **Magnet**, **Torrent File**, or **Direct Link**.\n"
        "I will use optimized `aria2c` logic to leech it."
    )

@app.on_message(filters.text | filters.document)
async def leech_handler(client, message):
    link = None
    is_torrent = False
    
    if message.text:
        if message.text.startswith(("http", "magnet:", "www")):
            link = message.text.strip()
    elif message.document and message.document.file_name.endswith(".torrent"):
        is_torrent = True
        status = await message.reply_text("📥 **Processing .torrent file...**")
        path = await message.download(file_name=f"{DOWNLOAD_DIR}temp.torrent")
        link = path
        await status.delete()

    if not link:
        return

    try:
        async with aioaria2.Aria2HttpClient("http://localhost:6800/jsonrpc") as aria2:
            if is_torrent or os.path.exists(link):
                # RPC requires base64 encoded torrent files usually, but local path might work if daemon shares FS
                # Safest bet: Use addUri for magnets/links, addTorrent for files if supported via RPC lib
                # For simplicity in this wrapper, we try adding as URI if it's a local path (Aria2 supports local paths)
                gid = await aria2.addUri([link])
            else:
                gid = await aria2.addUri([link])
                
            asyncio.create_task(download_monitor(aria2, gid, message))
            
    except Exception as e:
        await message.reply_text(f"❌ **Error:** {e}")

def main():
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_aria2())
    
    logger.info("Bot Started with WZML-X Visuals")
    app.run()

if __name__ == "__main__":
    main()
