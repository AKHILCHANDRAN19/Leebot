#!/usr/bin/env python3
"""
WZML-X Style Telegram Leech Bot - Single File Edition
Only Leech functionality, no mirror features
"""

import asyncio
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple

import aiofiles
import aria2p
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import uvloop

# ============= Configuration =============
API_ID = 2819362
API_HASH = "578ce3d09fadd539544a327c45b55ee4"
BOT_TOKEN = "8024921755:AAEeckFdBxX8jDhAMhvKmCJRlwoz3drlkTs"
OWNER_ID = 0  # CHANGE THIS TO YOUR USER ID

# Bot Settings
DOWNLOAD_DIR = "/usr/src/app/downloads/"
LEECH_SPLIT_SIZE = 2097152000  # 2GB
AS_DOCUMENT = False
ARIA2_RPC_PORT = 6800
ARIA2_RPC_HOST = "localhost"
ARIA2_RPC_SECRET = ""

# Aria2 Configuration
ARIA2_CONF = {
    "dir": DOWNLOAD_DIR,
    "max-connection-per-server": "16",
    "split": "32",
    "min-split-size": "1M",
    "max-concurrent-downloads": "3",
    "max-download-limit": "0",
    "seed-time": "0",
    "follow-torrent": "false",
    "file-allocation": "falloc"
}

# ============= URL Validator =============
class URLValidator:
    @staticmethod
    def is_valid_url(url: str) -> bool:
        """Validate if URL is properly formatted"""
        try:
            result = urllib.parse.urlparse(url)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    @staticmethod
    def is_supported_url(url: str) -> bool:
        """Check if URL is supported (direct link, yt-dlp, etc.)"""
        # Direct file extensions
        direct_exts = ('.mkv', '.mp4', '.avi', '.mov', '.flv', '.webm', '.mp3', '.m4a', '.flac', '.wav', 
                      '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.pdf', '.epub', '.mobi')
        
        # YT-DLP supported sites pattern
        yt_dlp_patterns = [
            r'https?://(www\.)?(youtube|youtu\.be|m\.youtube)\.com/',
            r'https?://(www\.)?(facebook|fb)\.com/',
            r'https?://(www\.)?(instagram|instagr\.am)\.com/',
            r'https?://(www\.)?(twitter|x)\.com/',
            r'https?://(www\.)?(tiktok)\.com/',
            r'https?://(www\.)?(vimeo)\.com/',
            r'https?://(www\.)?(dailymotion)\.com/',
            r'https?://(www\.)?(pornhub)\.com/',
        ]
        
        if url.lower().endswith(direct_exts):
            return True
        
        for pattern in yt_dlp_patterns:
            if re.match(pattern, url, re.IGNORECASE):
                return True
        
        # Generic direct link check
        return URLValidator.is_valid_url(url)

# ============= Aria2 Manager =============
class Aria2Manager:
    def __init__(self):
        self.aria2 = None
        self.is_connected = False
    
    async def connect(self):
        """Initialize Aria2 connection"""
        try:
            self.aria2 = aria2p.API(
                aria2p.Client(
                    host=f"http://{ARIA2_RPC_HOST}",
                    port=ARIA2_RPC_PORT,
                    secret=ARIA2_RPC_SECRET
                )
            )
            # Test connection
            self.aria2.get_global_options()
            self.is_connected = True
            print("✅ Aria2 connected successfully")
        except Exception as e:
            print(f"❌ Aria2 connection failed: {e}")
            self.is_connected = False
    
    async def start_aria2_daemon(self):
        """Start aria2 daemon if not running"""
        try:
            os.system(
                f"aria2c --enable-rpc --rpc-listen-all --daemon=true "
                f"--rpc-listen-port={ARIA2_RPC_PORT} "
                f"--rpc-secret={ARIA2_RPC_SECRET} "
                f"--dir={ARIA2_CONF['dir']} "
                f"--max-connection-per-server={ARIA2_CONF['max-connection-per-server']} "
                f"--split={ARIA2_CONF['split']} "
                f"--min-split-size={ARIA2_CONF['min-split-size']} "
                f"--max-concurrent-downloads={ARIA2_CONF['max-concurrent-downloads']} "
                f"--max-download-limit={ARIA2_CONF['max-download-limit']} "
                f"--seed-time={ARIA2_CONF['seed-time']} "
                f"--follow-torrent={ARIA2_CONF['follow-torrent']} "
                f"--file-allocation={ARIA2_CONF['file-allocation']} "
                f"--quiet=true"
            )
            await asyncio.sleep(2)  # Wait for daemon to start
            print("🚀 Aria2 daemon started")
        except Exception as e:
            print(f"❌ Failed to start Aria2 daemon: {e}")
    
    async def download(self, url: str, message: Message) -> Optional[str]:
        """Download file using Aria2 and return file path"""
        try:
            # Add download
            download = self.aria2.add(url, {"dir": DOWNLOAD_DIR})
            
            # Monitor progress
            last_progress = 0
            while not download.is_complete:
                if download.error_message:
                    raise Exception(f"Aria2 Error: {download.error_message}")
                
                download.update()
                progress = int(download.progress)
                
                # Update status every 10%
                if progress - last_progress >= 10:
                    await message.edit_text(
                        f"⬇️ Downloading...\n"
                        f"📁 {download.name}\n"
                        f"📊 {progress}%\n"
                        f"⚡ {download.download_speed_string()}\n"
                        f"📦 {download.completed_length_string()}/{download.total_length_string()}"
                    )
                    last_progress = progress
                
                await asyncio.sleep(2)
            
            if download.is_complete:
                return os.path.join(download.dir, download.files[0].path)
            else:
                return None
                
        except Exception as e:
            print(f"Download error: {e}")
            raise

# ============= File Utilities =============
class FileManager:
    @staticmethod
    def get_file_size(path: str) -> int:
        """Get file size in bytes"""
        try:
            return os.path.getsize(path)
        except:
            return 0
    
    @staticmethod
    async def split_file(file_path: str, max_size: int = LEECH_SPLIT_SIZE) -> list:
        """Split large file into parts"""
        if FileManager.get_file_size(file_path) <= max_size:
            return [file_path]
        
        # Use Linux split command (efficient for large files)
        output_dir = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)
        name, ext = os.path.splitext(base_name)
        
        # Create split files
        split_prefix = os.path.join(output_dir, f"{name}_part_")
        os.system(f"split -b {max_size} -d {file_path} {split_prefix}")
        
        # Get split files list
        split_files = []
        for f in os.listdir(output_dir):
            if f.startswith(f"{name}_part_"):
                split_files.append(os.path.join(output_dir, f))
        
        # Sort by part number
        split_files.sort()
        
        # Rename to include extension
        final_files = []
        for i, part in enumerate(split_files):
            new_name = f"{split_prefix}{i+1:03d}{ext}"
            os.rename(part, new_name)
            final_files.append(new_name)
        
        # Remove original
        os.remove(file_path)
        
        return final_files

# ============= Uploader =============
class TelegramUploader:
    def __init__(self, client: Client):
        self.client = client
    
    async def upload_file(self, file_path: str, chat_id: int, message: Message, split_file: bool = False):
        """Upload file to Telegram with progress"""
        file_size = FileManager.get_file_size(file_path)
        filename = os.path.basename(file_path)
        
        # Progress callback
        async def progress(current, total):
            if time.time() - getattr(self, '_last_update', 0) < 5:
                return
            self._last_update = time.time()
            
            try:
                progress_pct = int(current * 100 / total)
                await message.edit_text(
                    f"⬆️ Uploading{' part' if split_file else ''}...\n"
                    f"📁 {filename}\n"
                    f"📊 {progress_pct}%\n"
                    f"📦 {self.humanbytes(current)}/{self.humanbytes(total)}"
                )
            except:
                pass
        
        # Upload
        try:
            if AS_DOCUMENT or split_file:
                await self.client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=f"📄 {filename}",
                    progress=progress
                )
            else:
                # Try as video/audio first
                if filename.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                    await self.client.send_video(
                        chat_id=chat_id,
                        video=file_path,
                        caption=f"🎥 {filename}",
                        progress=progress
                    )
                elif filename.lower().endswith(('.mp3', '.m4a', '.flac', '.wav')):
                    await self.client.send_audio(
                        chat_id=chat_id,
                        audio=file_path,
                        caption=f"🎵 {filename}",
                        progress=progress
                    )
                else:
                    await self.client.send_document(
                        chat_id=chat_id,
                        document=file_path,
                        caption=f"📄 {filename}",
                        progress=progress
                    )
        
        except Exception as e:
            print(f"Upload error: {e}")
            raise
    
    def humanbytes(self, size: int) -> str:
        """Convert bytes to human readable format"""
        if not size:
            return "0B"
        
        size = int(size)
        power = 2**10
        n = 0
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        
        while size >= power and n < len(units) - 1:
            size /= power
            n += 1
        
        return f"{size:.2f} {units[n]}"

# ============= Main Bot =============
class LeechBot:
    def __init__(self):
        self.client = Client(
            "leech_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            in_memory=True,
            max_concurrent_transmissions=3
        )
        self.aria2 = Aria2Manager()
        self.uploader = TelegramUploader(self.client)
        self.validator = URLValidator()
    
    def is_owner(self, user_id: int) -> bool:
        """Check if user is authorized"""
        return user_id == OWNER_ID
    
    async def start_command(self, client: Client, message: Message):
        """Handle /start command"""
        if not self.is_owner(message.from_user.id):
            await message.reply_text("❌ You are not authorized to use this bot!")
            return
        
        await message.reply_text(
            "🤖 **WZML-X Leech Bot**\n\n"
            "Commands:\n"
            "`/leech <link>` - Leech file to Telegram\n"
            "`/status` - Check Aria2 status\n"
            "`/cancel` - Cancel current task\n"
            "`/ping` - Check bot response\n\n"
            "Supports: Direct links, YouTube, Twitter, Instagram, Facebook, and 1000+ yt-dlp sites",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    
    async def leech_command(self, client: Client, message: Message):
        """Handle /leech command"""
        if not self.is_owner(message.from_user.id):
            await message.reply_text("❌ Unauthorized!")
            return
        
        # Extract URL
        if len(message.command) < 2:
            await message.reply_text("❌ Usage: `/leech <link>`", parse_mode=enums.ParseMode.MARKDOWN)
            return
        
        url = message.command[1]
        
        # Validate URL
        if not self.validator.is_supported_url(url):
            await message.reply_text("❌ Invalid or unsupported URL!")
            return
        
        status_msg = await message.reply_text("🔄 Processing...")
        
        try:
            # Download using Aria2
            await status_msg.edit_text("⬇️ Starting Aria2 download...")
            file_path = await self.aria2.download(url, status_msg)
            
            if not file_path or not os.path.exists(file_path):
                raise Exception("Download failed or file not found")
            
            await status_msg.edit_text("✅ Download complete! Preparing upload...")
            
            # Check file size
            file_size = FileManager.get_file_size(file_path)
            
            if file_size > 2097152000:  # 2GB Telegram limit
                await status_msg.edit_text("⚠️ File >2GB, splitting...")
                parts = await FileManager.split_file(file_path)
                
                # Upload parts
                for i, part in enumerate(parts, 1):
                    await status_msg.edit_text(f"⬆️ Uploading part {i}/{len(parts)}...")
                    await self.uploader.upload_file(part, message.chat.id, status_msg, split_file=True)
                
                # Cleanup parts
                for part in parts:
                    os.remove(part)
                
            else:
                # Upload single file
                await self.uploader.upload_file(file_path, message.chat.id, status_msg)
            
            await status_msg.edit_text("✅ Leech completed successfully!")
            
            # Cleanup
            if os.path.exists(file_path):
                os.remove(file_path)
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
            print(f"Leech error: {e}")
    
    async def status_command(self, client: Client, message: Message):
        """Check Aria2 status"""
        if not self.is_owner(message.from_user.id):
            return
        
        try:
            stats = self.aria2.aria2.get_global_stats()
            await message.reply_text(
                "📊 **Aria2 Status**\n\n"
                f"📥 Active Downloads: {stats.num_active}\n"
                f"⌛ Waiting: {stats.num_waiting}\n"
                f"✅ Completed: {stats.num_stopped}\n"
                f"⚡ Download Speed: {self.uploader.humanbytes(stats.download_speed)}/s\n"
                f"⬆️ Upload Speed: {self.uploader.humanbytes(stats.upload_speed)}/s",
                parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception as e:
            await message.reply_text(f"❌ Aria2 not connected: {e}")
    
    async def ping_command(self, client: Client, message: Message):
        """Ping command"""
        if not self.is_owner(message.from_user.id):
            return
        
        start_time = time.time()
        m = await message.reply_text("🏓 Pinging...")
        end_time = time.time()
        ping_time = round((end_time - start_time) * 1000, 2)
        await m.edit_text(f"🏓 Pong! `{ping_time}ms`", parse_mode=enums.ParseMode.MARKDOWN)
    
    async def cancel_command(self, client: Client, message: Message):
        """Cancel all downloads"""
        if not self.is_owner(message.from_user.id):
            return
        
        try:
            downloads = self.aria2.aria2.get_downloads()
            for download in downloads:
                if download.is_active:
                    download.remove(force=True)
            await message.reply_text("✅ All active downloads cancelled!")
        except Exception as e:
            await message.reply_text(f"❌ Error: {e}")
    
    async def start_services(self):
        """Initialize bot and Aria2"""
        print("🚀 Starting WZML-X Leech Bot...")
        
        # Start Aria2 daemon
        await self.aria2.start_aria2_daemon()
        await self.aria2.connect()
        
        # Register handlers
        self.client.on_message(filters.command("start"))(self.start_command)
        self.client.on_message(filters.command("leech"))(self.leech_command)
        self.client.on_message(filters.command("status"))(self.status_command)
        self.client.on_message(filters.command("ping"))(self.ping_command)
        self.client.on_message(filters.command("cancel"))(self.cancel_command)
        
        print("✅ Bot ready!")

# ============= Run Bot =============
if __name__ == "__main__":
    # Install uvloop for better performance
    if sys.platform != "win32":
        uvloop.install()
    
    # Create download directory
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    # Initialize bot
    bot = LeechBot()
    
    # Start bot
    asyncio.run(bot.start_services())
    bot.client.run()
