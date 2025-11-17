#!/usr/bin/env python3
# Web Service LeechBot - Render Native Packages Edition
import os, re, asyncio, subprocess, logging, threading, sys, time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
import uvicorn
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait
from dotenv import load_dotenv

# === CONFIGURATION ===
load_dotenv()
API_ID = int(os.getenv("API_ID", "2819362"))
API_HASH = os.getenv("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0")
OWNER_ID = int(os.getenv("OWNER_ID", "6219290068"))
DUMP_CHANNEL_ID = int(os.getenv("DUMP_CHANNEL_ID", "-1003286196892"))

WORK_DIR = Path("bot_data")
WORK_DIR.mkdir(exist_ok=True, parents=True)
DOWNLOAD_DIR = WORK_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode='a')
    ]
)
logger = logging.getLogger(__name__)

# === PYROGRAM CLIENT ===
bot = Client("leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=str(WORK_DIR))

# === STATE ===
class DownloadTask:
    def __init__(self, task_id: str, url: str, status_msg: Optional[Message] = None):
        self.task_id = task_id
        self.url = url
        self.status_msg = status_msg
        self.start_time = datetime.now()
        self.download_start = None

tasks: Dict[str, DownloadTask] = {}

# === UTILITIES ===
def is_url_valid(url: str) -> bool:
    return bool(re.match(r'^(https?|ftp)://|^magnet:\?xt=urn:', url))

def is_torrent(url: str) -> bool:
    return url.startswith('magnet:?xt=urn:btih:')

def human_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def create_progress_bar(percentage: float, width: int = 10) -> str:
    filled = int(percentage / 100 * width)
    return "▪" * filled + "▫" * (width - filled)

async def detailed_progress(current: int, total: int, start_time: datetime, action: str) -> str:
    elapsed = (datetime.now() - start_time).total_seconds()
    speed = current / elapsed if elapsed > 0 else 0
    percentage = (current / total) * 100
    eta = (total - current) / speed if speed > 0 else 0
    
    minutes, seconds = divmod(int(eta), 60)
    eta_str = f"{minutes}m, {seconds}s" if minutes > 0 else f"{seconds}s"
    
    bar = create_progress_bar(percentage)
    
    return (
        f"{action}: {percentage:.2f}%\n"
        f"[{bar}]\n"
        f"{human_size(current)} of {human_size(total)}\n"
        f"Speed: {human_size(speed)}/sec\n"
        f"ETA: {eta_str}"
    )

# === DOWNLOAD MANAGERS ===
class AriaManager:
    def __init__(self):
        self.config_path = WORK_DIR / "aria2.conf"
        self._setup_config()
    
    def _setup_config(self):
        config = f"""dir={DOWNLOAD_DIR}
max-concurrent-downloads=3
max-connection-per-server=8
split=8
bt-max-peers=50
seed-ratio=0
seed-time=0
enable-dht=true
allow-overwrite=true
log-level=error
"""
        self.config_path.write_text(config)
    
    async def download(self, url: str, task_id: str, task: DownloadTask) -> Optional[Path]:
        cmd = ["aria2c", f"--conf-path={self.config_path}", url]
        logger.info(f"🚀 Executing: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        task.download_start = datetime.now()
        
        async def parse_progress():
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line = line.decode().strip()
                if '[DL:' in line and '%)' in line:
                    try:
                        match = re.search(r'\[DL:([^\)]+)\((\d+)%\)', line)
                        if match:
                            size_part = match.group(1)
                            percent = int(match.group(2))
                            downloaded_str = size_part.split('/')[0]
                            downloaded = self._parse_size(downloaded_str)
                            total = (downloaded * 100 / percent) if percent > 0 else downloaded
                            progress_text = await detailed_progress(int(downloaded), int(total), task.download_start, "📥 Downloading")
                            try:
                                await task.status_msg.edit_text(progress_text)
                            except:
                                pass
                    except Exception as e:
                        logger.debug(f"Parse error: {e}")
        
        progress_task = asyncio.create_task(parse_progress())
        await process.wait()
        progress_task.cancel()
        
        if process.returncode == 0:
            files = list(DOWNLOAD_DIR.glob('*'))
            if files:
                return max(files, key=lambda f: f.stat().st_mtime)
        
        stderr = await process.stderr.read()
        logger.error(f"❌ aria2c failed: {stderr.decode()}")
        return None
    
    def _parse_size(self, size_str: str) -> float:
        size_str = size_str.strip()
        if 'GiB' in size_str:
            return float(size_str.replace('GiB', '')) * 1024**3
        elif 'MiB' in size_str:
            return float(size_str.replace('MiB', '')) * 1024**2
        elif 'KiB' in size_str:
            return float(size_str.replace('KiB', '')) * 1024
        else:
            return float(size_str)

class YTDLManager:
    async def download(self, url: str, task_id: str, task: DownloadTask) -> Optional[Path]:
        output_template = str(DOWNLOAD_DIR / '%(title)s.%(ext)s')
        cmd = ["yt-dlp", url, "-o", output_template, "--quiet"]
        logger.info(f"🎬 Executing: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            files = list(DOWNLOAD_DIR.glob('*'))
            if files:
                return max(files, key=lambda f: f.stat().st_mtime)
        
        logger.error(f"❌ yt-dlp failed: {stderr.decode()}")
        return None

class UploadManager:
    async def upload(self, file_path: Path, chat_id: int, task_id: str, status_msg: Message, task: DownloadTask):
        try:
            if not file_path.exists():
                logger.error(f"❌ File not found: {file_path}")
                await status_msg.edit_text("❌ File not found after download!")
                return

            file_size = file_path.stat().st_size
            if file_size > MAX_FILE_SIZE:
                await self._split_upload(file_path, chat_id, task_id, status_msg, task)
                return
            
            upload_start = datetime.now()
            progress_text = await detailed_progress(0, file_size, upload_start, "📤 Uploading")
            await status_msg.edit_text(progress_text)
            
            async def progress(current: int, total: int):
                try:
                    if (datetime.now().second % 4) == 0:
                        progress_text = await detailed_progress(current, total, upload_start, "📤 Uploading")
                        asyncio.create_task(status_msg.edit_text(progress_text))
                except Exception as e:
                    logger.debug(f"Progress error: {e}")
            
            ext = file_path.suffix.lower()
            caption = f"<code>{file_path.name}</code>"
            
            if ext in ['.mp4', '.mkv', '.avi', '.mov']:
                sent_msg = await bot.send_video(chat_id, str(file_path), caption=caption, progress=progress)
            else:
                sent_msg = await bot.send_document(chat_id, str(file_path), caption=caption, progress=progress)
            
            if sent_msg and sent_msg.id:
                logger.info(f"✅ Upload SUCCESS: {file_path.name}")
                await status_msg.edit_text("✅ Upload completed successfully!")
            else:
                logger.error("❌ Upload returned None")
                await status_msg.edit_text("❌ Upload failed!")
                
        except FloodWait as e:
            logger.warning(f"⏳ FloodWait: {e.value}s")
            await asyncio.sleep(e.value + 5)
            await self.upload(file_path, chat_id, task_id, status_msg, task)
        except Exception as e:
            logger.error(f"❌ Upload error: {e}")
            await status_msg.edit_text(f"❌ Upload failed: {str(e)}")

    async def _split_upload(self, file_path: Path, chat_id: int, task_id: str, status_msg: Message, task: DownloadTask):
        split_dir = DOWNLOAD_DIR / "splits"
        split_dir.mkdir(exist_ok=True, parents=True)
        info_msg = await status_msg.reply_text(f"📦 File too large! Splitting...")
        
        cmd = f"split -b {MAX_FILE_SIZE} '{file_path}' '{split_dir}/{file_path.name}.part'"
        subprocess.run(cmd, shell=True, check=True)
        
        parts = sorted(split_dir.glob(f"{file_path.name}.part*"))
        for i, part in enumerate(parts, 1):
            await info_msg.edit_text(f"📤 Uploading part {i}/{len(parts)}")
            await self.upload(part, chat_id, task_id, status_msg, task)
            part.unlink()
        
        await info_msg.delete()

# === WEB SERVER ===
def run_web_server():
    try:
        web_app = FastAPI()
        
        @web_app.get("/", response_class=PlainTextResponse)
        async def root():
            return "LeechBot Pro Web Service is running"
        
        @web_app.get("/health", response_class=PlainTextResponse)
        async def health():
            return "OK"
        
        port = int(os.environ.get("PORT", 10000))
        uvicorn.run(web_app, host="0.0.0.0", port=port, log_level="info", access_log=False)
    except Exception as e:
        logger.error(f"❌ Web server error: {e}")
        sys.exit(1)

# === BOT HANDLERS ===
@bot.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    await message.reply_text(
        "🦞 **LeechBot Pro** (Web Service Mode)\n\n"
        "Send /leech <link> to download\n"
        "Supports: torrents, magnets, YouTube, direct links",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Help", callback_data="help")]])
    )

@bot.on_message(filters.command("leech") & filters.private)
async def leech_command(client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/leech <download_link>`")
        return
    
    url = message.text.split(maxsplit=1)[1].strip()
    logger.info(f"🔗 URL: {url[:100]}...")
    
    if not is_url_valid(url):
        await message.reply_text("❌ Invalid URL format!")
        return
    
    task_id = f"{message.from_user.id}_{int(asyncio.get_event_loop().time())}"
    status_msg = await message.reply_text("🚀 Starting download...")
    
    task = DownloadTask(task_id, url, status_msg)
    tasks[task_id] = task
    
    asyncio.create_task(process_download(task))

async def process_download(task: DownloadTask):
    try:
        logger.info(f"🎯 Processing: {task.task_id}")
        
        if is_torrent(task.url):
            downloader = AriaManager()
        else:
            downloader = YTDLManager()
        
        file_path = await downloader.download(task.url, task.task_id, task)
        
        if not file_path or not file_path.exists():
            await task.status_msg.edit_text("❌ Download failed!")
            return
        
        file_size = file_path.stat().st_size
        await task.status_msg.edit_text(f"✅ Downloaded: {file_path.name}\n📊 Size: {human_size(file_size)}")
        
        uploader = UploadManager()
        await uploader.upload(file_path, DUMP_CHANNEL_ID, task.task_id, task.status_msg, task)
        
        if file_path.exists():
            if file_path.is_file():
                file_path.unlink()
            else:
                import shutil
                shutil.rmtree(file_path)
        
        await task.status_msg.edit_text("✅ Task completed successfully!")
        
    except Exception as e:
        logger.error(f"❌ Failed: {e}")
        await task.status_msg.edit_text(f"❌ Error: {str(e)}")
    finally:
        tasks.pop(task.task_id, None)

# === MAIN ===
if __name__ == "__main__":
    logger.info("="*60)
    logger.info("🔰 LEECHBOT PRO STARTING")
    logger.info("="*60)
    
    if DUMP_CHANNEL_ID == 0:
        logger.error("❌ DUMP_CHANNEL_ID not set!")
        sys.exit(1)
    
    logger.info(f"📡 Channel: {DUMP_CHANNEL_ID}")
    logger.info(f"👤 Owner: {OWNER_ID}")
    
    # ✅ Verify aria2 is available
    try:
        result = subprocess.run(['aria2c', '--version'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            logger.info(f"✅ aria2c verified")
        else:
            logger.error("❌ aria2c verification failed!")
            logger.error("Manual fix: Connect via SSH and run 'sudo apt-get install aria2'")
            sys.exit(1)
    except Exception as e:
        logger.error(f"❌ aria2c not found: {e}")
        logger.error("This means Render's nativePackages didn't work. Use SSH to install.")
        sys.exit(1)
    
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    time.sleep(2)
    
    logger.info("🤖 Bot running...")
    bot.run()
