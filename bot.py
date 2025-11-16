import os
import time
import threading
import asyncio
import libtorrent as lt
import logging
import subprocess
import shutil
import re
import math
import psutil
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified
from pathlib import Path
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from humanfriendly import format_size, parse_size

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram API credentials
API_ID = 2819362
API_HASH = "578ce3d09fadd539544a327c45b55ee4"
BOT_TOKEN = "8290220435:AAHluT9Ns8ydCN9cC6qLpFkoCAK-EmhXpD0"

# Directories
DOWNLOAD_DIR = "./downloads"
EXTRACT_DIR = "./extracted"
UPLOAD_DIR = "./upload"
Path(DOWNLOAD_DIR).mkdir(exist_ok=True)
Path(EXTRACT_DIR).mkdir(exist_ok=True)
Path(UPLOAD_DIR).mkdir(exist_ok=True)

# Bot configuration
OWNER_ID = 0  # Replace with your Telegram user ID
MAX_FILE_SIZE = 2097152000  # 2GB (Telegram's limit for premium users)
CHUNK_SIZE = 1024 * 1024 * 5  # 5MB chunks for splitting
SPEED_LIMIT = 0  # 0 means no speed limit (in bytes per second)
LEECH_LOG_GROUP = None  # Replace with your log group ID if you want to log downloads

# User data
users = {}
downloads = {}
uploads = {}

# Initialize Pyrogram client
app = Client(
    "leech_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Helper functions
def get_size(size):
    """Convert bytes to human readable format"""
    if not size:
        return "0B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power and n < len(power_labels):
        size /= power
        n += 1
    return f"{size:.2f}{power_labels[n]}B"

def get_progress_bar(progress):
    """Generate a progress bar"""
    bar_length = 15
    filled_length = int(round(bar_length * progress / 100))
    bar = '█' * filled_length + '▁' * (bar_length - filled_length)
    return bar

def is_magnet(link):
    """Check if link is a magnet link"""
    return link.startswith('magnet:')

def is_url(link):
    """Check if link is a URL"""
    url_regex = re.compile(
        r'^(?:http|ftp)s?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_regex.match(link) is not None

def extract_archive(file_path, extract_to):
    """Extract archives in various formats"""
    try:
        if file_path.endswith('.zip'):
            subprocess.run(['unzip', '-o', file_path, '-d', extract_to], check=True)
        elif file_path.endswith('.rar'):
            subprocess.run(['unrar', 'x', '-o+', file_path, extract_to], check=True)
        elif file_path.endswith('.tar') or file_path.endswith('.tar.gz') or file_path.endswith('.tgz'):
            subprocess.run(['tar', '-xvf', file_path, '-C', extract_to], check=True)
        elif file_path.endswith('.tar.bz2') or file_path.endswith('.tbz2'):
            subprocess.run(['tar', '-xvjf', file_path, '-C', extract_to], check=True)
        elif file_path.endswith('.tar.xz') or file_path.endswith('.txz'):
            subprocess.run(['tar', '-xvJf', file_path, '-C', extract_to], check=True)
        elif file_path.endswith('.7z'):
            subprocess.run(['7z', 'x', file_path, f'-o{extract_to}'], check=True)
        else:
            return False
        return True
    except Exception as e:
        logger.error(f"Error extracting archive: {e}")
        return False

def split_file(file_path, chunk_size):
    """Split large files into chunks"""
    file_size = os.path.getsize(file_path)
    parts = math.ceil(file_size / chunk_size)
    
    with open(file_path, 'rb') as f:
        for i in range(parts):
            chunk = f.read(chunk_size)
            with open(f"{file_path}.part{i+1:03d}", 'wb') as chunk_file:
                chunk_file.write(chunk)
    
    return [f"{file_path}.part{i+1:03d}" for i in range(parts)]

def get_media_metadata(file_path):
    """Get metadata for media files"""
    try:
        parser = createParser(file_path)
        if not parser:
            return None
        
        metadata = extractMetadata(parser)
        return metadata
    except Exception as e:
        logger.error(f"Error getting metadata: {e}")
        return None

async def download_torrent(magnet_link, message, user_id):
    """Download torrent/magnet link using libtorrent"""
    try:
        ses = lt.session()
        ses.listen_port(6881)
        ses.start_dht()
        
        # Optimize settings for faster downloads
        settings = {
            'active_downloads': 10,
            'active_seeds': 5,
            'active_limit': 20,
            'connections_limit': 200,
            'download_rate_limit': SPEED_LIMIT,
            'upload_rate_limit': -1,  # Unlimited upload
            'cache_size': 100,  # Cache in MB
        }
        ses.apply_settings(settings)
        
        params = {
            'save_path': DOWNLOAD_DIR,
            'storage_mode': lt.storage_mode_t(2),
            'paused': False,
            'auto_managed': True,
            'duplicate_is_error': True
        }
        
        handle = lt.add_magnet_uri(ses, magnet_link, params)
        
        # Initial status message
        status_message = await message.reply("📥 Starting download...")
        
        # Wait for metadata to download
        while not handle.has_metadata():
            status = handle.status()
            progress_text = (
                f"📥 Downloading metadata...\n"
                f"⚡ Speed: {get_size(status.download_rate)}/s\n"
                f"📊 Peers: {status.num_peers}"
            )
            try:
                await status_message.edit_text(progress_text)
            except MessageNotModified:
                pass
            await asyncio.sleep(3)
        
        torrent_info = handle.get_torrent_info()
        torrent_name = torrent_info.name()
        torrent_size = torrent_info.total_size()
        
        # Update status with torrent info
        progress_text = (
            f"📥 {torrent_name}\n"
            f"📏 Size: {get_size(torrent_size)}\n"
            f"⏳ Downloading..."
        )
        await status_message.edit_text(progress_text)
        
        # Download progress
        last_update_time = time.time()
        start_time = time.time()
        
        while handle.status().state != lt.torrent_status.finished:
            if user_id not in downloads:
                break
                
            s = handle.status()
            progress = int(s.progress * 100)
            speed = s.download_rate
            eta = s.time_remaining if s.time_remaining != lt.seconds(-1) else 0
            
            # Update progress every 3 seconds
            current_time = time.time()
            if current_time - last_update_time >= 3:
                last_update_time = current_time
                
                elapsed = current_time - start_time
                progress_text = (
                    f"📥 {torrent_name}\n"
                    f"📊 Progress: {progress}% {get_progress_bar(progress)}\n"
                    f"⚡ Speed: {get_size(speed)}/s\n"
                    f"⏱️ ETA: {eta//60}:{eta%60:02d}\n"
                    f"📏 Size: {get_size(torrent_size)}\n"
                    f"✅ Downloaded: {get_size(s.total_done)}\n"
                    f"🌐 Peers: {s.num_peers}"
                )
                try:
                    await status_message.edit_text(progress_text)
                except MessageNotModified:
                    pass
            
            await asyncio.sleep(1)
        
        if user_id in downloads:
            downloads[user_id]['status'] = 'completed'
            await status_message.edit_text(f"✅ Download completed: {torrent_name}")
            
            # Get downloaded files
            files = []
            for file_index, file_info in enumerate(torrent_info.files()):
                file_path = os.path.join(DOWNLOAD_DIR, torrent_name, file_info.path)
                if os.path.exists(file_path) and os.path.isfile(file_path):
                    files.append(file_path)
            
            return files
        else:
            await status_message.edit_text("❌ Download cancelled.")
            return None
            
    except Exception as e:
        logger.error(f"Error downloading torrent: {e}")
        await message.reply(f"❌ Error downloading torrent: {str(e)}")
        return None

async def download_direct_link(url, message, user_id):
    """Download file from direct link using aria2c"""
    try:
        file_name = url.split('/')[-1]
        if not file_name:
            file_name = "downloaded_file"
            
        file_path = os.path.join(DOWNLOAD_DIR, file_name)
        
        # Initial status message
        status_message = await message.reply("📥 Starting download...")
        
        # Use aria2c for faster downloads
        cmd = [
            'aria2c', 
            '-x', '16',  # Max connections per server
            '-s', '16',  # Split file into N pieces
            '--max-connection-per-server', '16',
            '--split', '16',
            '--min-split-size', '1M',
            '--continue', 'true',
            '--max-tries', '0',
            '--retry-wait', '5',
            '--timeout', '60',
            '--check-certificate', 'false',
            '-d', DOWNLOAD_DIR,
            '-o', file_name,
            url
        ]
        
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        # Monitor download progress
        last_update_time = time.time()
        start_time = time.time()
        
        while process.poll() is None:
            if user_id not in downloads:
                process.terminate()
                break
                
            # Try to get file size and downloaded amount
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                elapsed = time.time() - start_time
                speed = file_size / elapsed if elapsed > 0 else 0
                
                # Update progress every 3 seconds
                current_time = time.time()
                if current_time - last_update_time >= 3:
                    last_update_time = current_time
                    
                    progress_text = (
                        f"📥 {file_name}\n"
                        f"📊 Downloaded: {get_size(file_size)}\n"
                        f"⚡ Speed: {get_size(speed)}/s\n"
                        f"⏱️ Time: {int(elapsed)}s"
                    )
                    try:
                        await status_message.edit_text(progress_text)
                    except MessageNotModified:
                        pass
            
            await asyncio.sleep(1)
        
        # Check if download completed
        if process.returncode == 0 and user_id in downloads:
            downloads[user_id]['status'] = 'completed'
            await status_message.edit_text(f"✅ Download completed: {file_name}")
            return [file_path]
        elif user_id not in downloads:
            await status_message.edit_text("❌ Download cancelled.")
            return None
        else:
            stderr = process.stderr.read()
            await status_message.edit_text(f"❌ Error downloading file: {stderr}")
            return None
            
    except Exception as e:
        logger.error(f"Error downloading direct link: {e}")
        await message.reply(f"❌ Error downloading file: {str(e)}")
        return None

async def upload_file(client, message, file_path, caption=None):
    """Upload file to Telegram with progress tracking"""
    try:
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)
        
        # Check if file is too large
        if file_size > MAX_FILE_SIZE:
            await message.reply(f"❌ File too large: {file_name} ({get_size(file_size)})\nSplitting into parts...")
            
            # Split file and upload parts
            parts = split_file(file_path, CHUNK_SIZE)
            for part_path in parts:
                part_name = os.path.basename(part_path)
                part_caption = f"Part {parts.index(part_path)+1} of {len(parts)}\nOriginal: {file_name}"
                
                await client.send_document(
                    chat_id=message.chat.id,
                    document=part_path,
                    caption=part_caption,
                    progress=upload_progress,
                    progress_args=(message, part_name, file_size)
                )
                
                # Remove part after upload
                os.remove(part_path)
            
            return True
        
        # Get metadata for media files
        metadata = get_media_metadata(file_path)
        
        # Determine file type
        if file_path.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm')):
            # Video file
            width, height, duration = 0, 0, 0
            if metadata:
                width = metadata.get('width', 0)
                height = metadata.get('height', 0)
                duration = metadata.get('duration', 0)
            
            thumbnail_path = None
            try:
                # Generate thumbnail using ffmpeg
                thumbnail_path = os.path.join(UPLOAD_DIR, f"{file_name}.jpg")
                subprocess.run([
                    'ffmpeg', '-i', file_path, '-ss', '00:00:01.000', '-vframes', '1', 
                    '-vf', 'scale=320:320:force_original_aspect_ratio=decrease', 
                    '-y', thumbnail_path
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                pass
            
            await client.send_video(
                chat_id=message.chat.id,
                video=file_path,
                caption=caption,
                width=width,
                height=height,
                duration=duration,
                thumb=thumbnail_path,
                progress=upload_progress,
                progress_args=(message, file_name, file_size)
            )
            
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
                
        elif file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')):
            # Image file
            await client.send_photo(
                chat_id=message.chat.id,
                photo=file_path,
                caption=caption,
                progress=upload_progress,
                progress_args=(message, file_name, file_size)
            )
            
        elif file_path.lower().endswith(('.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a')):
            # Audio file
            duration = 0
            if metadata:
                duration = metadata.get('duration', 0)
            
            thumbnail_path = None
            try:
                # Generate thumbnail using ffmpeg
                thumbnail_path = os.path.join(UPLOAD_DIR, f"{file_name}.jpg")
                subprocess.run([
                    'ffmpeg', '-i', file_path, '-ss', '00:00:01.000', '-vframes', '1', 
                    '-y', thumbnail_path
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                pass
            
            await client.send_audio(
                chat_id=message.chat.id,
                audio=file_path,
                caption=caption,
                duration=duration,
                thumb=thumbnail_path,
                progress=upload_progress,
                progress_args=(message, file_name, file_size)
            )
            
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
                
        else:
            # Document file
            await client.send_document(
                chat_id=message.chat.id,
                document=file_path,
                caption=caption,
                progress=upload_progress,
                progress_args=(message, file_name, file_size)
            )
        
        return True
        
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        await message.reply(f"❌ Error uploading file: {str(e)}")
        return False

async def upload_progress(current, total, message, file_name, file_size):
    """Upload progress callback"""
    try:
        progress = int((current / total) * 100)
        speed = current / (time.time() - uploads[message.chat.id]['start_time']) if message.chat.id in uploads else 0
        
        progress_text = (
            f"📤 Uploading {file_name}\n"
            f"📊 Progress: {progress}% {get_progress_bar(progress)}\n"
            f"⚡ Speed: {get_size(speed)}/s\n"
            f"✅ Uploaded: {get_size(current)} / {get_size(total)}"
        )
        
        try:
            await message.edit_text(progress_text)
        except MessageNotModified:
            pass
    except Exception as e:
        logger.error(f"Error in upload progress: {e}")

# Bot command handlers
@app.on_message(filters.command(["start"]))
async def start_command(client, message):
    """Handle /start command"""
    await message.reply(
        "🎉 Welcome to the Advanced Torrent Leech Bot!\n\n"
        "Features:\n"
        "• Fast torrent/magnet downloads\n"
        "• Direct link downloads\n"
        "• Archive extraction (ZIP, RAR, 7Z, TAR, etc.)\n"
        "• File splitting for large files\n"
        "• Progress tracking\n"
        "• Thumbnail generation\n\n"
        "Commands:\n"
        "/start - Start the bot\n"
        "/help - Show help\n"
        "/leech <url|magnet> - Leech from URL or magnet link\n"
        "/cancel - Cancel current download\n"
        "/status - Check download status\n"
        "/cleartemp - Clear temporary files"
    )

@app.on_message(filters.command(["help"]))
async def help_command(client, message):
    """Handle /help command"""
    await message.reply(
        "📖 Help:\n\n"
        "1. Send a torrent file, magnet link, or direct URL\n"
        "2. Wait for the download to complete\n"
        "3. Receive the downloaded files\n\n"
        "Commands:\n"
        "/leech <url|magnet> - Leech from URL or magnet link\n"
        "/cancel - Cancel current download\n"
        "/status - Check download status\n"
        "/cleartemp - Clear temporary files\n\n"
        "Tips:\n"
        "• For archives, the bot will automatically extract them\n"
        "• Large files will be split into parts\n"
        "• You can send multiple links to create a queue"
    )

@app.on_message(filters.command(["leech"]))
async def leech_command(client, message):
    """Handle /leech command"""
    if len(message.command) < 2:
        await message.reply("❌ Please provide a URL or magnet link.\nUsage: /leech <url|magnet>")
        return
    
    url = message.command[1]
    await process_link(client, message, url)

@app.on_message(filters.command(["cancel"]))
async def cancel_command(client, message):
    """Handle /cancel command"""
    user_id = message.from_user.id
    if user_id in downloads:
        downloads[user_id]['status'] = 'cancelled'
        await message.reply("⏹️ Download will be cancelled shortly...")
    else:
        await message.reply("❌ No active download to cancel.")

@app.on_message(filters.command(["status"]))
async def status_command(client, message):
    """Handle /status command"""
    user_id = message.from_user.id
    if user_id in downloads:
        download = downloads[user_id]
        status = download.get('status', 'Unknown')
        await message.reply(f"📊 Download Status: {status}")
    else:
        await message.reply("❌ No active download.")

@app.on_message(filters.command(["cleartemp"]))
async def clear_temp_command(client, message):
    """Handle /cleartemp command"""
    try:
        # Clean up download directory
        for item in os.listdir(DOWNLOAD_DIR):
            item_path = os.path.join(DOWNLOAD_DIR, item)
            if os.path.isdir(item_path):
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)
        
        # Clean up extract directory
        for item in os.listdir(EXTRACT_DIR):
            item_path = os.path.join(EXTRACT_DIR, item)
            if os.path.isdir(item_path):
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)
        
        # Clean up upload directory
        for item in os.listdir(UPLOAD_DIR):
            item_path = os.path.join(UPLOAD_DIR, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
        
        await message.reply("✅ Temporary files cleared successfully!")
    except Exception as e:
        await message.reply(f"❌ Error clearing temporary files: {str(e)}")

@app.on_message(filters.document & (filters.regex(r'\.torrent$') | filters.regex(r'\.magnet$')))
async def handle_torrent_file(client, message):
    """Handle torrent files"""
    try:
        file_path = await message.download()
        
        if file_path.endswith('.magnet'):
            with open(file_path, 'r') as f:
                magnet_link = f.read().strip()
            os.remove(file_path)
            await process_link(client, message, magnet_link)
        else:
            info = lt.torrent_info(file_path)
            magnet_link = lt.make_magnet_uri(info)
            os.remove(file_path)
            await process_link(client, message, magnet_link)
    except Exception as e:
        await message.reply(f"❌ Error processing torrent file: {str(e)}")

@app.on_message(filters.text & ~filters.command(["start", "help", "leech", "cancel", "status", "cleartemp"]))
async def handle_text_message(client, message):
    """Handle text messages (URLs and magnet links)"""
    text = message.text.strip()
    
    if is_magnet(text) or is_url(text):
        await process_link(client, message, text)
    else:
        # Not a valid link
        pass

async def process_link(client, message, link):
    """Process torrent/magnet link or direct URL"""
    user_id = message.from_user.id
    
    # Check if user already has an active download
    if user_id in downloads and downloads[user_id]['status'] == 'downloading':
        await message.reply("❌ You already have an active download. Please wait for it to complete or use /cancel to cancel it.")
        return
    
    # Initialize download tracking
    downloads[user_id] = {
        'status': 'downloading',
        'message': message
    }
    
    try:
        files = []
        
        if is_magnet(link):
            # Download torrent/magnet
            files = await download_torrent(link, message, user_id)
        elif is_url(link):
            # Download direct link
            files = await download_direct_link(link, message, user_id)
        else:
            await message.reply("❌ Invalid link. Please provide a valid magnet link or URL.")
            return
        
        if not files or user_id not in downloads:
            return
        
        # Process downloaded files
        for file_path in files:
            if user_id not in downloads:
                break
                
            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            
            # Check if file is an archive
            if file_name.lower().endswith(('.zip', '.rar', '.7z', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
                await message.reply(f"📦 Extracting archive: {file_name}")
                
                # Create extraction directory
                extract_dir = os.path.join(EXTRACT_DIR, f"{user_id}_{int(time.time())}")
                os.makedirs(extract_dir, exist_ok=True)
                
                # Extract archive
                if extract_archive(file_path, extract_dir):
                    # Get extracted files
                    extracted_files = []
                    for root, _, files in os.walk(extract_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            if os.path.isfile(file_path):
                                extracted_files.append(file_path)
                    
                    # Upload extracted files
                    for extracted_file in extracted_files:
                        if user_id not in downloads:
                            break
                            
                        extracted_name = os.path.basename(extracted_file)
                        await message.reply(f"📤 Uploading extracted file: {extracted_name}")
                        
                        # Initialize upload tracking
                        uploads[message.chat.id] = {
                            'start_time': time.time()
                        }
                        
                        await upload_file(client, message, extracted_file)
                    
                    # Clean up extraction directory
                    shutil.rmtree(extract_dir)
                else:
                    await message.reply(f"❌ Failed to extract archive: {file_name}")
                    # Upload original archive if extraction failed
                    uploads[message.chat.id] = {
                        'start_time': time.time()
                    }
                    await upload_file(client, message, file_path)
            else:
                # Not an archive, upload directly
                await message.reply(f"📤 Uploading file: {file_name}")
                
                # Initialize upload tracking
                uploads[message.chat.id] = {
                    'start_time': time.time()
                }
                
                await upload_file(client, message, file_path)
            
            # Remove original file after processing
            if os.path.exists(file_path):
                os.remove(file_path)
        
        # Clean up download tracking
        if user_id in downloads:
            del downloads[user_id]
            
        await message.reply("✅ All files uploaded successfully!")
        
    except Exception as e:
        logger.error(f"Error processing link: {e}")
        await message.reply(f"❌ Error processing link: {str(e)}")
        
        # Clean up download tracking
        if user_id in downloads:
            del downloads[user_id]

# Start the bot
if __name__ == "__main__":
    app.run()
