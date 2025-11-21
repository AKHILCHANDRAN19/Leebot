import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
import math
from aiohttp import web
from pyrogram import Client, filters
import aioaria2
import uvloop

# Install uvloop for WZML-X speed standards
uvloop.install()

# --- CREDENTIALS ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))

# --- CONSTANTS ---
DOWNLOAD_DIR = "/app/downloads/"
ARIA2_BIN = "blitzfetcher" # WZML-X Binary Name

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("WZML-X")

# --- WZML-X FORMATTING LOGIC (EXACT COPY) ---
SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

def get_readable_file_size(size_in_bytes):
    if size_in_bytes is None:
        return '0B'
    index = 0
    while size_in_bytes >= 1024:
        size_in_bytes /= 1024
        index += 1
    try:
        return f'{round(size_in_bytes, 2)} {SIZE_UNITS[index]}'
    except IndexError:
        return '0B'

def get_readable_time(seconds):
    if not seconds: return "0s"
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    days = int(days)
    if days != 0: result += f"{days}d "
    (hours, remainder) = divmod(remainder, 3600)
    hours = int(hours)
    if hours != 0: result += f"{hours}h "
    (minutes, seconds) = divmod(remainder, 60)
    minutes = int(minutes)
    if minutes != 0: result += f"{minutes}m "
    seconds = int(seconds)
    result += f"{seconds}s"
    return result

def get_progress_bar_string(pct):
    pct = float(str(pct).strip('%'))
    p = min(max(pct, 0), 100)
    cFull = int(p // 10)
    p_str = '▪' * cFull
    p_str += '▫' * (10 - cFull)
    return f"[{p_str}]"

# --- WEB SERVER (RENDER HEALTH CHECK) ---
async def health_check(request): 
    return web.Response(text="WZML-X Leeching Service Active")

def run_web():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    loop.run_forever()

# --- ARIA2 ENGINE ---
async def start_aria2():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    
    # Try to find blitzfetcher (WZML binary) or fallback to aria2c
    binary = shutil.which(ARIA2_BIN) or "aria2c"
    
    # Optimized Flags for Leeching
    cmd = [
        binary,
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
        f"--dir={DOWNLOAD_DIR}",
        "--bt-stop-timeout=1200"
    ]
    subprocess.Popen(cmd)
    await asyncio.sleep(2)

# --- BOT CLIENT ---
app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- UPLOAD HANDLER ---
last_up_time = 0
async def upload_progress(current, total, message, start_time):
    global last_up_time
    now = time.time()
    if now - last_up_time < 5: return
    last_up_time = now

    percentage = (current * 100) / total
    speed = current / (now - start_time)
    eta = (total - current) / speed if speed > 0 else 0

    # WZML-X Upload Template
    text = f"""Uploading: {percentage:.2f}%
{get_progress_bar_string(percentage)}
{get_readable_file_size(current)} of {get_readable_file_size(total)}
Speed: {get_readable_file_size(speed)}/sec
ETA: {get_readable_time(eta)}

Thanks for using this bot"""
    
    try: await message.edit(text)
    except: pass

# --- DOWNLOAD HANDLER ---
async def download_monitor(aria2, gid, message):
    last_msg_time = 0
    status_msg = await message.reply("⬇️ **Initializing Download...**")
    
    while True:
        try:
            # Force update status
            status = await aria2.tellStatus(gid)
            stat = status.get('status')

            # --- CRITICAL FIX FOR MAGNET STUCK ---
            # If Aria2 finishes Metadata download, it returns 'complete' 
            # AND provides a 'followedBy' GID. We MUST switch to that GID.
            if stat == 'complete' and 'followedBy' in status:
                new_gid = status['followedBy'][0]
                gid = new_gid 
                await status_msg.edit("🧲 **Metadata Downloaded. Starting Files...**")
                await asyncio.sleep(1)
                continue 
            # -------------------------------------

            if stat == 'error':
                err = status.get('errorMessage', 'Unknown Error')
                await status_msg.edit(f"❌ Error: {err}")
                return

            if stat == 'complete':
                await status_msg.edit("✅ **Download Complete. Extracting...**")
                
                # Get File Logic
                files = await aria2.getFiles(gid)
                # WZML Logic: Find largest file to upload if multiple
                filepath = files[0]['path']
                if len(files) > 1:
                    # Simple logic: find largest file
                    filepath = max(files, key=lambda x: int(x['length']))['path']

                if not os.path.exists(filepath):
                    await status_msg.edit("❌ Error: File not found on server.")
                    return

                # Upload
                start = time.time()
                try:
                    await app.send_document(
                        chat_id=message.chat.id,
                        document=filepath,
                        caption=f"**{os.path.basename(filepath)}**",
                        progress=upload_progress,
                        progress_args=(status_msg, start)
                    )
                    await status_msg.delete()
                    # Cleanup
                    try: shutil.rmtree(DOWNLOAD_DIR)
                    except: pass
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                except Exception as e:
                    await status_msg.edit(f"❌ Upload Error: {e}")
                return

            # Progress Bar Logic
            if stat == 'active' or stat == 'waiting':
                # Throttle Telegram Edits (Avoid FloodWait)
                if time.time() - last_msg_time < 4:
                    await asyncio.sleep(1)
                    continue

                done = int(status.get('completedLength', 0))
                total = int(status.get('totalLength', 1))
                speed = int(status.get('downloadSpeed', 0))
                
                percentage = (done / total) * 100 if total > 0 else 0
                
                if speed > 0:
                    eta = (total - done) / speed
                else:
                    eta = 0

                # WZML-X Download Template (Exact Match)
                msg = f"""Downloading: {percentage:.2f}%
{get_progress_bar_string(percentage)}
{get_readable_file_size(done)} of {get_readable_file_size(total)}
Speed: {get_readable_file_size(speed)}/sec
ETA: {get_readable_time(eta)}

Thanks for using this bot"""
                
                try:
                    await status_msg.edit(msg)
                    last_msg_time = time.time()
                except: pass

            await asyncio.sleep(2)

        except Exception as e:
            print(f"Loop Error: {e}")
            await asyncio.sleep(2)

# --- COMMANDS ---
@app.on_message(filters.command("start"))
async def start_h(c, m):
    await m.reply_text("**WZML-X Leech Bot**\nSend a Magnet link, Torrent file, or Direct URL.")

@app.on_message(filters.document | filters.text)
async def leech_h(c, m):
    link = None
    
    if m.document and m.document.file_name.endswith(".torrent"):
        msg = await m.reply("📥 **Reading Torrent File...**")
        link = await m.download(file_name=f"{DOWNLOAD_DIR}job.torrent")
        await msg.delete()
    elif m.text:
        link = m.text.strip()

    if not link: return

    try:
        async with aioaria2.Aria2HttpClient("http://localhost:6800/jsonrpc") as aria2:
            # Add Download
            gid = await aria2.addUri([link])
            # Start Monitor
            asyncio.create_task(download_monitor(aria2, gid, m))
    except Exception as e:
        await m.reply(f"❌ Error: {e}")

def main():
    # Start Web Server (Threaded)
    threading.Thread(target=run_web, daemon=True).start()
    
    # Start Aria2 (Async)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_aria2())
    
    # Start Bot
    logger.info("WZML-X Leech Bot Started")
    app.run()

if __name__ == "__main__":
    main()
