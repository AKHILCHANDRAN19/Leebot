import os
import time
import math
import shutil
import asyncio
import logging
import subprocess
import psutil
import aioaria2
import uvloop
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))

DOWNLOAD_DIR = "/app/downloads/"
ARIA2_BIN = "blitzfetcher"
START_TIME = time.time()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WZML-X")

# --- WZML EXACT FORMATTERS ---
SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

def get_readable_file_size(size_in_bytes):
    if size_in_bytes is None: return '0B'
    index = 0
    while size_in_bytes >= 1024:
        size_in_bytes /= 1024
        index += 1
    try:
        return f'{round(size_in_bytes, 2)}{SIZE_UNITS[index]}'
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
    pct = float(pct)
    p = min(max(pct, 0), 100)
    cFull = int(p // 8)
    p_str = '▤' * cFull
    p_str += '□' * (12 - cFull)
    return f"[{p_str}]"

def get_bot_stats():
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    try:
        disk = psutil.disk_usage(DOWNLOAD_DIR)
        free = get_readable_file_size(disk.free)
    except:
        free = "N/A"
    uptime = get_readable_time(time.time() - START_TIME)
    return f"""
⌬ Bot Stats
┠ CPU: {cpu}% | F: {free}
┠ RAM: {mem}% | UPTIME: {uptime}
┖ DL: 0B/s | UL: 0B/s"""

# --- WEB SERVER (For Render Health) ---
async def health_check(request): 
    return web.Response(text="WZML-X Alive")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web Server Running on Port {PORT}")

# --- ARIA2 ENGINE ---
def start_aria2():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    binary = shutil.which(ARIA2_BIN) or "aria2c"
    cmd = [
        binary, "--enable-rpc", "--rpc-listen-all=false", "--rpc-listen-port=6800",
        "--max-connection-per-server=16", "--rpc-max-request-size=1024M",
        "--seed-time=0.01", "--min-split-size=10M", "--follow-torrent=mem",
        "--split=16", "--daemon=true", "--allow-overwrite=true",
        f"--dir={DOWNLOAD_DIR}", "--bt-stop-timeout=1200", "--quiet=true"
    ]
    subprocess.Popen(cmd)

# --- LOGIC HANDLERS (Defined before Client Init) ---

# UPLOAD PROGRESS
last_up_time = 0
async def upload_progress(current, total, message, start_time, filename, user_info):
    global last_up_time
    now = time.time()
    if now - last_up_time < 5: return
    last_up_time = now

    pct = (current / total) * 100
    speed = current / (now - start_time)
    eta = (total - current) / speed if speed > 0 else 0
    elapsed = now - start_time

    text = f"""{filename}
┃ {get_progress_bar_string(pct)} {pct:.2f}%
┠ Processed: {get_readable_file_size(current)} of {get_readable_file_size(total)}
┠ Status: Uploading | ETA: {get_readable_time(eta)}
┠ Speed: {get_readable_file_size(speed)}/s | Elapsed: {get_readable_time(elapsed)}
┠ Mode:  #Leech | #Telegram
{user_info}
{get_bot_stats()}"""
    
    try: await message.edit(text)
    except: pass

# DOWNLOAD MONITOR (WZML LOGIC)
async def download_monitor(client, aria2, gid, message):
    last_msg = 0
    status_msg = await message.reply("⬇️ **Initializing...**")
    user_info = f"┠ User: {message.from_user.first_name} | ID: {message.from_user.id}"
    start_time = time.time()
    is_metadata = False

    while True:
        try:
            download = await aria2.tellStatus(gid)
            status = download.get('status')
            
            # --- MAGNET LOGIC ---
            if download.get('followedBy'):
                is_metadata = True
                gid = download['followedBy'][0]
                continue 
            
            if download.get('bittorrent') and not download.get('files'):
                 is_metadata = True
            else:
                 if download.get('files') and download['files'][0]['path'].startswith('[METADATA]'):
                     is_metadata = True
                 else:
                     is_metadata = False
            # --------------------

            name = download.get('bittorrent', {}).get('info', {}).get('name', download.get('files', [{}])[0].get('path', 'Unknown'))
            if is_metadata: name = f"[METADATA] {name}"
            name = os.path.basename(name)

            if status == 'active' or status == 'waiting':
                if time.time() - last_msg < 4:
                    await asyncio.sleep(1)
                    continue
                
                total = int(download.get('totalLength', 1))
                done = int(download.get('completedLength', 0))
                speed = int(download.get('downloadSpeed', 0))
                seeds = download.get('numSeeders', 0)
                peers = download.get('connections', 0)
                
                pct = (done/total) * 100 if total > 0 else 0
                eta = (total - done) / speed if speed > 0 else 0
                elapsed = time.time() - start_time

                msg = f"""{name}
┃ {get_progress_bar_string(pct)} {pct:.2f}%
┠ Processed: {get_readable_file_size(done)} of {get_readable_file_size(total)}
┠ Status: {'Downloading' if not is_metadata else 'Metadata'} | ETA: {get_readable_time(eta)}
┠ Speed: {get_readable_file_size(speed)}/s | Elapsed: {get_readable_time(elapsed)}
┠ Engine: Aria2 v1.36.0
┠ Mode:  #Leech | #Aria2
┠ Seeders: {seeds} | Leechers: {peers}
{user_info}
┖ /cancel_{gid}
{get_bot_stats()}"""
                
                try:
                    await status_msg.edit(msg)
                    last_msg = time.time()
                except: pass

            elif status == 'complete':
                await status_msg.edit("✅ **Download Complete. Extracting...**")
                files = await aria2.getFiles(gid)
                filepath = max(files, key=lambda x: int(x['length']))['path']

                if not os.path.exists(filepath):
                    await status_msg.edit("❌ File Error.")
                    return

                u_start = time.time()
                try:
                    await client.send_document(
                        chat_id=message.chat.id,
                        document=filepath,
                        caption=f"📂 **{os.path.basename(filepath)}**",
                        progress=upload_progress,
                        progress_args=(status_msg, u_start, os.path.basename(filepath), user_info)
                    )
                    await status_msg.delete()
                    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                except Exception as e:
                    await status_msg.edit(f"❌ Upload Error: {e}")
                return

            elif status == 'error':
                await status_msg.edit(f"❌ Error: {download.get('errorMessage')}")
                return

            await asyncio.sleep(2)
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(2)

# COMMAND HANDLERS
async def start_handler(client, message):
    await message.reply_text("**WZML-X Leech Ready** 🚀")

async def leech_handler(client, message):
    link = None
    if message.document and message.document.file_name.endswith(".torrent"):
        msg = await message.reply("📥 **Reading Torrent...**")
        link = await message.download(file_name=f"{DOWNLOAD_DIR}job.torrent")
        await msg.delete()
    elif message.text:
        link = message.text.strip()

    if not link: return

    try:
        async with aioaria2.Aria2HttpClient("http://localhost:6800/jsonrpc") as aria2:
            gid = await aria2.addUri([link])
            # Pass client to monitor
            asyncio.create_task(download_monitor(client, aria2, gid, message))
    except Exception as e:
        await message.reply(f"❌ Error: {e}")

# --- MAIN ENTRY POINT ---
async def main():
    # 1. Start Aria2
    start_aria2()
    await asyncio.sleep(3)

    # 2. Start Web Server (Concurrent)
    asyncio.create_task(start_web_server())

    # 3. Init Client INSIDE loop (Fixes RuntimeError)
    app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    
    # 4. Add Handlers
    app.add_handler(MessageHandler(start_handler, filters.command("start")))
    app.add_handler(MessageHandler(leech_handler, filters.text | filters.document))

    # 5. Start Bot
    logger.info("Bot Starting...")
    await app.start()
    await idle()
    await app.stop()

if __name__ == "__main__":
    # Fix for UVLoop Policy on Render
    uvloop.install()
    asyncio.run(main())
