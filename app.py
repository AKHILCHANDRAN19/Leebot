import os
import asyncio
import logging
import threading
import subprocess
import time
import shutil
from urllib.parse import quote
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
import aioaria2

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))

ARIA2_BIN = "blitzfetcher" 
DOWNLOAD_DIR = "/app/downloads/"

# Best Trackers list (WZML Style) to speed up Magnets
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://coppersurfer.tk:6969/announce",
    "udp://tracker.zer0day.to:1337/announce"
]

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger("WZML-X")

# --- UTILS ---
def humanbytes(size):
    if not size: return "0B"
    for unit in ['', 'K', 'M', 'G', 'T']:
        if size < 1024: return f"{size:.2f} {unit}B"
        size /= 1024

def time_formatter(seconds):
    if not seconds or seconds < 0: return "0s"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{int(h)}h {int(m)}m {int(s)}s" if h else f"{int(m)}m {int(s)}s"

def get_progressbar(current, total):
    pct = (current / total) * 100 if total > 0 else 0
    p = min(max(pct, 0), 100)
    cFull = int(p // 10)
    return '▪' * cFull + '▫' * (10 - cFull)

# --- WEB SERVER ---
async def health_check(request): return web.Response(text="Running")
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
    cmd = [shutil.which(ARIA2_BIN) or "aria2c"]
    cmd.extend([
        "--enable-rpc", "--rpc-listen-all=false", "--rpc-listen-port=6800",
        "--max-connection-per-server=16", "--rpc-max-request-size=1024M",
        "--seed-time=0.01", "--min-split-size=10M", "--follow-torrent=mem",
        "--split=16", "--daemon=true", f"--dir={DOWNLOAD_DIR}",
        "--bt-stop-timeout=1200", "--bt-tracker=" + ",".join(TRACKERS)
    ])
    subprocess.Popen(cmd)
    await asyncio.sleep(2)

# --- BOT CLIENT ---
app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- DOWNLOAD LOGIC (WZML STYLE) ---
async def download_monitor(aria2, gid, message):
    last_msg = 0
    status_msg = await message.reply_text("🔄 **Processing Link...**")
    
    while True:
        try:
            status = await aria2.tellStatus(gid)
            stat = status.get('status')
            
            # 1. HANDLE MAGNET METADATA GID SWAP
            if 'followedBy' in status:
                new_gid = status['followedBy'][0]
                logger.info(f"Magnet detected. Switching GID {gid} -> {new_gid}")
                gid = new_gid
                await status_msg.edit_text("🧲 **Metadata Fetched. Starting Download...**")
                continue

            # 2. CALCULATE METRICS
            completed = int(status.get('completedLength', 0))
            total = int(status.get('totalLength', 1))
            speed = int(status.get('downloadSpeed', 0))
            seeds = status.get('numSeeders', 0)
            peers = status.get('connections', 0)
            
            # 3. FORMAT STATUS MESSAGE
            if stat == 'active':
                if time.time() - last_msg < 3: # Throttle updates
                    await asyncio.sleep(1)
                    continue
                
                percentage = (completed/total) * 100 if total else 0
                eta = (total - completed) / speed if speed > 0 else 0
                
                text = (
                    f"⬇️ **Downloading**: {percentage:.2f}%\n"
                    f"[{get_progressbar(completed, total)}]\n"
                    f"{humanbytes(completed)} of {humanbytes(total)}\n"
                    f"**Speed:** {humanbytes(speed)}/s | **ETA:** {time_formatter(eta)}\n"
                    f"**Seeds:** {seeds} | **Peers:** {peers}"
                )
                try:
                    await status_msg.edit_text(text)
                    last_msg = time.time()
                except: pass

            # 4. HANDLE COMPLETION
            elif stat == 'complete':
                await status_msg.edit_text("✅ **Download Complete. Uploading...**")
                files = await aria2.getFiles(gid)
                filepath = files[0]['path']
                
                # Upload Logic
                start = time.time()
                try:
                    await app.send_document(
                        chat_id=message.chat.id,
                        document=filepath,
                        caption=f"📂 **{os.path.basename(filepath)}**\n\n__Uploaded via WZML Logic__",
                        progress=upload_progress,
                        progress_args=(status_msg, start)
                    )
                    await status_msg.delete()
                    if os.path.exists(filepath): os.remove(filepath)
                except Exception as e:
                    await status_msg.edit_text(f"❌ **Upload Failed:** {e}")
                break
            
            elif stat == 'error':
                await status_msg.edit_text("❌ **Download Error**")
                break
                
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(e)
            break

# --- UPLOAD PROGRESS ---
last_up_time = 0
async def upload_progress(current, total, message, start):
    global last_up_time
    now = time.time()
    if now - last_up_time < 3: return
    last_up_time = now
    
    speed = current / (now - start)
    eta = (total - current) / speed if speed > 0 else 0
    pct = (current/total) * 100
    
    text = (
        f"⬆️ **Uploading**: {pct:.2f}%\n"
        f"[{get_progressbar(current, total)}]\n"
        f"{humanbytes(current)} of {humanbytes(total)}\n"
        f"**Speed:** {humanbytes(speed)}/s | **ETA:** {time_formatter(eta)}"
    )
    try: await message.edit_text(text)
    except: pass

# --- HANDLERS ---
@app.on_message(filters.command("start"))
async def start_h(c, m):
    await m.reply_text("⚡ **WZML-X Leech Bot is Ready!**\nSend a link/file.")

@app.on_message(filters.text | filters.document)
async def leech_h(c, m):
    link = None
    if m.text and m.text.startswith(("http", "magnet:")):
        link = m.text.strip()
        # Inject Trackers into Magnet
        if "magnet:" in link:
            for tr in TRACKERS:
                link += f"&tr={quote(tr)}"
    elif m.document and m.document.file_name.endswith(".torrent"):
        msg = await m.reply_text("📥 **Processing Torrent File...**")
        link = await m.download(file_name=f"{DOWNLOAD_DIR}temp.torrent")
        await msg.delete()

    if not link: return

    try:
        async with aioaria2.Aria2HttpClient("http://localhost:6800/jsonrpc") as aria2:
            # WZML-X always uses addUri for magnets, even file paths in some configs
            gid = await aria2.addUri([link])
            asyncio.create_task(download_monitor(aria2, gid, m))
    except Exception as e:
        await m.reply_text(f"❌ **Error:** {e}")

# --- MAIN ---
def main():
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.get_event_loop().run_until_complete(start_aria2())
    app.run()

if __name__ == "__main__":
    main()
