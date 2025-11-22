import os
import time
import shutil
import asyncio
import logging
import subprocess
import psutil
import aioaria2
import aioqbt
import uvloop
import magic
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler

# --- CONFIG ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))
DOWNLOAD_DIR = "/app/downloads/"
ARIA2_BIN = "blitzfetcher"
START_TIME = time.time()

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WZML-X")

# --- FORMATTERS ---
SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
def get_readable_file_size(size_in_bytes):
    if size_in_bytes is None: return '0B'
    index = 0
    while size_in_bytes >= 1024:
        size_in_bytes /= 1024
        index += 1
    try: return f'{round(size_in_bytes, 2)}{SIZE_UNITS[index]}'
    except: return '0B'

def get_readable_time(seconds):
    if not seconds: return "0s"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{int(h)}h {int(m)}m {int(s)}s" if h else f"{int(m)}m {int(s)}s"

def get_progress_bar_string(pct):
    p = min(max(float(pct), 0), 100)
    cFull = int(p // 8.33)
    return f"[{'▤' * cFull}{'□' * (12 - cFull)}]"

def get_bot_stats():
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    try:
        disk = psutil.disk_usage(DOWNLOAD_DIR)
        free = get_readable_file_size(disk.free)
    except: free = "N/A"
    uptime = get_readable_time(time.time() - START_TIME)
    return f"\n⌬ Bot Stats\n┠ CPU: {cpu}% | F: {free}\n┠ RAM: {mem}% | UPTIME: {uptime}\n┖ DL: 0B/s | UL: 0B/s"

# --- WEB SERVER ---
async def health_check(request): return web.Response(text="WZML-X Active")
async def start_web():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web Server running on {PORT}")

# --- ENGINES ---
def start_engines():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    # Aria2
    binary = shutil.which(ARIA2_BIN) or "aria2c"
    cmd = [
        binary, "--enable-rpc", "--rpc-listen-all=false", "--rpc-listen-port=6800",
        "--max-connection-per-server=16", "--seed-time=0.01", "--daemon=true",
        f"--dir={DOWNLOAD_DIR}", "--bt-stop-timeout=1200", "--quiet=true"
    ]
    subprocess.Popen(cmd)
    # qBittorrent
    if not os.path.exists("/root/.config/qBittorrent/"):
        os.makedirs("/root/.config/qBittorrent/")
    subprocess.Popen(["qbittorrent-nox", "--webui-port=8090", "-d"])

# --- UPLOAD ---
last_up = 0
async def upload_progress(current, total, message, start):
    global last_up
    now = time.time()
    if now - last_up < 4: return
    last_up = now
    pct = (current/total)*100
    speed = current/(now-start)
    eta = (total-current)/speed if speed>0 else 0
    
    msg = f"Uploading: {pct:.2f}%\n{get_progress_bar_string(pct)}\n{get_readable_file_size(current)} of {get_readable_file_size(total)}\nSpeed: {get_readable_file_size(speed)}/s | ETA: {get_readable_time(eta)}{get_bot_stats()}"
    try: await message.edit(msg)
    except: pass

async def upload_handler(client, message, path):
    if not os.path.exists(path): return
    start = time.time()
    try:
        fname = os.path.basename(path)
        mime = magic.from_file(path, mime=True)
        if "video" in mime:
             await client.send_video(
                chat_id=message.chat.id, video=path,
                caption=f"<code>{fname}</code>\n\n{get_bot_stats()}",
                supports_streaming=True,
                progress=upload_progress, progress_args=(message, start)
            )
        else:
            await client.send_document(
                chat_id=message.chat.id, document=path,
                caption=f"<code>{fname}</code>\n\n{get_bot_stats()}",
                progress=upload_progress, progress_args=(message, start)
            )
        await message.delete()
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    except Exception as e:
        await message.edit(f"❌ Upload Error: {e}")

# --- MONITORS ---
async def aria2_monitor(client, aria2, gid, message):
    last_msg = 0
    # CHANGED: Initial message is a placeholder, immediately updated by loop
    status_msg = await message.reply("⬇️ **Task Added**") 
    start_time = time.time()

    while True:
        try:
            down = await aria2.tellStatus(gid)
            stat = down.get('status')

            if down.get('followedBy'):
                gid = down['followedBy'][0]
                continue

            if stat == 'complete':
                await status_msg.edit("✅ **Download Complete.**")
                files = await aria2.getFiles(gid)
                filepath = max(files, key=lambda x: int(x['length']))['path']
                await upload_handler(client, status_msg, filepath)
                return

            if stat == 'active':
                if time.time() - last_msg > 4:
                    total = int(down.get('totalLength', 1))
                    done = int(down.get('completedLength', 0))
                    speed = int(down.get('downloadSpeed', 0))
                    pct = (done/total)*100 if total else 0
                    eta = (total-done)/speed if speed>0 else 0
                    elapsed = time.time() - start_time
                    
                    msg = f"Aria2 Downloading: {pct:.2f}%\n{get_progress_bar_string(pct)}\n{get_readable_file_size(done)} of {get_readable_file_size(total)}\nSpeed: {get_readable_file_size(speed)}/s | ETA: {get_readable_time(eta)} | Elapsed: {get_readable_time(elapsed)}{get_bot_stats()}"
                    try: await status_msg.edit(msg)
                    except: pass
                    last_msg = time.time()
            
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(e)
            break

async def qbit_monitor(client, qb, thash, message):
    last_msg = 0
    # CHANGED: Initial message is a placeholder, immediately updated by loop
    status_msg = await message.reply("⬇️ **Task Added**")
    start_time = time.time()

    while True:
        try:
            tor = (await qb.torrents.info(hashes=[thash]))[0]
            state = tor.state

            # CHANGED: Handle metaDL (Metadata) separately to show status
            if state == 'metaDL':
                 if time.time() - last_msg > 4:
                    await status_msg.edit("🧲 **Metadata Fetching...**")
                    last_msg = time.time()

            elif state in ['uploading', 'stalledUP', 'queuedUP', 'completed', 'pausedUP']:
                await status_msg.edit("✅ **qBit: Download Complete.**")
                await qb.torrents.pause(hashes=[thash])
                path = tor.content_path
                if os.path.isdir(path):
                    largest = ""
                    max_size = 0
                    for r, d, f in os.walk(path):
                        for file in f:
                            fp = os.path.join(r, file)
                            s = os.path.getsize(fp)
                            if s > max_size:
                                max_size = s
                                largest = fp
                    path = largest
                
                await upload_handler(client, status_msg, path)
                await qb.torrents.delete(hashes=[thash], delete_files=True)
                return

            elif state in ['downloading', 'stalledDL']:
                if time.time() - last_msg > 4:
                    pct = tor.progress * 100
                    elapsed = time.time() - start_time
                    msg = f"qBit Downloading: {pct:.2f}%\n{get_progress_bar_string(pct)}\n{get_readable_file_size(tor.downloaded)} of {get_readable_file_size(tor.total_size)}\nSpeed: {get_readable_file_size(tor.dlspeed)}/s | ETA: {get_readable_time(tor.eta)} | Elapsed: {get_readable_time(elapsed)}{get_bot_stats()}"
                    try: await status_msg.edit(msg)
                    except: pass
                    last_msg = time.time()

            await asyncio.sleep(2)
        except Exception as e:
            logger.error(e)
            break

# --- HANDLERS ---
async def cmd_handler(client, message):
    cmd = message.command[0]
    link = None
    if len(message.command) > 1:
        link = message.command[1]
    elif message.reply_to_message:
        link = message.reply_to_message.text or ""
        if not link and message.reply_to_message.document:
            link = await message.reply_to_message.download(f"{DOWNLOAD_DIR}job.torrent")

    if not link:
        await message.reply("Link/Torrent Missing.")
        return

    if cmd == "leech":
        try:
            async with aioaria2.Aria2HttpClient("http://localhost:6800/jsonrpc") as aria2:
                gid = await aria2.addUri([link])
                asyncio.create_task(aria2_monitor(client, aria2, gid, message))
        except Exception as e:
            await message.reply(f"Aria2 Error: {e}")

    elif cmd == "qbleech":
        try:
            async with aioqbt.create_client("http://localhost:8090") as qb:
                await qb.auth.log_in("admin", "adminadmin")
                await qb.torrents.add(urls=link, save_path=DOWNLOAD_DIR)
                await asyncio.sleep(1)
                torrents = await qb.torrents.info(sort='added_on', reverse=True)
                if torrents:
                    asyncio.create_task(qbit_monitor(client, qb, torrents[0].hash, message))
        except Exception as e:
            await message.reply(f"qBit Error: {e}")

async def start(c, m): await m.reply("Bot Active")

# --- MAIN ---
async def main():
    start_engines()
    await asyncio.sleep(4) 
    asyncio.create_task(start_web())

    app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    app.add_handler(MessageHandler(start, filters.command("start")))
    app.add_handler(MessageHandler(cmd_handler, filters.command(["leech", "qbleech"])))

    logger.info("Bot Started")
    await app.start()
    await idle()
    await app.stop()

if __name__ == "__main__":
    uvloop.install()
    asyncio.run(main())
