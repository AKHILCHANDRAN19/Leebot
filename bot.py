#!/usr/bin/env python3
import os
import asyncio
import sys
import signal
from pathlib import Path
from typing import Optional, Dict, Any
import aria2p
from qbittorrentapi import Client as qbaClient, LoginFailed, APIConnectionError
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from telethon.errors import RPCError, FloodWaitError
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(',')))

# Validate config
if not all([API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS]):
    logger.error("❌ Missing required environment variables!")
    sys.exit(1)

# Paths
DOWNLOAD_PATH = Path("/tmp/downloads")
DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))

# Aria2 config
ARIA2_RPC_HOST = os.getenv("ARIA2_RPC_HOST", "http://localhost")
ARIA2_RPC_PORT = int(os.getenv("ARIA2_RPC_PORT", "6800"))
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET", "default_secret")

# qBittorrent config
QB_HOST = os.getenv("QB_HOST", "http://localhost")
QB_PORT = int(os.getenv("QB_PORT", "8080"))
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS", "adminadmin")

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

# --- CLIENTS ---
bot = TelegramClient('leech_bot', API_ID, API_HASH)

def init_aria2() -> Optional[aria2p.API]:
    """Initialize aria2c with retry logic"""
    for attempt in range(5):
        try:
            client = aria2p.Client(
                host=ARIA2_RPC_HOST,
                port=ARIA2_RPC_PORT,
                secret=ARIA2_RPC_SECRET,
                timeout=30
            )
            api = aria2p.API(client)
            api.get_version()
            logger.info("✅ Aria2c connected")
            return api
        except Exception as e:
            logger.warning(f"Aria2c attempt {attempt + 1}/5 failed: {e}")
            if attempt < 4:
                asyncio.sleep(3)
    return None

def init_qbittorrent() -> Optional[qbaClient]:
    """Initialize qBittorrent with retry logic"""
    for attempt in range(5):
        try:
            client = qbaClient(
                host=QB_HOST,
                port=QB_PORT,
                username=QB_USER,
                password=QB_PASS,
                REQUESTS_ARGS={'timeout': 30}
            )
            client.app_version()
            logger.info("✅ qBittorrent connected")
            
            # Optimize settings
            client.application.set_preferences({
                "max_active_downloads": MAX_PARALLEL,
                "max_active_torrents": MAX_PARALLEL,
                "max_active_uploads": 3,
                "max_connec": 100,
                "max_connec_per_torrent": 50,
                "async_io_threads": 8,
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True
            })
            return client
        except Exception as e:
            logger.warning(f"qBittorrent attempt {attempt + 1}/5 failed: {e}")
            if attempt < 4:
                asyncio.sleep(3)
    return None

aria2: Optional[aria2p.API] = None
qb: Optional[qbaClient] = None

# --- DOWNLOAD TRACKER ---
active_downloads: Dict[str, Dict[str, Any]] = {}

# --- UTILITIES ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_progress_bar(percentage: int) -> str:
    filled = int(percentage / 100 * 12)
    return "█" * filled + "░" * (12 - filled)

def format_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

def format_speed(speed: int) -> str:
    return format_bytes(speed) + "/s"

async def cleanup_download(gid: str):
    if gid in active_downloads:
        del active_downloads[gid]

# --- UPLOAD LOGIC ---
async def upload_file(event, file_path: Path):
    """Upload with progress and error recovery"""
    if not file_path.exists():
        return
    
    file_size = file_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        await event.reply(f"❌ File exceeds 2GB limit: {format_bytes(file_size)}")
        file_path.unlink()
        return

    is_video = file_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.webm']
    
    status_msg = await event.reply(
        f"📤 Uploading: `{file_path.name[:40]}`...\n"
        f"Size: {format_bytes(file_size)}"
    )

    async def progress_callback(current: int, total: int):
        try:
            percent = int(current / total * 100)
            if percent % 25 == 0:
                await status_msg.edit(
                    f"📤 Uploading: {get_progress_bar(percent)} {percent}%"
                )
        except:
            pass

    try:
        if is_video:
            await bot.send_file(
                event.chat_id,
                file_path,
                caption=f"`{file_path.name}`",
                supports_streaming=True,
                progress_callback=progress_callback
            )
        else:
            await bot.send_file(
                event.chat_id,
                file_path,
                caption=f"`{file_path.name}`",
                progress_callback=progress_callback
            )
        
        await status_msg.edit("✅ Upload complete!")
        await asyncio.sleep(1)
        file_path.unlink()
        await status_msg.delete()
        
    except FloodWaitError as e:
        logger.warning(f"Rate limited, waiting {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await upload_file(event, file_path)
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await status_msg.edit(f"❌ Upload error: {str(e)}")
        if file_path.exists():
            file_path.unlink()

# --- ARIA2 MONITOR ---
async def monitor_aria_download(gid: str, event):
    while gid in active_downloads:
        try:
            download = aria2.get_download(gid)
            
            if download.is_complete:
                file_path = Path(download.files[0].path)
                await event.edit("✅ Download complete! Uploading...")
                await upload_file(event, file_path)
                await cleanup_download(gid)
                break
            
            if download.error_message:
                await event.edit(f"❌ Error: {download.error_message}")
                await cleanup_download(gid)
                break
            
            if download.total_length > 0:
                percentage = int(download.completed_length / download.total_length * 100)
                await event.edit(
                    f"⬇️ Aria2: `{download.name[:40]}`\n"
                    f"{get_progress_bar(percentage)} {percentage}%\n"
                    f"Speed: {format_speed(download.download_speed)}"
                )
            
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Aria2 monitor error: {e}")
            await event.edit(f"❌ Monitor failed: {str(e)}")
            await cleanup_download(gid)
            break

# --- QBITTORRENT MONITOR ---
async def monitor_qb_download(torrent_hash: str, event):
    while torrent_hash in active_downloads:
        try:
            torrents = qb.torrents.info(torrent_hashes=torrent_hash)
            if not torrents:
                await cleanup_download(torrent_hash)
                break
            
            torrent = torrents[0]
            
            if torrent.state_enum.is_complete:
                await event.edit("✅ Download complete! Uploading...")
                
                save_path = Path(torrent.save_path)
                for file_info in qb.torrents_files(torrent_hash=torrent_hash):
                    file_path = save_path / file_info.name
                    if file_path.exists() and file_path.stat().st_size > 0:
                        await upload_file(event, file_path)
                
                qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
                await cleanup_download(torrent_hash)
                break
            
            if torrent.state_enum.errored:
                await event.edit(f"❌ Error: {torrent.state}")
                qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
                await cleanup_download(torrent_hash)
                break
            
            progress = torrent.progress * 100
            if progress > 0:
                await event.edit(
                    f"⬇️ qBittorrent: `{torrent.name[:40]}`\n"
                    f"{get_progress_bar(int(progress))} {progress:.1f}%\n"
                    f"Speed: {format_speed(torrent.dlspeed)}"
                )
            
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"qBittorrent monitor error: {e}")
            await event.edit(f"❌ Monitor failed: {str(e)}")
            qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
            await cleanup_download(torrent_hash)
            break

# --- BOT EVENT HANDLERS ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if not is_admin(event.sender_id):
        return
    
    await event.reply(
        "🤖 **Leech Bot Ready!**\n\n"
        "`/aria <url>` - aria2c download\n"
        "`/qb <magnet>` - qBittorrent\n"
        "`/cancel <id>` - Cancel\n"
        "`/status` - Active downloads\n"
        "`/stats` - System stats",
        buttons=[
            [Button.inline("📊 Status", b"status")],
            [Button.inline("📈 Stats", b"stats")]
        ]
    )

@bot.on(events.NewMessage(pattern='/aria'))
async def aria_handler(event):
    if not is_admin(event.sender_id) or not aria2:
        return
    
    try:
        link = event.raw_text.split(maxsplit=1)[1]
        status_msg = await event.reply("🔄 Processing...")
        
        download = aria2.add_magnet(link) if link.startswith('magnet:') else aria2.add_urlp(link)
        
        active_downloads[download.gid] = {'type': 'aria2', 'event': status_msg}
        asyncio.create_task(monitor_aria_download(download.gid, status_msg))
        
    except Exception as e:
        await event.reply(f"❌ Failed: {str(e)}")

@bot.on(events.NewMessage(pattern='/qb'))
async def qb_handler(event):
    if not is_admin(event.sender_id) or not qb:
        return
    
    try:
        link = event.raw_text.split(maxsplit=1)[1]
        if not link.startswith('magnet:'):
            await event.reply("❌ Only magnet links!")
            return
        
        status_msg = await event.reply("🔄 Processing...")
        
        qb.torrents.add(urls=link, save_path=str(DOWNLOAD_PATH))
        await asyncio.sleep(2)
        
        torrents = qb.torrents.info(sort_by='added_on', reverse=True, limit=1)
        if torrents:
            torrent_hash = torrents[0].hash
            active_downloads[torrent_hash] = {'type': 'qb', 'event': status_msg}
            asyncio.create_task(monitor_qb_download(torrent_hash, status_msg))
        else:
            await status_msg.edit("❌ Failed to add torrent")
            
    except Exception as e:
        await event.reply(f"❌ Failed: {str(e)}")

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_handler(event):
    if not is_admin(event.sender_id):
        return
    
    try:
        dl_id = event.raw_text.split(maxsplit=1)[1]
        if dl_id in active_downloads:
            if active_downloads[dl_id]['type'] == 'aria2':
                aria2.remove(dl_id, force=True)
            else:
                qb.torrents.delete(delete_files=True, torrent_hashes=dl_id)
            
            await event.reply(f"✅ Cancelled: `{dl_id}`")
            await cleanup_download(dl_id)
        else:
            await event.reply("❌ ID not found")
    except Exception as e:
        await event.reply(f"❌ Error: {str(e)}")

@bot.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    if not is_admin(event.sender_id):
        return
    
    if not active_downloads:
        await event.reply("📊 No active downloads")
        return
    
    text = "📊 **Active Downloads:**\n\n"
    for gid, info in active_downloads.items():
        text += f"`{gid}` ({info['type']})\n"
    
    await event.reply(text)

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    if not is_admin(event.sender_id):
        return
    
    text = "📈 **Stats:**\n\n"
    
    if aria2:
        try:
            stats = aria2.get_global_stats()
            text += f"**Aria2:** {stats.num_active} active\n"
        except:
            text += "**Aria2:** Offline\n"
    
    if qb:
        try:
            info = qb.transfer_info()
            text += f"**qBittorrent:** {len(qb.torrents.info())} torrents\n"
        except:
            text += "**qBittorrent:** Offline\n"
    
    await event.reply(text)

@bot.on(events.NewMessage(incoming=True, func=lambda e: e.document))
async def torrent_file_handler(event):
    if not is_admin(event.sender_id) or not qb:
        return
    
    if event.document.mime_type == "application/x-bittorrent":
        try:
            status_msg = await event.reply("📥 Processing torrent file...")
            
            torrent_path = DOWNLOAD_PATH / f"{event.document.id}.torrent"
            await event.download_media(file=torrent_path)
            
            qb.torrents.add(torrent_files=str(torrent_path), save_path=str(DOWNLOAD_PATH))
            torrent_path.unlink()
            
            await asyncio.sleep(2)
            torrents = qb.torrents.info(sort_by='added_on', reverse=True, limit=1)
            
            if torrents:
                torrent_hash = torrents[0].hash
                active_downloads[torrent_hash] = {'type': 'qb', 'event': status_msg}
                asyncio.create_task(monitor_qb_download(torrent_hash, status_msg))
            else:
                await status_msg.edit("❌ Failed")
                
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

# --- MAIN STARTUP ---
async def startup():
    """Initialize everything"""
    logger.info("=" * 60)
    logger.info("🚀 Initializing Telegram Leech Bot")
    logger.info("=" * 60)
    
    global aria2, qb
    
    # Initialize clients
    aria2 = init_aria2()
    qb = init_qbittorrent()
    
    if not aria2 and not qb:
        logger.error("❌ CRITICAL: No download clients available!")
        sys.exit(1)
    
    # Test Telegram connection
    try:
        me = await bot.get_me()
        logger.info(f"✅ Bot logged in as @{me.username}")
    except Exception as e:
        logger.error(f"❌ Telegram auth failed: {e}")
        sys.exit(1)
    
    logger.info(f"✅ Ready! Admin IDs: {ADMIN_IDS}")
    logger.info("=" * 60)

def graceful_shutdown(signum, frame):
    """Handle termination signals"""
    logger.info("Received shutdown signal")
    asyncio.create_task(shutdown())

async def shutdown():
    """Cleanup on exit"""
    logger.info("Cleaning up downloads...")
    for gid in list(active_downloads.keys()):
        await cleanup_download(gid)
    await bot.disconnect()
    logger.info("Shutdown complete")

# Signal handlers
signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)

# Run startup
with bot:
    bot.loop.run_until_complete(startup())
    bot.run_until_disconnected()
