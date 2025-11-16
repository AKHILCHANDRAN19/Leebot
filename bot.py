#!/usr/bin/env python3
import os
import asyncio
import sys
from pathlib import Path
from typing import Optional
import aria2p
from qbittorrentapi import Client as qbaClient, LoginFailed, APIConnectionError
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from telethon.errors import RPCError, FloodWaitError

# --- CONFIGURATION ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(',')))

# Download path (Render mounts disk here)
DOWNLOAD_PATH = Path("/tmp/downloads")
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB Telegram limit

# Aria2 config
ARIA2_RPC_HOST = os.getenv("ARIA2_RPC_HOST", "http://localhost")
ARIA2_RPC_PORT = int(os.getenv("ARIA2_RPC_PORT", "6800"))
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET")

# qBittorrent config
QB_HOST = os.getenv("QB_HOST", "http://localhost")
QB_PORT = int(os.getenv("QB_PORT", "8080"))
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS")

# --- CLIENTS ---
bot = TelegramClient('leech_bot', API_ID, API_HASH)

# Initialize with retry
def init_aria2() -> Optional[aria2p.API]:
    """Initialize aria2c with connection retry"""
    for attempt in range(3):
        try:
            client = aria2p.Client(
                host=ARIA2_RPC_HOST,
                port=ARIA2_RPC_PORT,
                secret=ARIA2_RPC_SECRET,
                timeout=30
            )
            api = aria2p.API(client)
            api.get_version()
            print("✅ Aria2c connected successfully")
            return api
        except Exception as e:
            print(f"⚠️ Aria2c connection attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                asyncio.sleep(2)
    return None

def init_qbittorrent() -> Optional[qbaClient]:
    """Initialize qBittorrent with connection retry"""
    for attempt in range(3):
        try:
            client = qbaClient(
                host=QB_HOST,
                port=QB_PORT,
                username=QB_USER,
                password=QB_PASS,
                REQUESTS_ARGS={'timeout': 30}
            )
            client.app_version()
            print("✅ qBittorrent connected successfully")
            
            # Optimize settings
            client.application.set_preferences({
                "max_active_downloads": MAX_PARALLEL,
                "max_active_torrents": MAX_PARALLEL,
                "max_active_uploads": 3,
                "max_connec": 100,
                "max_connec_per_torrent": 50,
                "async_io_threads": 8,
                "enable_dht": True,
                "enable_lsd": True
            })
            return client
        except (LoginFailed, APIConnectionError) as e:
            print(f"⚠️ qBittorrent connection attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                asyncio.sleep(2)
    return None

aria2 = None
qb = None

# --- ACTIVE DOWNLOADS TRACKER ---
active_downloads = {}

# --- UTILITY FUNCTIONS ---
def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_IDS

def get_progress_bar(percentage: int) -> str:
    """Generate visual progress bar"""
    bar_length = 12
    filled = int(percentage / 100 * bar_length)
    return "█" * filled + "░" * (bar_length - filled)

def format_bytes(size: int) -> str:
    """Convert bytes to human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

def format_speed(speed: int) -> str:
    """Convert bytes/s to human-readable speed"""
    return format_bytes(speed) + "/s"

async def cleanup_download(gid: str):
    """Remove completed download from tracker"""
    if gid in active_downloads:
        del active_downloads[gid]

# --- UPLOAD HANDLER ---
async def upload_file(event, file_path: Path):
    """Upload file to Telegram with progress and validation"""
    try:
        if not file_path.exists():
            await event.reply(f"❌ File not found: `{file_path.name}`")
            return

        file_size = file_path.stat().st_size
        
        # Check Telegram file size limit
        if file_size > MAX_FILE_SIZE:
            await event.reply(f"❌ File too large ({format_bytes(file_size)}) > 2GB Telegram limit")
            file_path.unlink()
            return

        # Check if it's a video
        is_video = file_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov', '.webm']
        
        status_msg = await event.reply(
            f"📤 **Uploading**...\n"
            f"`{file_path.name}`\n"
            f"Size: {format_bytes(file_size)}"
        )

        # Upload with progress
        async def progress_callback(current: int, total: int):
            percent = int(current / total * 100)
            if percent % 25 == 0:  # Update every 25%
                try:
                    await status_msg.edit(
                        f"📤 **Uploading**...\n"
                        f"`{file_path.name}`\n"
                        f"{get_progress_bar(percent)} {percent}%"
                    )
                except:
                    pass  # Ignore edit errors

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

        # Clean up
        await status_msg.edit("✅ Upload complete! Cleaning up...")
        await asyncio.sleep(1)
        file_path.unlink()
        await status_msg.delete()
        
    except FloodWaitError as e:
        await event.reply(f"⚠️ Rate limited by Telegram. Waiting {e.seconds}s...")
        await asyncio.sleep(e.seconds)
        await upload_file(event, file_path)  # Retry
    except RPCError as e:
        await event.reply(f"❌ Telegram error: {e}")
    except Exception as e:
        await event.reply(f"❌ Upload failed: {str(e)}")
        if file_path.exists():
            file_path.unlink()

# --- ARIA2 PROGRESS MONITOR ---
async def monitor_aria_download(gid: str, event):
    """Monitor aria2c download progress"""
    try:
        while True:
            if gid not in active_downloads:
                return
                
            download = aria2.get_download(gid)
            
            if download.is_complete:
                file_path = Path(download.files[0].path)
                await event.edit("✅ Download complete! Starting upload...")
                await upload_file(event, file_path)
                await cleanup_download(gid)
                break
            
            if download.error_message:
                await event.edit(f"❌ Aria2 error: `{download.error_message}`")
                await cleanup_download(gid)
                break
            
            # Progress update
            if download.total_length > 0:
                percentage = int(download.completed_length / download.total_length * 100)
                await event.edit(
                    f"⬇️ **Aria2 Downloading**...\n"
                    f"`{download.name[:50]}{'...' if len(download.name) > 50 else ''}`\n"
                    f"{get_progress_bar(percentage)} **{percentage}%**\n"
                    f"Speed: `{format_speed(download.download_speed)}` | "
                    f"Size: `{download.total_length_string()}`"
                )
            
            await asyncio.sleep(2)
            
    except Exception as e:
        await event.edit(f"❌ Monitor error: `{str(e)}`")
        await cleanup_download(gid)

# --- QBITTORRENT PROGRESS MONITOR ---
async def monitor_qb_download(torrent_hash: str, event):
    """Monitor qBittorrent download progress"""
    try:
        while True:
            if torrent_hash not in active_downloads:
                return
                
            torrents = qb.torrents.info(torrent_hashes=torrent_hash)
            if not torrents:
                await event.edit("❌ Torrent not found")
                await cleanup_download(torrent_hash)
                return
                
            torrent = torrents[0]
            
            if torrent.state_enum.is_complete:
                save_path = Path(torrent.save_path)
                files = qb.torrents_files(torrent_hash=torrent_hash)
                
                await event.edit("✅ qBittorrent download complete! Starting upload...")
                
                # Upload all files
                for file_info in files:
                    file_path = save_path / file_info.name
                    if file_path.exists() and file_path.stat().st_size > 0:
                        await upload_file(event, file_path)
                
                # Clean up torrent
                qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
                await cleanup_download(torrent_hash)
                break
            
            if torrent.state_enum.errored:
                await event.edit(f"❌ qBittorrent error: `{torrent.state}`")
                qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
                await cleanup_download(torrent_hash)
                break
            
            # Progress update
            progress = torrent.progress * 100
            if progress > 0:
                await event.edit(
                    f"⬇️ **qBittorrent Downloading**...\n"
                    f"`{torrent.name[:50]}{'...' if len(torrent.name) > 50 else ''}`\n"
                    f"{get_progress_bar(int(progress))} **{int(progress)}%**\n"
                    f"Speed: `{format_speed(torrent.dlspeed)}` | "
                    f"Size: `{format_bytes(torrent.total_size)}`\n"
                    f"Seeds: {torrent.num_seeds} | Peers: {torrent.num_leechs}"
                )
            
            await asyncio.sleep(3)
            
    except Exception as e:
        await event.edit(f"❌ Monitor error: `{str(e)}`")
        qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
        await cleanup_download(torrent_hash)

# --- BOT COMMANDS ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    """Start command - only for admins"""
    if not is_admin(event.sender_id):
        return
    
    await event.reply(
        "🤖 **Leech Bot Ready!**\n\n"
        "**Commands:**\n"
        "`/aria <magnet/url>` - Download with aria2c\n"
        "`/qb <magnet>` - Download with qBittorrent\n"
        "`/cancel <id>` - Cancel download\n"
        "`/status` - Show active downloads\n"
        "`/stats` - Show system stats\n\n"
        "**Just send a .torrent file** to auto-download with qBittorrent",
        buttons=[
            [Button.inline("📊 Status", b"status")],
            [Button.inline("📈 Stats", b"stats")]
        ]
    )

@bot.on(events.NewMessage(pattern='/aria'))
async def aria_command(event):
    """Add download to aria2c"""
    if not is_admin(event.sender_id):
        return
    
    try:
        message_text = event.raw_text
        if len(message_text.split()) < 2:
            await event.reply("❌ Provide a valid magnet link or URL!")
            return
        
        link = message_text.split(maxsplit=1)[1]
        if not (link.startswith('magnet:') or link.startswith('http')):
            await event.reply("❌ Invalid link format!")
            return
        
        status_msg = await event.reply("🔄 **Processing with aria2c...**")
        
        # Add download
        download = aria2.add_magnet(link) if link.startswith('magnet:') else aria2.add_urlp(link)
        
        # Store in tracker
        active_downloads[download.gid] = {
            'type': 'aria2',
            'event': status_msg
        }
        
        # Start monitor
        asyncio.create_task(monitor_aria_download(download.gid, status_msg))
        
    except Exception as e:
        await event.reply(f"❌ Failed: `{str(e)}`")

@bot.on(events.NewMessage(pattern='/qb'))
async def qb_command(event):
    """Add torrent to qBittorrent"""
    if not is_admin(event.sender_id):
        return
    
    try:
        message_text = event.raw_text
        if len(message_text.split()) < 2:
            await event.reply("❌ Provide a valid magnet link!")
            return
        
        link = message_text.split(maxsplit=1)[1]
        if not link.startswith('magnet:'):
            await event.reply("❌ Only magnet links supported for qBittorrent!")
            return
        
        status_msg = await event.reply("🔄 **Processing with qBittorrent...**")
        
        # Add torrent
        qb.torrents.add(urls=link, save_path=str(DOWNLOAD_PATH))
        
        # Wait for torrent to appear
        await asyncio.sleep(2)
        torrents = qb.torrents.info(sort_by='added_on', reverse=True, limit=1)
        
        if not torrents:
            await status_msg.edit("❌ Failed to add torrent")
            return
            
        torrent_hash = torrents[0].hash
        active_downloads[torrent_hash] = {
            'type': 'qb',
            'event': status_msg
        }
        
        # Start monitor
        asyncio.create_task(monitor_qb_download(torrent_hash, status_msg))
        
    except Exception as e:
        await event.reply(f"❌ Failed: `{str(e)}`")

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_command(event):
    """Cancel active download"""
    if not is_admin(event.sender_id):
        return
    
    try:
        message_text = event.raw_text
        if len(message_text.split()) < 2:
            await event.reply("❌ Provide download ID!")
            return
        
        dl_id = message_text.split(maxsplit=1)[1]
        
        if dl_id in active_downloads:
            dl_info = active_downloads[dl_id]
            if dl_info['type'] == 'aria2':
                aria2.remove(dl_id, force=True)
            else:
                qb.torrents.delete(delete_files=True, torrent_hashes=dl_id)
            
            await event.reply(f"✅ Cancelled: `{dl_id}`")
            await cleanup_download(dl_id)
        else:
            await event.reply("❌ Download ID not found!")
            
    except Exception as e:
        await event.reply(f"❌ Cancel failed: `{str(e)}`")

@bot.on(events.NewMessage(pattern='/status'))
async def status_command(event):
    """Show active downloads"""
    if not is_admin(event.sender_id):
        return
    
    if not active_downloads:
        await event.reply("📊 **No active downloads**")
        return
    
    status_text = "📊 **Active Downloads:**\n\n"
    for gid, info in active_downloads.items():
        status_text += f"📥 `{gid}` ({info['type']})\n"
    
    await event.reply(status_text)

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_command(event):
    """Show system statistics"""
    if not is_admin(event.sender_id):
        return
    
    stats_text = "📈 **System Stats:**\n\n"
    
    # Aria2 stats
    if aria2:
        try:
            aria_stats = aria2.get_global_stats()
            stats_text += (
                f"**Aria2:**\n"
                f"Active: {aria_stats.num_active}\n"
                f"Waiting: {aria_stats.num_waiting}\n"
                f"Speed: {format_speed(aria_stats.download_speed)}\n\n"
            )
        except:
            stats_text += "**Aria2:** Not connected\n\n"
    
    # qBittorrent stats
    if qb:
        try:
            qb_stats = qb.transfer_info()
            stats_text += (
                f"**qBittorrent:**\n"
                f"Active: {len(qb.torrents.info())}\n"
                f"DL Speed: {format_speed(qb_stats['dl_info_speed'])}\n"
                f"UL Speed: {format_speed(qb_stats['up_info_speed'])}\n"
            )
        except:
            stats_text += "**qBittorrent:** Not connected\n"
    
    await event.reply(stats_text)

# --- FILE HANDLER FOR .TORRENT FILES ---
@bot.on(events.NewMessage(incoming=True, func=lambda e: e.document))
async def torrent_file_handler(event):
    """Handle .torrent files"""
    if not is_admin(event.sender_id):
        return
    
    if event.document.mime_type == "application/x-bittorrent":
        status_msg = await event.reply("📥 **Receiving torrent file...**")
        
        torrent_path = DOWNLOAD_PATH / f"{event.document.id}.torrent"
        
        try:
            await event.download_media(file=torrent_path)
            await status_msg.edit("🔄 **Adding to qBittorrent...**")
            
            qb.torrents.add(torrent_files=str(torrent_path), save_path=str(DOWNLOAD_PATH))
            torrent_path.unlink()
            
            await asyncio.sleep(2)
            torrents = qb.torrents.info(sort_by='added_on', reverse=True, limit=1)
            
            if torrents:
                torrent_hash = torrents[0].hash
                active_downloads[torrent_hash] = {
                    'type': 'qb',
                    'event': status_msg
                }
                asyncio.create_task(monitor_qb_download(torrent_hash, status_msg))
            else:
                await status_msg.edit("❌ Failed to process torrent file")
                
        except Exception as e:
            await status_msg.edit(f"❌ Error: `{str(e)}`")
            if torrent_path.exists():
                torrent_path.unlink()

# --- BUTTON HANDLERS ---
@bot.on(events.CallbackQuery(data=b"status"))
async def status_callback(event):
    await status_command(event)

@bot.on(events.CallbackQuery(data=b"stats"))
async def stats_callback(event):
    await stats_command(event)

# --- STARTUP FUNCTION ---
async def startup():
    """Initialize bot and connections"""
    print("=" * 50)
    print("🚀 Starting Telegram Leech Bot...")
    print("=" * 50)
    
    global aria2, qb
    
    # Create download directory
    DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
    print(f"📁 Download path: {DOWNLOAD_PATH}")
    print(f"💾 Free space: {format_bytes(DOWNLOAD_PATH.stat().st_size)}")
    
    # Initialize clients
    aria2 = init_aria2()
    qb = init_qbittorrent()
    
    if not aria2 and not qb:
        print("❌ CRITICAL: No download clients available!")
        sys.exit(1)
    
    print(f"✅ Bot initialized! Admin IDs: {ADMIN_IDS}")
    print("📡 Bot is running and ready for commands...")
    print("=" * 50)

# --- SHUTDOWN HANDLER ---
async def shutdown():
    """Graceful shutdown"""
    print("\n🛑 Shutting down...")
    
    # Cancel all active downloads
    for gid in list(active_downloads.keys()):
        try:
            if active_downloads[gid]['type'] == 'aria2':
                aria2.remove(gid, force=True)
            else:
                qb.torrents.delete(delete_files=True, torrent_hashes=gid)
        except:
            pass
    
    await bot.disconnect()
    print("✅ Shutdown complete")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(startup())
        
        # Start the bot
        bot.run_until_disconnected()
    except KeyboardInterrupt:
        loop.run_until_complete(shutdown())
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        sys.exit(1)
