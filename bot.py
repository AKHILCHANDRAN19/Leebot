import os
import asyncio
import tempfile
import aiofiles
import aria2p
from qbittorrentapi import Client as qbaClient
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from pathlib import Path

# --- CONFIGURATION ---
API_ID = int(os.getenv("API_ID", "2819362"))
API_HASH = os.getenv("API_HASH", "578ce3d09fadd539544a327c45b55ee4")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "123456789").split(',')))  # CHANGE THIS

# Aria2c config
ARIA2_RPC_HOST = os.getenv("ARIA2_RPC_HOST", "http://localhost")
ARIA2_RPC_PORT = int(os.getenv("ARIA2_RPC_PORT", "6800"))
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET", "your_secret")

# qBittorrent config
QB_HOST = os.getenv("QB_HOST", "http://localhost")
QB_PORT = int(os.getenv("QB_PORT", "8080"))
QB_USER = os.getenv("QB_USER", "admin")
QB_PASS = os.getenv("QB_PASS", "adminadmin")

# Bot config
DOWNLOAD_PATH = Path("/tmp/downloads")
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))

# --- CLIENTS ---
bot = TelegramClient('leech_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
aria2 = aria2p.Client(
    host=ARIA2_RPC_HOST,
    port=ARIA2_RPC_PORT,
    secret=ARIA2_RPC_SECRET
)
aria2_api = aria2p.API(aria2)

qb = qbaClient(
    host=QB_HOST,
    port=QB_PORT,
    username=QB_USER,
    password=QB_PASS
)

# --- ACTIVE DOWNLOADS ---
active_downloads = {}

# --- UTILS ---
def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_progress_bar(percentage):
    bar_length = 10
    filled = int(percentage / 100 * bar_length)
    return "█" * filled + "░" * (bar_length - filled)

async def upload_file(event, file_path):
    """Upload file to Telegram with progress"""
    file_path = Path(file_path)
    if not file_path.exists():
        return
    
    await event.reply(f"📤 Uploading: `{file_path.name}`")
    
    try:
        # Get file size
        file_size = file_path.stat().st_size
        
        # Upload with progress
        async def progress_callback(current, total):
            percent = int(current / total * 100)
            if percent % 20 == 0:  # Update every 20%
                await event.edit(f"📤 Uploading: `{file_path.name}`\n{get_progress_bar(percent)} {percent}%")
        
        # Check if it's a video
        is_video = file_path.suffix.lower() in ['.mp4', '.mkv', '.avi', '.mov']
        
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
        
        # Clean up after upload
        await asyncio.sleep(1)
        file_path.unlink()
        
    except Exception as e:
        await event.reply(f"❌ Upload failed: {str(e)}")

# --- ARIA2 HANDLERS ---
async def check_aria_progress(gid, event):
    """Monitor aria2c download progress"""
    try:
        while True:
            download = aria2_api.get_download(gid)
            
            if download.is_complete:
                file_path = Path(download.files[0].path)
                await event.edit("✅ Download complete! Starting upload...")
                await upload_file(event, file_path)
                break
            
            if download.error_message:
                await event.edit(f"❌ Aria2 error: {download.error_message}")
                break
            
            # Progress update
            if download.total_length > 0:
                percentage = int(download.completed_length / download.total_length * 100)
                speed = download.download_speed_string()
                size = download.total_length_string()
                
                await event.edit(
                    f"⬇️ **Aria2 Downloading**...\n"
                    f"`{download.name}`\n"
                    f"{get_progress_bar(percentage)} {percentage}%\n"
                    f"Speed: {speed} | Size: {size}"
                )
            
            await asyncio.sleep(3)
            
    except Exception as e:
        await event.edit(f"❌ Progress check failed: {str(e)}")

# --- QBITTORRENT HANDLERS ---
async def check_qb_progress(torrent_hash, event):
    """Monitor qBittorrent download progress"""
    try:
        while True:
            torrent = qb.torrents.info(torrent_hashes=torrent_hash)[0]
            
            if torrent.state_enum.is_complete:
                # Get files
                files = qb.torrents_files(torrent_hash=torrent_hash)
                save_path = torrent.save_path
                
                await event.edit("✅ qBittorrent download complete! Starting upload...")
                
                # Upload all files
                for file in files:
                    file_path = Path(save_path) / file.name
                    if file_path.exists():
                        await upload_file(event, file_path)
                
                # Remove torrent
                qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
                break
            
            if torrent.state_enum.errored:
                await event.edit(f"❌ qBittorrent error: {torrent.state}")
                qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)
                break
            
            # Progress update
            if torrent.total_size > 0:
                percentage = int(torrent.downloaded / torrent.total_size * 100)
                speed = torrent.dlspeed
                size = torrent.total_size
                
                # Convert speed to human readable
                if speed > 1024*1024:
                    speed_str = f"{speed/(1024*1024):.2f} MB/s"
                elif speed > 1024:
                    speed_str = f"{speed/1024:.2f} KB/s"
                else:
                    speed_str = f"{speed} B/s"
                
                # Convert size
                if size > 1024*1024*1024:
                    size_str = f"{size/(1024**3):.2f} GB"
                elif size > 1024*1024:
                    size_str = f"{size/(1024**2):.2f} MB"
                else:
                    size_str = f"{size/1024:.2f} KB"
                
                await event.edit(
                    f"⬇️ **qBittorrent Downloading**...\n"
                    f"`{torrent.name}`\n"
                    f"{get_progress_bar(percentage)} {percentage}%\n"
                    f"Speed: {speed_str} | Size: {size_str}\n"
                    f"Seeds: {torrent.num_leechs} | Leechers: {torrent.num_leechs}"
                )
            
            await asyncio.sleep(5)
            
    except Exception as e:
        await event.edit(f"❌ qBittorrent progress check failed: {str(e)}")
        qb.torrents.delete(delete_files=True, torrent_hashes=torrent_hash)

# --- BOT COMMANDS ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    if not is_admin(event.sender_id):
        return
    
    await event.reply(
        "🤖 **Leech Bot is Ready!**\n\n"
        "Commands:\n"
        "/aria <magnet/torrent> - Download with aria2c\n"
        "/qb <magnet/torrent> - Download with qBittorrent\n"
        "/cancel <gid> - Cancel aria2 download\n"
        "/status - Show active downloads\n"
        "/stats - Show system stats"
    )

@bot.on(events.NewMessage(pattern='/aria'))
async def aria_command(event):
    if not is_admin(event.sender_id):
        return
    
    # Get URL/magnet
    message_text = event.raw_text
    if len(message_text.split()) < 2:
        await event.reply("❌ Provide a magnet link or URL!")
        return
    
    link = message_text.split(maxsplit=1)[1]
    
    status_msg = await event.reply("🔄 Processing with aria2c...")
    
    try:
        # Add download
        download = aria2_api.add_magnet(link) if link.startswith('magnet:') else aria2_api.add_urlp(link)
        
        # Store active download
        active_downloads[download.gid] = {
            'type': 'aria2',
            'event': status_msg
        }
        
        # Start monitoring
        asyncio.create_task(check_aria_progress(download.gid, status_msg))
        
    except Exception as e:
        await status_msg.edit(f"❌ Failed to add download: {str(e)}")

@bot.on(events.NewMessage(pattern='/qb'))
async def qb_command(event):
    if not is_admin(event.sender_id):
        return
    
    # Get URL/magnet
    message_text = event.raw_text
    if len(message_text.split()) < 2:
        await event.reply("❌ Provide a magnet link or torrent file!")
        return
    
    link = message_text.split(maxsplit=1)[1]
    
    status_msg = await event.reply("🔄 Processing with qBittorrent...")
    
    try:
        # Add torrent
        if link.startswith('magnet:'):
            torrent = qb.torrents.add(urls=link, save_path=str(DOWNLOAD_PATH))
        else:
            # For .torrent files, you'd need to download first
            await status_msg.edit("❌ Direct .torrent file links not supported yet. Use magnets.")
            return
        
        # Get torrent hash
        await asyncio.sleep(2)  # Wait for torrent to appear
        torrents = qb.torrents.info(sort_by='added_on', reverse=True)
        if torrents:
            torrent_hash = torrents[0].hash
            
            # Store active download
            active_downloads[torrent_hash] = {
                'type': 'qb',
                'event': status_msg
            }
            
            # Start monitoring
            asyncio.create_task(check_qb_progress(torrent_hash, status_msg))
        else:
            await status_msg.edit("❌ Failed to add torrent to qBittorrent")
        
    except Exception as e:
        await status_msg.edit(f"❌ qBittorrent error: {str(e)}")

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_command(event):
    if not is_admin(event.sender_id):
        return
    
    message_text = event.raw_text
    if len(message_text.split()) < 2:
        await event.reply("❌ Provide a GID or torrent hash!")
        return
    
    gid = message_text.split(maxsplit=1)[1]
    
    if gid in active_downloads:
        dl_info = active_downloads[gid]
        if dl_info['type'] == 'aria2':
            aria2_api.remove(gid, force=True)
        elif dl_info['type'] == 'qb':
            qb.torrents.delete(delete_files=True, torrent_hashes=gid)
        
        del active_downloads[gid]
        await event.reply(f"✅ Cancelled: {gid}")
    else:
        await event.reply("❌ Download not found!")

@bot.on(events.NewMessage(pattern='/status'))
async def status_command(event):
    if not is_admin(event.sender_id):
        return
    
    if not active_downloads:
        await event.reply("📊 **No active downloads**")
        return
    
    status_text = "📊 **Active Downloads:**\n\n"
    for gid, info in active_downloads.items():
        status_text += f"**{gid}** ({info['type']})\n"
    
    await event.reply(status_text)

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_command(event):
    if not is_admin(event.sender_id):
        return
    
    # Aria2 stats
    aria2_info = aria2_api.get_global_stats()
    aria_stats = (
        f"📈 **Aria2 Stats:**\n"
        f"Active: {aria2_info.num_active}\n"
        f"Waiting: {aria2_info.num_waiting}\n"
        f"Download Speed: {aria2_info.download_speed_string()}\n\n"
    )
    
    # qBittorrent stats
    qb_stats = qb.transfer_info()
    qb_info = (
        f"📈 **qBittorrent Stats:**\n"
        f"Active Torrents: {len(qb.torrents.info())}\n"
        f"Download Speed: {qb_stats['dl_info_speed'] / 1024:.2f} KB/s\n"
        f"Upload Speed: {qb_stats['up_info_speed'] / 1024:.2f} KB/s\n"
    )
    
    await event.reply(aria_stats + qb_info)

# --- FILE HANDLER ---
@bot.on(events.NewMessage(incoming=True))
async def file_handler(event):
    if not is_admin(event.sender_id):
        return
    
    # Handle .torrent files
    if event.document and event.document.mime_type == "application/x-bittorrent":
        status_msg = await event.reply("📥 Downloading .torrent file...")
        
        # Download file
        torrent_path = DOWNLOAD_PATH / f"{event.document.id}.torrent"
        await event.download_media(file=torrent_path)
        
        await status_msg.edit("🔄 Adding to qBittorrent...")
        
        try:
            # Add to qBittorrent
            qb.torrents.add(torrent_files=str(torrent_path), save_path=str(DOWNLOAD_PATH))
            torrent_path.unlink()  # Remove torrent file
            
            # Get hash and monitor
            await asyncio.sleep(2)
            torrents = qb.torrents.info(sort_by='added_on', reverse=True)
            if torrents:
                torrent_hash = torrents[0].hash
                active_downloads[torrent_hash] = {
                    'type': 'qb',
                    'event': status_msg
                }
                asyncio.create_task(check_qb_progress(torrent_hash, status_msg))
            else:
                await status_msg.edit("❌ Failed to add torrent")
                
        except Exception as e:
            await status_msg.edit(f"❌ Error: {str(e)}")
            if torrent_path.exists():
                torrent_path.unlink()

# --- STARTUP ---
async def startup():
    """Initialize clients and create directories"""
    print("🚀 Starting Leech Bot...")
    
    # Create download directory
    DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
    
    # Test aria2 connection
    try:
        aria2_api.get_version()
        print("✅ Aria2c connected")
    except Exception as e:
        print(f"⚠️ Aria2c connection failed: {e}. Start aria2c with: aria2c --enable-rpc --rpc-listen-all --rpc-secret=your_secret")
    
    # Test qBittorrent connection
    try:
        qb.app_version()
        print("✅ qBittorrent connected")
        # Configure qBittorrent
        qb.application.set_preferences({
            "max_active_downloads": MAX_PARALLEL,
            "max_active_torrents": MAX_PARALLEL,
            "max_active_uploads": 3
        })
    except Exception as e:
        print(f"⚠️ qBittorrent connection failed: {e}. Ensure qBittorrent is running with Web UI enabled")
    
    print(f"✅ Bot is ready! Admin IDs: {ADMIN_IDS}")

# Run startup
loop = asyncio.get_event_loop()
loop.run_until_complete(startup())

# Start bot
print("📡 Bot is running...")
bot.run_until_disconnected()
