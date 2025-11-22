import os
import asyncio
import logging
import subprocess
import shutil
import time
import uvloop
from aiohttp import web
from pyrogram import Client, filters, idle
import aioaria2

# --- INSTALL UVLOOP FIRST ---
uvloop.install()

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "2819362"))
API_HASH = os.environ.get("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8390475015:AAF8dauJYTWFwktTQABzG17_-JTN4r71R3M")
PORT = int(os.environ.get("PORT", 10000))

DOWNLOAD_DIR = "/app/downloads/"
ARIA2_BIN = "blitzfetcher" # WZML Binary

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("WZML-X")

# --- FORMATTING HELPERS ---
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
    p_str = '▪' * cFull
    p_str += '▫' * (10 - cFull)
    return f"[{p_str}]"

# --- BOT CLIENT ---
app = Client("wzml_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- WEB SERVER LOGIC ---
async def health_check(request):
    return web.Response(text="WZML-X Active")

async def start_web_server():
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web Server Running on Port {PORT}")

# --- ARIA2 LOGIC ---
def start_aria2_daemon():
    if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
    binary = shutil.which(ARIA2_BIN) or "aria2c"
    cmd = [
        binary,
        "--enable-rpc", "--rpc-listen-all=false", "--rpc-listen-port=6800",
        "--max-connection-per-server=16", "--rpc-max-request-size=1024M",
        "--seed-time=0.01", "--min-split-size=10M", "--follow-torrent=mem",
        "--split=16", "--daemon=true", "--allow-overwrite=true",
        f"--dir={DOWNLOAD_DIR}", "--bt-stop-timeout=1200"
    ]
    subprocess.Popen(cmd)
    # No asyncio.sleep here, handled in main loop

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

    text = f"""Uploading: {percentage:.2f}%
{get_progress_bar_string(percentage)}
{get_readable_file_size(current)} of {get_readable_file_size(total)}
Speed: {get_readable_file_size(speed)}/sec
ETA: {get_readable_time(eta)}

Thanks for using this bot"""
    try: await message.edit(text)
    except: pass

# --- DOWNLOAD MONITOR ---
async def download_monitor(aria2, gid, message):
    last_msg_time = 0
    status_msg = await message.reply("⬇️ **Initializing Download...**")
    
    while True:
        try:
            status = await aria2.tellStatus(gid)
            stat = status.get('status')

            # MAGNET METADATA FIX
            if stat == 'complete' and 'followedBy' in status:
                gid = status['followedBy'][0]
                await status_msg.edit("🧲 **Metadata Fetched. Downloading...**")
                await asyncio.sleep(1)
                continue

            if stat == 'error':
                await status_msg.edit(f"❌ Error: {status.get('errorMessage')}")
                return

            if stat == 'complete':
                await status_msg.edit("✅ **Download Complete. Uploading...**")
                files = await aria2.getFiles(gid)
                filepath = files[0]['path']
                
                # Find largest file if multiple
                if len(files) > 1:
                    filepath = max(files, key=lambda x: int(x['length']))['path']

                if not os.path.exists(filepath):
                    await status_msg.edit("❌ File missing.")
                    return

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
                    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                except Exception as e:
                    await status_msg.edit(f"❌ Upload Failed: {e}")
                return

            # PROGRESS BAR
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
            print(f"Monitor Error: {e}")
            await asyncio.sleep(2)

# --- HANDLERS ---
@app.on_message(filters.command("start"))
async def start_h(c, m):
    await m.reply_text("**WZML-X Leech Ready** 🚀")

@app.on_message(filters.text | filters.document)
async def leech_h(c, m):
    link = None
    if m.document and m.document.file_name.endswith(".torrent"):
        msg = await m.reply("📥 **Reading Torrent...**")
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
        await m.reply(f"❌ Error: {e}")

# --- MAIN ASYNC ENTRY POINT ---
async def main():
    # 1. Start Aria2 Daemon (Subprocess)
    start_aria2_daemon()
    await asyncio.sleep(3) # Wait for RPC to be ready
    
    # 2. Start Web Server (For Render Health Check)
    await start_web_server()
    
    # 3. Start Pyrogram Client
    logger.info("Starting Bot...")
    await app.start()
    
    # 4. Idle to keep script running
    await idle()
    await app.stop()

if __name__ == "__main__":
    # THIS IS THE FIX: We use asyncio.run() which creates the loop correctly for uvloop
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
