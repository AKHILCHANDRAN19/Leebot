#!/usr/bin/env python3
# Web Service LeechBot - Production Edition
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

# ✅ Persistent logging to file
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode='a')  # Debug logs persist
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
        self.download_start = None  # ✅ Track download start time
        self.error_msg = None  # ✅ Persistent error display

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

# ✅ FIXED: Use CORRECT start_time parameter
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
        f"ETA: {eta_str}\n\n"
        "Thanks for using this bot"
    )

# ✅ FIXED: Multi-method aria2 installation
def ensure_aria2_installed():
    """Try multiple methods to install aria2"""
    try:
        # Method 1: Check if already installed
        result = subprocess.run(['which', 'aria2c'], capture_output=True, text=True)
        if result.returncode == 0:
            path = result.stdout.strip()
            # Test it works
            test = subprocess.run([path, '--version'], capture_output=True, text=True)
            if test.returncode == 0:
                logger.info(f"✅ aria2c working: {test.stdout.split()[0]}")
                return True
        
        logger.warning("aria2c not found, installing...")
        
        # Method 2: apt-get (most reliable)
        install_cmd = "apt-get update -qq && apt-get install -y aria2"
        install = subprocess.run(install_cmd, shell=True, capture_output=True, text=True, timeout=120)
        
        if install.returncode == 0:
            # Verify
            verify = subprocess.run(['which', 'aria2c'], capture_output=True, text=True)
            if verify.returncode == 0:
                logger.info(f"✅ aria2c installed: {verify.stdout.strip()}")
                return True
        
        logger.error(f"❌ apt-get failed: {install.stderr}")
        
        # Method 3: apt (fallback)
        alt_cmd = "apt update -qq && apt install -y aria2"
        alt = subprocess.run(alt_cmd, shell=True, capture_output=True, text=True, timeout=120)
        
        if alt.returncode == 0:
            verify = subprocess.run(['which', 'aria2c'], capture_output=True, text=True)
            if verify.returncode == 0:
                logger.info(f"✅ aria2c installed via apt: {verify.stdout.strip()}")
                return True
        
        logger.error(f"❌ All methods failed. apt stderr: {alt.stderr}")
        return False
        
    except Exception as e:
        logger.error(f"❌ Installation error: {e}")
        return False

# === DOWNLOAD MANAGERS ===
class AriaManager:
    def __init__(self):
        self.config_path = WORK_DIR / "aria2.conf"
        self._setup_config()
        
        # ✅ Verify on startup
        if not ensure_aria2_installed():
            raise RuntimeError(
                "❌ aria2c installation failed!\n\n"
                "Manual fix: SSH into Render and run:\n"
                "sudo apt-get update && sudo apt-get install -y aria2\n"
                "Then redeploy."
            )
    
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
log-level=error
"""
        self.config_path.write_text(config)
    
    async def download(self, url: str, task_id: str, task: DownloadTask) -> Optional[Path]:
        """Download with real-time progress"""
        cmd = ["aria2c", f"--conf-path={self.config_path}", url]
        logger.info(f"🚀 Starting download: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, 'PATH': '/usr/bin:/usr/local/bin:/bin:/usr/sbin:/usr/local/sbin'}
        )
        
        task.download_start = datetime.now()  # ✅ Set download start time
        
        # ✅ NEW: Real-time progress parser
        async def read_progress():
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                
                line = line.decode().strip()
                
                # Parse aria2 output: [DL:1.2MiB/100MiB(1%)] ...
                if '[DL:' in line and '%)' in line:
                    try:
                        # Extract sizes
                        size_match = re.search(r'\[DL:([^\)]+)\((\d+)%\)\]', line)
                        if size_match:
                            size_str = size_match.group(1)  # "1.2MiB/100MiB"
                            percent = int(size_match.group(2))
                            
                            # Parse downloaded amount
                            downloaded_str = size_str.split('/')[0]
                            
                            # Convert to bytes for progress bar
                            if 'GiB' in downloaded_str:
                                downloaded = float(downloaded_str.replace('GiB', '')) * 1024**3
                            elif 'MiB' in downloaded_str:
                                downloaded = float(downloaded_str.replace('MiB', '')) * 1024**2
                            elif 'KiB' in downloaded_str:
                                downloaded = float(downloaded_str.replace('KiB', '')) * 1024
                            else:
                                downloaded = float(downloaded_str)
                            
                            # Get total size if available
                            total_str = size_str.split('/')[1] if '/' in size_str else None
                            
                            if task.download_start:
                                progress_text = await detailed_progress(
                                    int(downloaded), 
                                    int(downloaded * 100 / percent) if percent > 0 else int(downloaded),
                                    task.download_start,
                                    "📥 Downloading"
                                )
                                try:
                                    await task.status_msg.edit_text(progress_text)
                                except:
                                    pass
                    
                    except Exception as e:
                        logger.debug(f"Parse error: {e}")
        
        # Start reading progress
        progress_task = asyncio.create_task(read_progress())
        
        # Wait for completion
        await process.wait()
        progress_task.cancel()
        
        # Check result
        if process.returncode == 0:
            files = list(DOWNLOAD_DIR.glob('*'))
            if files:
                downloaded = max(files, key=lambda f: f.stat().st_mtime)
                logger.info(f"✅ Download complete: {downloaded.name}")
                return downloaded
        
        stderr = await process.stderr.read()
        logger.error(f"❌ aria2c failed: {stderr.decode()}")
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
            
            # ✅ Create persistent error message
            error_msg = await status_msg.reply_text("⏳ Preparing upload...")
            
            progress_text = await detailed_progress(0, file_size, datetime.now(), "📤 Uploading")
            await status_msg.edit_text(progress_text)
            
            # ✅ Track upload start time
            upload_start = datetime.now()
            
            async def progress(current: int, total: int):
                try:
                    # Update every 3 seconds
                    if (datetime.now().second % 3) == 0:
                        progress_text = await detailed_progress(current, total, upload_start, "📤 Uploading")
                        asyncio.create_task(status_msg.edit_text(progress_text))
                except Exception as e:
                    logger.debug(f"Progress error: {e}")
            
            ext = file_path.suffix.lower()
            if ext in ['.mp4', '.mkv', '.avi', '.mov']:
                sent_msg = await bot.send_video(chat_id, str(file_path), caption=file_path.name, progress=progress)
            else:
                sent_msg = await bot.send_document(chat_id, str(file_path), caption=file_path.name, progress=progress)
            
            if sent_msg:
                await error_msg.delete()
                await status_msg.edit_text("✅ Upload completed successfully!")
                logger.info(f"✅ Uploaded: {file_path.name} ({human_size(file_size)})")
                
                # ✅ Verify upload
                try:
                    await bot.get_messages(chat_id, sent_msg.id)
                    logger.info(f"✅ Verified in channel: {chat_id}")
                except:
                    logger.error(f"❌ Upload verification failed for {sent_msg.id}")
            else:
                logger.error("❌ send_video/document returned None")
                await error_msg.edit_text("❌ Upload failed: No message returned")
                
        except FloodWait as e:
            logger.warning(f"⏳ FloodWait: {e.value}s")
            await asyncio.sleep(e.value + 5)
            await self.upload(file_path, chat_id, task_id, status_msg, task)
        except Exception as e:
            logger.error(f"❌ Upload error: {e}", exc_info=True)
            if task.error_msg:
                await task.error_msg.edit_text(f"❌ Upload error: {str(e)}")
            await status_msg.edit_text("❌ Upload failed!")

    async def _split_upload(self, file_path: Path, chat_id: int, task_id: str, status_msg: Message, task: DownloadTask):
        split_dir = DOWNLOAD_DIR / "splits"
        split_dir.mkdir(exist_ok=True, parents=True)
        
        info_msg = await status_msg.reply_text(f"📦 File too large! Splitting into {MAX_FILE_SIZE // (1024**3)}GB parts...")
        
        cmd = f"split -b {MAX_FILE_SIZE} '{file_path}' '{split_dir}/{file_path.name}.part'"
        subprocess.run(cmd, shell=True, check=True)
        
        parts = sorted(split_dir.glob(f"{file_path.name}.part*"))
        for i, part in enumerate(parts, 1):
            await info_msg.edit_text(f"📤 Uploading part {i}/{len(parts)}: {part.name}")
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
    
    url = message.command[1].strip()
    logger.info(f"🔗 URL Received: {url}")
    logger.info(f"📊 URL Length: {len(url)} bytes")
    
    if not is_url_valid(url):
        logger.error(f"❌ Invalid URL format: {url[:100]}...")
        await message.reply_text("❌ Invalid URL format!")
        return
    
    task_id = f"{message.from_user.id}_{int(asyncio.get_event_loop().time())}"
    status_msg = await message.reply_text("🚀 Starting download...")
    
    task = DownloadTask(task_id, url, status_msg)
    tasks[task_id] = task
    
    # ✅ Create persistent error message
    task.error_msg = await status_msg.reply_text("⏳ Initializing...")
    
    asyncio.create_task(process_download(task))

async def process_download(task: DownloadTask):
    try:
        # Clear error message
        if task.error_msg:
            await task.error_msg.delete()
            task.error_msg = None
        
        logger.info(f"🎯 Processing task: {task.task_id}")
        logger.info(f"📡 URL: {task.url}")
        
        # Show initial progress
        progress_text = await detailed_progress(0, 1, datetime.now(), "📥 Downloading")
        await task.status_msg.edit_text(progress_text)
        
        # Choose downloader
        if is_torrent(task.url):
            logger.info("🧲 Detected torrent/magnet link")
            downloader = AriaManager()
        else:
            logger.info("🔗 Detected direct/yt-dlp link")
            downloader = YTDLManager()
        
        file_path = await downloader.download(task.url, task.task_id, task)
        
        if not file_path or not file_path.exists():
            logger.error(f"❌ Download failed: file_path={file_path}")
            await task.status_msg.edit_text("❌ Download failed or file not found!")
            return
        
        file_size = file_path.stat().st_size
        logger.info(f"✅ Download SUCCESS: {file_path.name} ({human_size(file_size)})")
        
        await task.status_msg.edit_text(
            f"✅ Download complete!\n"
            f"📁 File: {file_path.name}\n"
            f"📊 Size: {human_size(file_size)}\n"
            f"🚀 Starting upload..."
        )
        
        uploader = UploadManager()
        await uploader.upload(file_path, DUMP_CHANNEL_ID, task.task_id, task.status_msg, task)
        
        # Cleanup
        if file_path.exists():
            if file_path.is_file():
                logger.info(f"🗑️ Deleting file: {file_path}")
                file_path.unlink()
            else:
                import shutil
                logger.info(f"🗑️ Deleting folder: {file_path}")
                shutil.rmtree(file_path)
        
        await task.status_msg.edit_text("✅ Task completed successfully!")
        
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in process_download: {e}", exc_info=True)
        error_details = f"❌ Error: {str(e)}\n\nTask ID: {task.task_id}"
        
        # Keep error message persistent
        if task.error_msg:
            await task.error_msg.edit_text(error_details)
        else:
            await task.status_msg.reply_text(error_details)
        
        await task.status_msg.edit_text("❌ Task failed!")
    finally:
        tasks.pop(task.task_id, None)

# === MAIN ===
if __name__ == "__main__":
    logger.info("="*60)
    logger.info("🔰 LEECHBOT PRO - PRODUCTION MODE")
    logger.info("="*60)
    
    if DUMP_CHANNEL_ID == 0:
        logger.error("❌ CRITICAL: DUMP_CHANNEL_ID not set!")
        sys.exit(1)
    
    logger.info(f"📡 DUMP_CHANNEL_ID: {DUMP_CHANNEL_ID}")
    logger.info(f"👤 OWNER_ID: {OWNER_ID}")
    logger.info(f"💾 MAX_FILE_SIZE: {human_size(MAX_FILE_SIZE)}")
    
    # Verify aria2
    logger.info("🔍 Verifying aria2 installation...")
    if not ensure_aria2_installed():
        logger.error("❌ aria2 verification failed!")
        logger.error("Quick fix: Use Render SSH to run 'sudo apt-get install aria2'")
    
    logger.info("🚀 Starting web server...")
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    time.sleep(2)
    
    logger.info("🤖 Starting bot client...")
    bot.run()
