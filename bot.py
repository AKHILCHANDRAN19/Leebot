#!/usr/bin/env python3
# Single-File LeechBot Pro for Render Web Service (Python 3.13+)
import os, re, asyncio, subprocess, logging, sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
import uvicorn
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait
from dotenv import load_dotenv

# === CONFIGURATION ===
load_dotenv()
API_ID = int(os.getenv("API_ID", "2819362"))
API_HASH = os.getenv("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DUMP_CHANNEL_ID = int(os.getenv("DUMP_CHANNEL_ID", "0"))

WORK_DIR = Path("bot_data")
WORK_DIR.mkdir(exist_ok=True, parents=True)
DOWNLOAD_DIR = WORK_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === PYROGRAM CLIENT ===
bot = Client("leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=str(WORK_DIR))

# === STATE MANAGEMENT ===
class DownloadTask:
    def __init__(self, task_id: str, url: str, status_msg: Optional[Message] = None):
        self.task_id = task_id
        self.url = url
        self.status_msg = status_msg
        self.start_time = datetime.now()
        self.file_path: Optional[Path] = None

tasks: Dict[str, DownloadTask] = {}

# === UTILITIES ===
def is_url_valid(url: str) -> bool:
    return bool(re.match(r'^(https?|ftp|magnet):\/\/', url))

def is_torrent(url: str) -> bool:
    return url.startswith('magnet:?xt=urn:btih:')

def human_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

# === DOWNLOAD MANAGERS ===
class AriaManager:
    def __init__(self):
        self.config_path = WORK_DIR / "aria2.conf"
        self._setup_config()
    
    def _setup_config(self):
        config = f"""dir={DOWNLOAD_DIR}
max-concurrent-downloads=10
max-connection-per-server=16
split=16
bt-max-peers=100
seed-ratio=0
seed-time=0
enable-dht=true
allow-overwrite=true
"""
        self.config_path.write_text(config)
    
    async def download(self, url: str, task_id: str) -> Optional[Path]:
        cmd = ["aria2c", f"--conf-path={self.config_path}", "--quiet", url]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await process.wait()
        
        if process.returncode == 0:
            files = list(DOWNLOAD_DIR.glob('*'))
            return max(files, key=lambda f: f.stat().st_mtime) if files else None
        return None

class YTDLManager:
    async def download(self, url: str, task_id: str) -> Optional[Path]:
        cmd = ["yt-dlp", url, "-o", str(DOWNLOAD_DIR / '%(title)s.%(ext)s'), "--quiet"]
        process = await asyncio.create_subprocess_exec(*cmd)
        await process.wait()
        
        if process.returncode == 0:
            files = list(DOWNLOAD_DIR.glob('*'))
            return max(files, key=lambda f: f.stat().st_mtime) if files else None
        return None

class UploadManager:
    async def upload(self, file_path: Path, chat_id: int, task_id: str):
        try:
            if file_path.stat().st_size > MAX_FILE_SIZE:
                await self._split_upload(file_path, chat_id, task_id)
                return
            
            status_msg = await bot.send_message(chat_id, "📤 Uploading...")
            
            async def progress(current: int, total: int):
                try:
                    percent = (current / total) * 100
                    await status_msg.edit_text(f"📤 UPLOADING: {percent:.1f}% | {human_size(current)} / {human_size(total)}")
                except:
                    pass
            
            ext = file_path.suffix.lower()
            if ext in ['.mp4', '.mkv', '.avi', '.mov']:
                await bot.send_video(chat_id, str(file_path), caption=file_path.name, progress=progress)
            else:
                await bot.send_document(chat_id, str(file_path), caption=file_path.name, progress=progress)
            
            await status_msg.delete()
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await self.upload(file_path, chat_id, task_id)
        except Exception as e:
            logger.error(f"Upload error: {e}")
    
    async def _split_upload(self, file_path: Path, chat_id: int, task_id: str):
        split_dir = DOWNLOAD_DIR / "splits"
        split_dir.mkdir(exist_ok=True, parents=True)
        cmd = f"split -b {MAX_FILE_SIZE} '{file_path}' '{split_dir}/{file_path.name}.part'"
        subprocess.run(cmd, shell=True, check=True)
        for part in sorted(split_dir.glob(f"{file_path.name}.part*")):
            await self.upload(part, chat_id, task_id)
            part.unlink()

# === FASTAPI WEB SERVER ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.start()
    logger.info("Bot started in background")
    yield
    await bot.stop()
    logger.info("Bot stopped")

web_app = FastAPI(lifespan=lifespan)

@web_app.get("/", response_class=PlainTextResponse)
async def root():
    return "LeechBot Pro Web Service is running"

@web_app.get("/health", response_class=PlainTextResponse)
async def health():
    return "OK"

@web_app.post("/leech")
async def leech_api(url: str, user_id: int = None):
    if not is_url_valid(url):
        raise HTTPException(status_code=400, detail="Invalid URL")
    task_id = f"api_{asyncio.get_event_loop().time()}"
    task = DownloadTask(task_id, url)
    tasks[task_id] = task
    asyncio.create_task(process_download(task))
    return JSONResponse({"status": "started", "task_id": task_id})

# === BOT COMMAND HANDLERS ===
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
    
    url = message.command[1]
    task_id = f"{message.from_user.id}_{asyncio.get_event_loop().time()}"
    status_msg = await message.reply_text("🚀 Starting download...")
    
    task = DownloadTask(task_id, url, status_msg)
    tasks[task_id] = task
    asyncio.create_task(process_download(task))

# === MAIN PROCESSOR ===
async def process_download(task: DownloadTask):
    try:
        downloader = AriaManager() if is_torrent(task.url) else YTDLManager()
        file_path = await downloader.download(task.url, task.task_id)
        
        if not file_path or not file_path.exists():
            await task.status_msg.edit_text("❌ Download failed!")
            return
        
        await task.status_msg.edit_text("✅ Download complete! Uploading...")
        
        uploader = UploadManager()
        await uploader.upload(file_path, DUMP_CHANNEL_ID, task.task_id)
        
        if file_path.is_file():
            file_path.unlink()
        else:
            import shutil
            shutil.rmtree(file_path)
        
        await task.status_msg.edit_text("✅ Task completed!")
    except Exception as e:
        logger.error(f"Process error: {e}")
        await task.status_msg.edit_text(f"❌ Error: {str(e)}")
    finally:
        tasks.pop(task.task_id, None)

# === ENTRY POINT ===
if __name__ == "__main__":
    # Render provides PORT env var
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Starting LeechBot Pro on port {port}")
    
    # Run FastAPI + bot together
    uvicorn.run(web_app, host="0.0.0.0", port=port, log_level="info")
