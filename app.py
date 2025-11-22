import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
from aiohttp import web
from pyrogram import Client, filters
import aioaria2
import uvloop

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))

DOWNLOAD_DIR = "/app/downloads/"
# Explicitly fallback to system aria2c if blitzfetcher is missing
ARIA2_BIN = "blitzfetcher" if shutil.which("blitzfetcher") else "aria2c"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("WZML-X")

# --- WZML FORMATTING LOGIC ---
SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

def get_readable_file_size(size_in_bytes):
    if size_in_bytes is None: return '0B'
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
    return f"[{'▪' * cFull}{'▫' * (10 - cFull)}]"

# --- WEB SERVER ---
async def health_check(request): return web.Response(text="Running")

def run_web():
    # Use standard asyncio loop for web server thread to avoid uvloop conflict
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    loop.run_until_complete(site.start())
    loop.run_forever()

# --- ARIA2 ENGINE ---
def start_aria2():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    cmd = [
        ARIA2_BIN,
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
        "--check-certificate=false",
        f"--dir={DOWNLOAD_DIR}",
        "--bt-stop-timeout=1200"
    ]
    subprocess.Popen(cmd)
    time.sleep(2) # Sync sleep to ensure startup

# --- CLIENT INITIALIZATION (THE FIX) ---
# Initialize Client INSIDE main to ensure loop is ready
app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- PROGRESS UPDATE ---
last_up_time = 0
async def upload_progress(current, total, message, start_time):
    global last_up_time
    now = time.time()
    if now - last_up_time < 5: return
    last_up_time = now

    percentage = (current * 100) / total
    speed = current / (now - start_time)
    eta = (total - current) / speed if speed > 0 else 0

    text = f"""Uploading: {percentage:.2f}%
{get_progress_bar_string(percentage)}
{get_readable_file_size(current)} of {get_readable_file_size(total)}
Speed: {get_readable_file_size(speed)}/sec
ETA: {get_readable_time(eta)}

Thanks for using this bot"""
    try: await message.edit(text)
    except: pass

# --- MONITORING LOGIC ---
async def download_monitor(aria2, gid, message):
    last_msg_time = 0
    status_msg = await message.reply("⬇️ **Initializing Download...**")
    
    while True:
        try:
            status = await aria2.tellStatus(gid)
            stat = status.get('status')

            # 1. MAGNET METADATA HANDLER
            if stat == 'complete' and 'followedBy' in status:
                gid = status['followedBy'][0]
                await status_msg.edit("🧲 **Metadata Downloaded. Fetching Files...**")
                await asyncio.sleep(1)
                continue

            # 2. ERROR
            if stat == 'error':
                err = status.get('errorMessage', 'Unknown Error')
                await status_msg.edit(f"❌ Error: {err}")
                return

            # 3. COMPLETION
            if stat == 'complete':
                await status_msg.edit("✅ **Download Complete. Uploading...**")
                files = await aria2.getFiles(gid)
                filepath = files[0]['path']
                
                if not os.path.exists(filepath):
                    await status_msg.edit("❌ Error: File missing.")
                    return

                start = time.time()
                try:
                    # Determine if Video or Document
                    ext = os.path.splitext(filepath)[1].lower()
                    if ext in ['.mp4', '.mkv', '.avi', '.mov']:
                        await app.send_video(
                            chat_id=message.chat.id,
                            video=filepath,
                            caption=f"**{os.path.basename(filepath)}**",
                            progress=upload_progress,
                            progress_args=(status_msg, start),
                            supports_streaming=True
                        )
                    else:
                        await app.send_document(
                            chat_id=message.chat.id,
                            document=filepath,
                            caption=f"**{os.path.basename(filepath)}**",
                            progress=upload_progress,
                            progress_args=(status_msg, start)
                        )
                    await status_msg.delete()
                    try: shutil.rmtree(DOWNLOAD_DIR)
                    except: pass
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                except Exception as e:
                    await status_msg.edit(f"❌ Upload Error: {e}")
                return

            # 4. ACTIVE DOWNLOAD
            if stat == 'active' or stat == 'waiting':
                if time.time() - last_msg_time < 4:
                    await asyncio.sleep(1)
                    continue

                done = int(status.get('completedLength', 0))
                total = int(status.get('totalLength', 1))
                speed = int(status.get('downloadSpeed', 0))
                
                percentage = (done / total) * 100 if total > 0 else 0
                eta = (total - done) / speed if speed > 0 else 0

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
            logger.error(f"Monitor Error: {e}")
            await asyncio.sleep(2)

# --- HANDLERS ---
@app.on_message(filters.command("start"))
async def start_h(c, m):
    await m.reply_text("**WZML-X Leech Bot Ready** 🚀\n\nSend any Magnet Link or Direct URL (Seedr/Gdrive etc).")

@app.on_message(filters.text | filters.document)
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
            gid = await aria2.addUri([link])
            asyncio.create_task(download_monitor(aria2, gid, m))
    except Exception as e:
        await m.reply(f"❌ **Aria2 Error:** {e}")

# --- MAIN EXECUTION ---
def main():
    # 1. Start Web Server (Daemon Thread)
    threading.Thread(target=run_web, daemon=True).start()
    
    # 2. Start Aria2 (Blocking Call before Loop)
    start_aria2()
    
    # 3. Install UVLoop and Start Bot
    uvloop.install()
    app.run()

if __name__ == "__main__":
    main()
