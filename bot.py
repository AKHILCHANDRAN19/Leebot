#!/usr/bin/env python3
# Python 3.13+ Compatible LeechBot Pro
import os
import re
import json
import asyncio
import subprocess
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Configuration
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DUMP_CHANNEL_ID = int(os.getenv("DUMP_CHANNEL_ID", "0"))

# FIXED: Use relative path instead of /app
WORK_DIR = Path("bot_data")  # Creates in current working directory (/opt/render/project/src)
WORK_DIR.mkdir(exist_ok=True, parents=True)  # parents=True creates parent dirs if needed
DOWNLOAD_DIR = WORK_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# State management
class DownloadTask:
    def __init__(self, task_id: str, url: str, status_msg: Message):
        self.task_id = task_id
        self.url = url
        self.status_msg = status_msg
        self.start_time = datetime.now()
        self.downloaded_path: Optional[Path] = None

tasks: Dict[str, DownloadTask] = {}

# Pyrogram client
app = Client("leech_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=str(WORK_DIR))

def is_url_valid(url: str) -> bool:
    """Validate URL"""
    return bool(re.match(r'^(https?|ftp|magnet):\/\/', url))

def is_torrent(url: str) -> bool:
    return url.startswith('magnet:?xt=urn:btih:')

def is_ytdl_supported(url: str) -> bool:
    return any(x in url for x in ['youtube.com', 'youtu.be', 'instagram.com', 'facebook.com', 'twitter.com', 'x.com'])

def human_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def human_time(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}h {(seconds%3600)//60}m"

class AriaManager:
    """Python 3.13 compatible aria2c wrapper using asyncio subprocess"""
    
    def __init__(self):
        self.config_path = WORK_DIR / "aria2.conf"
        self.session_path = WORK_DIR / "aria2.session"
        self._setup_config()
    
    def _setup_config(self):
        """Create aria2c config file"""
        config = f"""# Aria2 Config for Python 3.13
dir={DOWNLOAD_DIR}
input-file={self.session_path}
save-session={self.session_path}
save-session-interval=30
continue=true
max-concurrent-downloads=10
max-connection-per-server=16
split=16
min-split-size=10M
max-overall-download-limit=0
max-upload-limit=0
seed-ratio=0
seed-time=0
bt-enable-hook-after-hash-check=true
bt-max-peers=100
bt-request-peer-speed-limit=50M
enable-dht=true
enable-dht6=true
enable-peer-exchange=true
dht-file-path={WORK_DIR}/dht.dat
dht-file-path6={WORK_DIR}/dht6.dat
allow-overwrite=true
auto-file-renaming=true
max-resume-failure-tries=0
"""
        self.config_path.write_text(config)
    
    async def download(self, url: str, task_id: str) -> Optional[Path]:
        """Download using aria2c with async subprocess"""
        try:
            cmd = [
                "aria2c",
                f"--conf-path={self.config_path}",
                "--console-log-level=warn",
                url
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Monitor progress
            asyncio.create_task(self._monitor_progress(process, task_id))
            
            await process.wait()
            
            if process.returncode == 0:
                # Find downloaded file
                files = list(DOWNLOAD_DIR.glob('*'))
                if files:
                    return max(files, key=lambda f: f.stat().st_mtime)  # Return newest file
            
            return None
            
        except Exception as e:
            logger.error(f"Aria2 download failed: {e}")
            return None
    
    async def _monitor_progress(self, process: asyncio.subprocess.Process, task_id: str):
        """Monitor aria2c output for progress"""
        if process.stderr is None:
            return
        
        async for line in process.stderr:
            line = line.decode().strip()
            if "ETA:" in line and task_id in tasks:
                try:
                    await tasks[task_id].status_msg.edit_text(
                        f"📥 **DOWNLOADING**\n\n{line[:200]}"  # Prevent message too long
                    )
                except:
                    pass

class YTDLManager:
    """yt-dlp download manager"""
    
    async def download(self, url: str, task_id: str) -> Optional[Path]:
        try:
            ydl_opts = {
                'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
                'format': 'best',
                'noplaylist': True,
                'progress_hooks': [self._hook],
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return Path(ydl.prepare_filename(info))
        except Exception as e:
            logger.error(f"YTDL error: {e}")
            return None
    
    def _hook(self, d: Dict[str, Any]):
        if d['status'] == 'downloading':
            # Progress is handled by yt-dlp's native output
            pass

class UploadManager:
    """Handle Telegram uploads with splitting"""
    
    async def upload(self, file_path: Path, chat_id: int, task_id: str):
        try:
            size = file_path.stat().st_size
            
            if size > MAX_FILE_SIZE:
                await self._split_upload(file_path, chat_id, task_id)
                return
            
            status_msg = await app.send_message(chat_id, "📤 Starting upload...")
            
            async def progress(current: int, total: int):
                try:
                    percent = (current / total) * 100
                    await status_msg.edit_text(
                        f"📤 UPLOADING: {percent:.1f}% | {human_size(current)} / {human_size(total)}"
                    )
                except:
                    pass
            
            # Determine file type
            ext = file_path.suffix.lower()
            if ext in ['.mp4', '.mkv', '.avi', '.mov']:
                await app.send_video(chat_id, str(file_path), caption=file_path.name, progress=progress)
            else:
                await app.send_document(chat_id, str(file_path), caption=file_path.name, progress=progress)
            
            await status_msg.delete()
            
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await self.upload(file_path, chat_id, task_id)
        except Exception as e:
            logger.error(f"Upload error: {e}")
    
    async def _split_upload(self, file_path: Path, chat_id: int, task_id: str):
        """Split file into parts and upload"""
        split_dir = DOWNLOAD_DIR / "splits"
        split_dir.mkdir(exist_ok=True, parents=True)
        
        # Use Unix split command
        cmd = f"split -b {MAX_FILE_SIZE} '{file_path}' '{split_dir}/{file_path.name}.part'"
        subprocess.run(cmd, shell=True, check=True)
        
        parts = sorted(split_dir.glob(f"{file_path.name}.part*"))
        
        for i, part in enumerate(parts, 1):
            await self.upload(part, chat_id, task_id)
            part.unlink()  # Clean up after upload

async def process_download(url: str, task_id: str, is_ytdl: bool = False) -> Optional[Path]:
    """Main download dispatcher"""
    try:
        if is_ytdl:
            manager = YTDLManager()
        else:
            manager = AriaManager()
        
        return await manager.download(url, task_id)
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

@app.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    await message.reply_text(
        "🦞 **LeechBot Pro** (Python 3.13+)\n\n"
        "**Commands:**\n"
        "/leech <link> - Download & upload to Telegram\n\n"
        "**Supported:**\n"
        "• Torrents & Magnets\n"
        "• Direct URLs\n"
        "• YouTube/Instagram/etc\n"
        "• Auto-splits >2GB files",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Help", callback_data="help")]])
    )

@app.on_message(filters.command("leech") & filters.private)
async def leech(client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/leech <download_link>`")
        return
    
    url = message.command[1]
    if not is_url_valid(url):
        await message.reply_text("Invalid URL!")
        return
    
    task_id = f"{message.from_user.id}_{int(asyncio.get_event_loop().time())}"
    status_msg = await message.reply_text("🚀 Initializing download...")
    
    tasks[task_id] = DownloadTask(task_id, url, status_msg)
    
    # Determine download type
    is_ytdl = is_ytdl_supported(url)
    
    # Run download
    file_path = await process_download(url, task_id, is_ytdl)
    
    if file_path and file_path.exists():
        await status_msg.edit_text("✅ Download complete! Starting upload...")
        
        # Upload
        uploader = UploadManager()
        await uploader.upload(file_path, DUMP_CHANNEL_ID, task_id)
        
        # Cleanup
        if file_path.is_file():
            file_path.unlink()
        else:
            import shutil
            shutil.rmtree(file_path)
        
        await status_msg.edit_text("✅ Task completed successfully!")
    else:
        await status_msg.edit_text("❌ Task failed!")
    
    tasks.pop(task_id, None)

@app.on_message(filters.command("stats") & filters.private)
async def stats(client, message: Message):
    """Show current tasks"""
    if not tasks:
        await message.reply_text("No active tasks")
        return
    
    text = "📊 **Active Tasks:**\n\n"
    for task_id, task in tasks.items():
        elapsed = datetime.now() - task.start_time
        text += f"**Task:** {task_id}\n"
        text += f"**URL:** {task.url[:50]}...\n"
        text += f"**Time:** {human_time(int(elapsed.total_seconds()))}\n\n"
    
    await message.reply_text(text)

# Keep bot alive
@app.on_message(filters.command("ping") & filters.private)
async def ping(client, message: Message):
    await message.reply_text("🏓 Pong!")

if __name__ == "__main__":
    logger.info("Starting LeechBot Pro on Python 3.13+...")
    app.run()
