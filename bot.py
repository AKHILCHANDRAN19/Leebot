import os
import asyncio
import logging
import time
import shutil
from pathlib import Path
from typing import List, Optional, Callable
import aiofiles
import aiohttp
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait
import yt_dlp
import aioqbt
import psutil
from tenacity import retry, stop_after_attempt, wait_exponential
from PIL import Image
import magic
import uvloop
from fastapi import FastAPI
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ========== ⚠️ HARDCODED CREDENTIALS - FOR TESTING ONLY ==========
HARDCODED_CONFIG = {
    'API_ID': 2819362,
    'API_HASH': '578ce3d09fadd539544a327c45b55ee4',
    'BOT_TOKEN': '8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0',
    'ALLOWED_USERS': None,  # None = allow all users
}
# ========== END CREDENTIALS ==========

CONFIG = {
    'DOWNLOAD_DIR': Path('/tmp/downloads'),
    'MAX_CONCURRENT_DOWNLOADS': 3,
    'MAX_CONCURRENT_UPLOADS': 5,
    'QBITT_HOST': "http://localhost:8080",
    'QBITT_USER': 'admin',
    'QBITT_PASS': 'adminadmin',
    **HARDCODED_CONFIG,
}

# ========== LOGGING ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== FASTAPI FOR RENDER ==========
app = FastAPI()

@app.get("/")
async def health_check():
    return {
        "status": "alive",
        "cpu": f"{psutil.cpu_percent()}%",
        "memory": f"{psutil.virtual_memory().percent}%",
        "active_downloads": len(active_downloads)
    }

# ========== GLOBALS ==========
bot = Client(
    "leech_bot",
    api_id=CONFIG['API_ID'],
    api_hash=CONFIG['API_HASH'],
    bot_token=CONFIG['BOT_TOKEN'],
    workers=100,
    max_concurrent_transmissions=CONFIG['MAX_CONCURRENT_UPLOADS']
)

download_semaphore = asyncio.Semaphore(CONFIG['MAX_CONCURRENT_DOWNLOADS'])
upload_semaphore = asyncio.Semaphore(CONFIG['MAX_CONCURRENT_UPLOADS'])
active_downloads = {}
scheduler = AsyncIOScheduler()

# ========== UTILITY FUNCTIONS ==========
def is_allowed(user_id: int) -> bool:
    if CONFIG['ALLOWED_USERS'] is None:
        return True
    return user_id in CONFIG['ALLOWED_USERS']

def human_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

def get_file_info(file_path: Path) -> dict:
    mime = magic.from_file(str(file_path), mime=True)
    return {
        'name': file_path.name,
        'size': file_path.stat().st_size,
        'mime': mime,
        'is_video': mime.startswith('video/'),
        'is_archive': mime in [
            'application/zip', 'application/x-7z-compressed',
            'application/x-rar', 'application/x-tar',
            'application/gzip', 'application/x-bzip2'
        ]
    }

def get_filename_from_url(url: str) -> str:
    from urllib.parse import urlparse, unquote
    path = urlparse(url).path
    return Path(unquote(path)).name or f"download_{int(time.time())}.bin"

def is_ytdlp_supported(url: str) -> bool:
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
            ydl.extract_info(url, download=False)
            return True
    except:
        return False

async def create_thumbnail(video_path: Path) -> Optional[Path]:
    thumb_path = video_path.with_suffix('.jpg')
    try:
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', str(video_path), '-ss', '00:00:01',
            '-vframes', '1', '-vf', 'scale=320:-1', str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.wait()
        if thumb_path.exists():
            return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail failed: {e}")
    return None

async def create_zip(files: List[Path], zip_path: Path):
    import zipfile
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file in files:
            zf.write(file, file.name)

async def extract_archive(file_path: Path, extract_to: Path) -> List[Path]:
    await asyncio.to_thread(extract_to.mkdir, parents=True, exist_ok=True)
    mime = magic.from_file(str(file_path), mime=True)
    
    try:
        if mime == 'application/zip':
            await asyncio.to_thread(shutil.unpack_archive, str(file_path), str(extract_to), 'zip')
        elif mime == 'application/x-tar':
            await asyncio.to_thread(shutil.unpack_archive, str(file_path), str(extract_to), 'tar')
        elif mime == 'application/gzip':
            await asyncio.to_thread(shutil.unpack_archive, str(file_path), str(extract_to), 'gztar')
        elif mime == 'application/x-bzip2':
            await asyncio.to_thread(shutil.unpack_archive, str(file_path), str(extract_to), 'bztar')
        elif mime == 'application/x-rar':
            process = await asyncio.create_subprocess_exec(
                'unrar', 'x', str(file_path), str(extract_to), '-y',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()
        else:
            return [file_path]
        
        files = [f for f in extract_to.rglob('*') if f.is_file()]
        return files if files else [file_path]
        
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return [file_path]

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def download_direct(url: str, dest: Path, progress_callback: Callable):
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=0),
        connector=aiohttp.TCPConnector(limit=5)
    ) as session:
        async with session.get(url, raise_for_status=True) as response:
            total = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            async with aiofiles.open(dest, 'wb') as f:
                async for chunk in response.content.iter_chunked(1024*1024):
                    if chunk:
                        await f.write(chunk)
                        downloaded += len(chunk)
                        await progress_callback(downloaded, total)

async def download_ytdlp(url: str, dest_dir: Path, progress_callback: Callable) -> List[Path]:
    files = []
    
    def progress_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            asyncio.create_task(progress_callback(downloaded, total))
    
    ytdl_opts = {
        'outtmpl': str(dest_dir / '%(title)s.%(ext)s'),
        'progress_hooks': [progress_hook],
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }
    
    with yt_dlp.YoutubeDL(ytdl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if 'entries' in info:
            for entry in info['entries']:
                if entry:
                    file = dest_dir / f"{entry['title']}.{entry['ext']}"
                    if file.exists():
                        files.append(file)
        else:
            file = dest_dir / f"{info['title']}.{info['ext']}"
            if file.exists():
                files.append(file)
    
    return files

async def download_torrent(url: str, dest_dir: Path, progress_msg: Message) -> List[Path]:
    try:
        async with aioqbt.Client(CONFIG['QBITT_HOST'], username=CONFIG['QBITT_USER'], password=CONFIG['QBITT_PASS']) as qb:
            if url.startswith('magnet:'):
                await qb.torrents.add(magnet_urls=[url], save_path=str(dest_dir))
            else:
                torrent_file = dest_dir / "temp.torrent"
                await download_direct(url, torrent_file, lambda d, t: None)
                with open(torrent_file, 'rb') as f:
                    await qb.torrents.add(torrent_files=f.read(), save_path=str(dest_dir))
                torrent_file.unlink()
            
            await asyncio.sleep(2)
            torrents = await qb.torrents.info()
            torrent = max(torrents, key=lambda t: t.added_on)
            
            while torrent.state not in ['uploading', 'pausedUP', 'stalledUP']:
                torrent = await qb.torrents.info(torrent_hash=torrent.hash)
                if torrent.state in ['error', 'missingFiles']:
                    raise Exception("Torrent failed")
                
                progress = torrent.progress * 100
                await progress_msg.edit_text(
                    f"🌀 **Torrent**\n"
                    f"{torrent.name[:50]}\n"
                    f"Progress: {progress:.1f}% | {human_bytes(torrent.dlspeed)}/s\n"
                    f"Peers: {torrent.num_leechs} | Seeds: {torrent.num_seeds}"
                )
                await asyncio.sleep(2)
            
            return [f for f in dest_dir.rglob('*') if f.is_file() and not f.name.endswith('.!qB')]
            
    except Exception as e:
        logger.error(f"Torrent error: {e}")
        raise

async def upload_file(message: Message, file_path: Path, as_video: bool = False):
    async with upload_semaphore:
        info = get_file_info(file_path)
        progress_data = {'last_update': 0}
        
        async def progress_callback(current: int, total: int):
            now = time.time()
            if now - progress_data['last_update'] < 5:
                return
            progress_data['last_update'] = now
            
            await message.edit_text(
                f"📤 **Uploading**\n"
                f"`{file_path.name[:60]}`\n"
                f"Progress: {(current/total)*100:.1f}%"
            )
        
        try:
            thumb = await create_thumbnail(file_path) if as_video and info['is_video'] else None
            
            if as_video and info['is_video']:
                await message.reply_video(
                    str(file_path),
                    caption=f"<code>{file_path.name}</code>",
                    thumb=str(thumb) if thumb else None,
                    supports_streaming=True,
                    progress=progress_callback
                )
            else:
                await message.reply_document(
                    str(file_path),
                    caption=f"<code>{file_path.name}</code>",
                    thumb=str(thumb) if thumb else None,
                    force_document=True,
                    progress=progress_callback
                )
                
        except FloodWait as f:
            logger.warning(f"FloodWait: {f.value}s")
            await asyncio.sleep(f.value)
            await upload_file(message, file_path, as_video)
        except Exception as e:
            logger.error(f"Upload error: {e}")
            await message.reply(f"❌ Upload failed: `{e}`")
        finally:
            if thumb and thumb.exists():
                thumb.unlink()

async def process_leech(message: Message, url: str, flags: List[str]):
    download_dir = CONFIG['DOWNLOAD_DIR'] / str(message.id)
    download_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        as_video = 'v' in flags
        do_zip = 'z' in flags
        do_unzip = 'e' in flags
        
        status_msg = await message.reply("🔄 Starting download...")
        
        async with download_semaphore:
            if url.startswith('magnet:') or url.endswith('.torrent'):
                files = await download_torrent(url, download_dir, status_msg)
            elif is_ytdlp_supported(url):
                files = await download_ytdlp(url, download_dir, lambda d, t: None)
            else:
                filename = get_filename_from_url(url)
                file_path = download_dir / filename
                await download_direct(url, file_path, lambda d, t: None)
                files = [file_path]
            
            if not files:
                raise Exception("No files downloaded")
            
            await status_msg.edit("✅ Download complete, processing...")
            
            if do_zip:
                await status_msg.edit("📦 Creating zip...")
                zip_path = download_dir / f"{download_dir.name}.zip"
                await create_zip(files, zip_path)
                upload_files = [zip_path]
            elif do_unzip:
                await status_msg.edit("📂 Extracting...")
                upload_files = []
                for file in files:
                    if get_file_info(file)['is_archive']:
                        extracted = await extract_archive(file, download_dir / "extracted")
                        upload_files.extend(extracted)
                        file.unlink()
                    else:
                        upload_files.append(file)
            else:
                upload_files = files
            
            await status_msg.edit(f"📤 Uploading {len(upload_files)} file(s)...")
            upload_tasks = [upload_file(message, file, as_video) for file in upload_files]
            await asyncio.gather(*upload_tasks)
            
            await status_msg.edit("✅ **Leech completed!**")
            
    except Exception as e:
        logger.error(f"Leech error: {e}")
        await message.reply(f"❌ **Error:** `{e}`")
    finally:
        if download_dir.exists():
            shutil.rmtree(download_dir)

# ========== COMMAND HANDLERS ==========
@bot.on_message(filters.command("leech") & filters.private)
async def leech_handler(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return await message.reply("❌ **Unauthorized**")
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply(
            "📥 **Leech Usage**\n\n"
            "`/leech <URL>` - Document\n"
            "`/leech <URL> -v` - Video\n"
            "`/leech <URL> -z` - ZIP\n"
            "`/leech <URL> -e` - Extract\n"
            "`/leech <URL> -v -z` - Combine\n\n"
            "**Supported:** Direct links, YouTube, TikTok, Instagram, Magnet/Torrent\n\n"
            "**Example:** `/leech https://example.com/file.zip -e -v`"
        )
    
    cmd_text = args[1]
    flags = []
    for flag in ['-v', '-z', '-e']:
        if flag in cmd_text:
            flags.append(flag.replace('-', ''))
            cmd_text = cmd_text.replace(flag, '')
    
    url = cmd_text.strip()
    if not url:
        return await message.reply("❌ No URL provided")
    
    asyncio.create_task(process_leech(message, url, flags))

@bot.on_message(filters.command("status") & filters.private)
async def status_handler(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    text = (
        "🤖 **Bot Status**\n\n"
        f"**CPU:** {cpu}%\n"
        f"**RAM:** {memory.percent}% ({human_bytes(memory.used)}/{human_bytes(memory.total)})\n"
        f"**Disk:** {disk.percent}% ({human_bytes(disk.used)}/{human_bytes(disk.total)})\n"
        f"**Active Downloads:** {len(active_downloads)}\n"
        f"**Concurrent Limit:** {CONFIG['MAX_CONCURRENT_DOWNLOADS']}"
    )
    await message.reply(text)

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client: Client, message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.reply("⏹️ Cancel registered. Use `/leech` for new downloads.")

# ========== BACKGROUND TASKS ==========
@scheduler.scheduled_job('interval', minutes=5)
async def keep_alive():
    logger.info(f"Pulse | CPU: {psutil.cpu_percent()}% | Memory: {psutil.virtual_memory().percent}%")

# ========== MAIN ==========
def main():
    """Fixed: Scheduler starts INSIDE the async loop"""
    uvloop.install()
    CONFIG['DOWNLOAD_DIR'].mkdir(parents=True, exist_ok=True)
    
    async def run_services():
        # ✅ CORRECT: Start scheduler after event loop is running
        scheduler.start()
        logger.info("✅ Scheduler started successfully")
        
        await bot.start()
        logger.info("✅ Bot started successfully")
        
        # Start web server for Render health checks
        port = int(os.getenv("PORT", 8000))
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="error",
            access_log=False
        )
        server = uvicorn.Server(config)
        await server.serve()
    
    try:
        asyncio.run(run_services())
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        # Cleanup
        try:
            scheduler.shutdown()
        except:
            pass
        try:
            bot.stop()
        except:
            pass
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    main()
