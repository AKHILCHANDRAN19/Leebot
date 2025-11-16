#!/usr/bin/env python3
# Copyright 2024 | LeechBot Pro
import os
import re
import json
import asyncio
import subprocess
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

import aiohttp
import yt_dlp
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.errors import FloodWait, BadRequest
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
API_ID = int(os.getenv("API_ID", "2819362"))
API_HASH = os.getenv("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))
DUMP_CHANNEL_ID = int(os.getenv("DUMP_CHANNEL_ID", "-1001234567890"))

# Constants
WORK_DIR = Path("/app/downloads")
WORK_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR = WORK_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB for free Telegram accounts

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global state
class BotState:
    def __init__(self):
        self.downloads: Dict[str, Any] = {}
        self.uploads: Dict[str, Any] = {}
        self.aria2c_process: Optional[subprocess.Popen] = None

state = BotState()

# Initialize Pyrogram Client
app = Client(
    "leech_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(WORK_DIR)
)

def is_valid_url(url: str) -> bool:
    """Validate URL format"""
    pattern = re.compile(
        r'^(https?|ftp|magnet):\/\/'  # Added magnet support
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain
        r'localhost|'  # localhost
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return bool(pattern.match(url))

def is_torrent_link(url: str) -> bool:
    """Check if link is torrent or magnet"""
    return url.startswith('magnet:?xt=urn:btih:') or url.endswith('.torrent')

def is_ytdl_link(url: str) -> bool:
    """Check if URL is supported by yt-dlp"""
    return any(domain in url for domain in ['youtube.com', 'youtu.be', 'twitter.com', 'x.com', 'instagram.com', 'facebook.com', 'tiktok.com'])

def get_file_size(path: Path) -> int:
    """Get file/folder size in bytes"""
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())

def format_size(size: int) -> str:
    """Format bytes to human readable"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def format_time(seconds: int) -> str:
    """Format seconds to human readable"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    else:
        return f"{seconds//3600}h {(seconds%3600)//60}m {seconds%3600%60}s"

class Aria2Downloader:
    def __init__(self):
        self.process = None
        self.session_file = WORK_DIR / "aria2.session"
        self.config_file = WORK_DIR / "aria2.conf"
        self._create_config()
    
    def _create_config(self):
        """Create aria2c configuration for torrent support"""
        config = f"""
# Aria2 Configuration for Torrent Support
dir={DOWNLOAD_DIR}
input-file={self.session_file}
save-session={self.session_file}
save-session-interval=60
continue=true
max-concurrent-downloads=5
max-connection-per-server=16
split=10
min-split-size=10M
max-overall-download-limit=0
max-download-limit=0
max-overall-upload-limit=50K
max-upload-limit=20K
seed-ratio=0
seed-time=0
bt-enable-hook-after-hash-check=true
bt-enable-lpd=true
bt-max-peers=55
bt-request-peer-speed-limit=10M
bt-stop-timeout=0
bt-tracker-connect-timeout=60
bt-tracker-timeout=60
dht-file-path={WORK_DIR}/dht.dat
dht-file-path6={WORK_DIR}/dht6.dat
enable-dht=true
enable-dht6=true
enable-peer-exchange=true
peer-id-prefix=-TR2770-
user-agent=Transmission/2.77
peer-agent=Transmission/2.77
allow-overwrite=true
auto-file-renaming=true
file-allocation=trunc
max-resume-failure-tries=0
"""
        self.config_file.write_text(config)
    
    async def download(self, url: str, task_id: str) -> Optional[Path]:
        """Download file/torrent using aria2c"""
        try:
            if url.startswith('magnet:?'):
                # For magnet links, add additional bt options
                cmd = [
                    "aria2c",
                    f"--conf-path={self.config_file}",
                    "--follow-torrent=mem",
                    "--bt-metadata-only=false",
                    "--bt-save-metadata=false",
                    url
                ]
            else:
                cmd = ["aria2c", f"--conf-path={self.config_file}", url]
            
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Parse progress
            for line in self.process.stdout:
                await self._parse_progress(line, task_id)
            
            self.process.wait()
            
            if self.process.returncode == 0:
                # Find downloaded file
                downloaded_files = list(DOWNLOAD_DIR.glob('*'))
                if downloaded_files:
                    return downloaded_files[0]
            
            return None
            
        except Exception as e:
            logger.error(f"Aria2 download error: {e}")
            return None
    
    async def _parse_progress(self, line: str, task_id: str):
        """Parse aria2c progress output"""
        if "ETA:" in line:
            # Parse aria2c progress format
            parts = line.split()
            try:
                progress = parts[1]  # "99%"
                downloaded = parts[2].split('/')[0]  # "1.2MiB"
                total = parts[2].split('/')[1].split('(')[0]  # "2.3MiB"
                speed = parts[3][1:]  # "1.2MiB/s"
                eta = parts[4].split(':')[1]  # "1m30s"
                
                progress_msg = (
                    f"📥 DOWNLOADING\n\n"
                    f"Progress: {progress}\n"
                    f"Speed: {speed}\n"
                    f"ETA: {eta}\n"
                    f"Downloaded: {downloaded} / {total}"
                )
                
                # Update status message if exists
                if task_id in state.downloads:
                    try:
                        await state.downloads[task_id]['status_msg'].edit_text(progress_msg)
                    except:
                        pass
            except:
                pass

class YTDLDownloader:
    async def download(self, url: str, task_id: str) -> Optional[Path]:
        """Download video using yt-dlp"""
        try:
            ydl_opts = {
                'outtmpl': str(DOWNLOAD_DIR / '%(title)s.%(ext)s'),
                'format': 'best',
                'progress_hooks': [self._progress_hook],
                'noplaylist': True,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                return Path(filename)
                
        except Exception as e:
            logger.error(f"YTDL error: {e}")
            return None
    
    def _progress_hook(self, d: Dict[str, Any]):
        if d['status'] == 'downloading':
            try:
                percent = d.get('_percent_str', '').strip()
                speed = d.get('_speed_str', '').strip()
                eta = d.get('_eta_str', '').strip()
                
                # Update task progress
                # This would need task_id context - simplified for brevity
            except:
                pass

class TelegramUploader:
    async def upload(self, file_path: Path, chat_id: int, task_id: str):
        """Upload file to Telegram with progress"""
        try:
            file_size = file_path.stat().st_size
            
            # Check if file needs splitting
            if file_size > MAX_FILE_SIZE:
                await self._split_and_upload(file_path, chat_id, task_id)
                return
            
            progress_msg = await app.send_message(
                chat_id,
                f"📤 UPLOADING: 0% | {format_size(0)} / {format_size(file_size)}"
            )
            
            async def progress(current: int, total: int):
                percent = (current / total) * 100
                try:
                    await progress_msg.edit_text(
                        f"📤 UPLOADING: {percent:.1f}% | {format_size(current)} / {format_size(total)}\n"
                        f"Speed: Calculating..."
                    )
                except:
                    pass
            
            # Send file
            if file_path.suffix.lower() in ['.mp4', '.mkv', '.avi']:
                await app.send_video(
                    chat_id,
                    str(file_path),
                    caption=f"{file_path.name}",
                    progress=progress
                )
            else:
                await app.send_document(
                    chat_id,
                    str(file_path),
                    caption=f"{file_path.name}",
                    progress=progress
                )
            
            await progress_msg.delete()
            
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await self.upload(file_path, chat_id, task_id)
        except Exception as e:
            logger.error(f"Upload error: {e}")
    
    async def _split_and_upload(self, file_path: Path, chat_id: int, task_id: str):
        """Split large files and upload parts"""
        split_dir = DOWNLOAD_DIR / "splits"
        split_dir.mkdir(exist_ok=True)
        
        # Use split command
        cmd = f"split -b {MAX_FILE_SIZE} '{file_path}' '{split_dir}/{file_path.name}.part'"
        subprocess.run(cmd, shell=True, check=True)
        
        parts = sorted(split_dir.glob(f"{file_path.name}.part*"))
        
        for i, part in enumerate(parts, 1):
            await self.upload(part, chat_id, task_id)
            part.unlink()  # Clean up part after upload

async def download_task(url: str, task_id: str, is_ytdl: bool = False) -> Optional[Path]:
    """Main download task dispatcher"""
    try:
        if is_ytdl or is_ytdl_link(url):
            downloader = YTDLDownloader()
            return await downloader.download(url, task_id)
        elif is_torrent_link(url):
            downloader = Aria2Downloader()
            return await downloader.download(url, task_id)
        else:
            # Direct download or GDrive, etc.
            downloader = Aria2Downloader()
            return await downloader.download(url, task_id)
    except Exception as e:
        logger.error(f"Download task error: {e}")
        return None

# Bot Command Handlers
@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message: Message):
    await message.reply_text(
        "🦞 **LeechBot Pro**\n\n"
        "Send /leech <link> to download and upload to Telegram\n"
        "Send /mirror <link> to download to Google Drive\n"
        "Supports: Direct links, torrents, magnets, YouTube, etc.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Help", callback_data="help")
        ]])
    )

@app.on_message(filters.command("leech") & filters.private)
async def leech_command(client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: /leech <download_link>")
        return
    
    url = message.command[1]
    if not is_valid_url(url):
        await message.reply_text("Invalid URL format!")
        return
    
    task_id = f"{message.from_user.id}_{int(asyncio.get_event_loop().time())}"
    
    # Create status message
    status_msg = await message.reply_text("🚀 Starting download...")
    state.downloads[task_id] = {'status_msg': status_msg, 'url': url}
    
    # Start download
    file_path = await download_task(url, task_id)
    
    if file_path and file_path.exists():
        await status_msg.edit_text("✅ Download complete! Uploading...")
        
        # Upload to Telegram
        uploader = TelegramUploader()
        await uploader.upload(file_path, DUMP_CHANNEL_ID, task_id)
        
        # Clean up
        if file_path.is_file():
            file_path.unlink()
        else:
            import shutil
            shutil.rmtree(file_path)
        
        await status_msg.edit_text("✅ Task completed successfully!")
    else:
        await status_msg.edit_text("❌ Download failed!")
    
    # Cleanup state
    state.downloads.pop(task_id, None)

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    if query.data == "help":
        await query.message.edit_text(
            "📖 **Help Guide**\n\n"
            "**Commands:**\n"
            "/leech <link> - Download and upload to Telegram\n\n"
            "**Supported Links:**\n"
            "• Direct HTTP/HTTPS links\n"
            "• Magnet links & .torrent files\n"
            "• YouTube/Instagram/TikTok/etc.\n"
            "• Google Drive (coming soon)\n\n"
            "**Features:**\n"
            "• Automatic file splitting (>2GB)\n"
            "• Progress tracking\n"
            "• Fast torrent downloads"
        )

# Start the bot
if __name__ == "__main__":
    logger.info("Starting LeechBot Pro...")
    app.run()
