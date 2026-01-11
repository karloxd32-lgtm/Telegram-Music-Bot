import os
import sys
import json
import asyncio
import logging
import aiofiles
import aiohttp
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Union

from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ChatMember
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pytgcalls import PyTgCalls
from pytgcalls.types import Update
from pytgcalls.types.input_stream import AudioPiped, AudioVideoPiped
from pytgcalls.types.stream import StreamAudioEnded

from youtube_search import YoutubeSearch
import yt_dlp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    # Load from environment variables
    API_ID = int(os.getenv("API_ID", "39932230"))
    API_HASH = os.getenv("API_HASH", "785206fcbe254023f3fcb941237caee2")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "8376943274: AAHF9XtjbsersspZzcLfwVIkiR5QwJJpCuo")
    OWNER_ID = int(os.getenv("OWNER_ID", "6523402499"))
    SESSION_NAME = os.getenv("SESSION_NAME", "music_bot")
    
    # Bot settings
    MAX_QUEUE_SIZE = 100
    MAX_DOWNLOAD_SIZE = 50  # MB
    ALLOWED_FORMATS = ["mp3", "m4a", "ogg", "wav", "flac"]
    
    # Paths
    QUEUE_FILE = "data/queue.json"
    TEMP_DIR = "temp"
    
    # Messages
    HELP_TEXT = """
🤖 **Music Bot Help**

🎵 **Music Commands**
• /play [song name/URL] - Play a song
• /pause - Pause current song
• /resume - Resume paused song
• /skip - Skip to next song
• /stop - Stop current song
• /end - End the playback
• /clear - Clear the queue

🛠 **Utility Commands**
• /ping - Check bot latency
• /reboot - Reboot the bot (Owner only)
• /broadcast - Broadcast message (Owner only)

🛡 **Admin/Moderation Commands**
• /mute [user] - Mute a user
• /unmute [user] - Unmute a user
• /tmute [user] [time] - Temporary mute
• /ban [user] - Ban a user
• /unban [user] - Unban a user
• /kick [user] - Kick a user
• /stats - Show bot statistics

📝 **Usage Tips**
• Add the bot to your group
• Make it admin with voice chat permissions
• Use /play to start playing music
"""

# ============================================================================
# DATA STRUCTURES
# ============================================================================

class QueueItem:
    def __init__(self, chat_id: int, title: str, url: str, duration: str, requested_by: str):
        self.chat_id = chat_id
        self.title = title
        self.url = url
        self.duration = duration
        self.requested_by = requested_by
        self.added_at = datetime.now()

class MusicBot:
    def __init__(self):
        self.queues: Dict[int, List[QueueItem]] = {}
        self.now_playing: Dict[int, QueueItem] = {}
        self.is_playing: Dict[int, bool] = {}
        self.is_paused: Dict[int, bool] = {}
        self.user_mutes: Dict[int, Dict[int, datetime]] = {}  # {chat_id: {user_id: unmute_time}}
        self.banned_users: Dict[int, List[int]] = {}  # {chat_id: [user_ids]}
        self.stats = {
            "songs_played": 0,
            "commands_used": 0,
            "users_served": set(),
            "start_time": datetime.now()
        }
        
        # Create directories
        os.makedirs("temp", exist_ok=True)
        os.makedirs("data", exist_ok=True)
        
        # Load saved data
        self.load_data()
    
    def load_data(self):
        """Load queues and data from file"""
        try:
            if os.path.exists(Config.QUEUE_FILE):
                with open(Config.QUEUE_FILE, 'r') as f:
                    data = json.load(f)
                    # Convert back to QueueItem objects
                    for chat_id_str, items in data.get('queues', {}).items():
                        chat_id = int(chat_id_str)
                        self.queues[chat_id] = [
                            QueueItem(
                                chat_id=item['chat_id'],
                                title=item['title'],
                                url=item['url'],
                                duration=item['duration'],
                                requested_by=item['requested_by']
                            ) for item in items
                        ]
        except Exception as e:
            logger.error(f"Error loading data: {e}")
    
    def save_data(self):
        """Save queues to file"""
        try:
            data = {
                'queues': {
                    str(chat_id): [
                        {
                            'chat_id': item.chat_id,
                            'title': item.title,
                            'url': item.url,
                            'duration': item.duration,
                            'requested_by': item.requested_by
                        } for item in items
                    ] for chat_id, items in self.queues.items()
                }
            }
            with open(Config.QUEUE_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving data: {e}")

# ============================================================================
# INITIALIZE BOT
# ============================================================================

# Initialize bot client
bot = Client(
    name=Config.SESSION_NAME,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN
)

# Initialize PyTgCalls
calls = PyTgCalls(bot)
music_bot = MusicBot()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def is_admin(chat_id: int, user_id: int) -> bool:
    """Check if user is admin in chat"""
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in [
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]
    except:
        return False

def is_owner(user_id: int) -> bool:
    """Check if user is bot owner"""
    return user_id == Config.OWNER_ID

def format_time(seconds: int) -> str:
    """Convert seconds to HH:MM:SS format"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

async def search_youtube(query: str) -> Tuple[str, str, str]:
    """Search YouTube for a song and return first result"""
    try:
        results = YoutubeSearch(query, max_results=1).to_dict()
        if results:
            video = results[0]
            title = video['title']
            url = f"https://youtube.com/watch?v={video['id']}"
            duration = video.get('duration', '0:00')
            return title, url, duration
    except Exception as e:
        logger.error(f"Search error: {e}")
    return None, None, None

async def download_audio(url: str, chat_id: int) -> Optional[str]:
    """Download audio from YouTube"""
    try:
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio',
            'outtmpl': f'temp/%(id)s.%(ext)s',
            'quiet': True,
            'no_warnings': True,
            'extractaudio': True,
            'audioformat': 'mp3',
            'noplaylist': True,
            'max_filesize': Config.MAX_DOWNLOAD_SIZE * 1024 * 1024,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Change extension to .mp3 if needed
            if not filename.endswith('.mp3'):
                new_filename = os.path.splitext(filename)[0] + '.mp3'
                os.rename(filename, new_filename)
                return new_filename
            return filename
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

async def send_status_message(chat_id: int, text: str):
    """Send status message with formatting"""
    await bot.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.MARKDOWN
    )

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

# 🎵 MUSIC COMMANDS

@bot.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Start command handler"""
    music_bot.stats["commands_used"] += 1
    music_bot.stats["users_served"].add(message.from_user.id)
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Commands", callback_data="help"),
         InlineKeyboardButton("🎵 Play Music", callback_data="play_help")],
        [InlineKeyboardButton("👥 Support", url="https://t.me/+0000000000"),
         InlineKeyboardButton("📢 Channel", url="https://t.me/+0000000000")]
    ])
    
    await message.reply_text(
        f"👋 Hello {message.from_user.mention}!\n\n"
        "I'm a Music Bot for Telegram Voice Chats.\n"
        "I can play music from YouTube in your group's voice chat.\n\n"
        "Use /help to see all available commands.",
        reply_markup=keyboard
    )

@bot.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    """Help command handler"""
    music_bot.stats["commands_used"] += 1
    await message.reply_text(Config.HELP_TEXT)

@bot.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    """Play music command"""
    music_bot.stats["commands_used"] += 1
    
    # Check if in group
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await message.reply_text("❌ This command can only be used in groups!")
        return
    
    # Check if user is in voice chat
    try:
        member = await client.get_chat_member(message.chat.id, message.from_user.id)
        if not member.joined_date:
            await message.reply_text("❌ You must be in the voice chat to play music!")
            return
    except:
        pass
    
    # Check for query
    if len(message.command) < 2:
        await message.reply_text("❌ Please provide a song name or YouTube URL!\n\nExample: `/play Believer`")
        return
    
    query = " ".join(message.command[1:])
    chat_id = message.chat.id
    
    # Send searching message
    status_msg = await message.reply_text("🔍 Searching...")
    
    # Search YouTube
    title, url, duration = await search_youtube(query)
    if not title:
        await status_msg.edit_text("❌ No results found!")
        return
    
    # Add to queue
    if chat_id not in music_bot.queues:
        music_bot.queues[chat_id] = []
    
    if len(music_bot.queues[chat_id]) >= Config.MAX_QUEUE_SIZE:
        await status_msg.edit_text("❌ Queue is full! Maximum size reached.")
        return
    
    queue_item = QueueItem(
        chat_id=chat_id,
        title=title,
        url=url,
        duration=duration,
        requested_by=message.from_user.mention
    )
    music_bot.queues[chat_id].append(queue_item)
    
    # Save queue
    music_bot.save_data()
    
    # If nothing is playing, start playing
    if not music_bot.is_playing.get(chat_id) or not music_bot.is_paused.get(chat_id):
        await play_next(chat_id)
        await status_msg.edit_text(f"🎶 Now playing: **{title}**\n"
                                  f"⏱ Duration: `{duration}`\n"
                                  f"👤 Requested by: {message.from_user.mention}")
    else:
        position = len(music_bot.queues[chat_id])
        await status_msg.edit_text(f"✅ Added to queue (#{position})\n"
                                  f"🎵 **{title}**\n"
                                  f"⏱ `{duration}`\n"
                                  f"👤 Requested by: {message.from_user.mention}")

@bot.on_message(filters.command("pause"))
async def pause_command(client: Client, message: Message):
    """Pause music command"""
    music_bot.stats["commands_used"] += 1
    chat_id = message.chat.id
    
    if not music_bot.is_playing.get(chat_id) or music_bot.is_paused.get(chat_id):
        await message.reply_text("❌ No music is playing or already paused!")
        return
    
    try:
        await calls.pause_stream(chat_id)
        music_bot.is_paused[chat_id] = True
        await message.reply_text("⏸ Music paused!")
    except Exception as e:
        await message.reply_text("❌ Failed to pause music!")

@bot.on_message(filters.command("resume"))
async def resume_command(client: Client, message: Message):
    """Resume music command"""
    music_bot.stats["commands_used"] += 1
    chat_id = message.chat.id
    
    if not music_bot.is_paused.get(chat_id):
        await message.reply_text("❌ Music is not paused!")
        return
    
    try:
        await calls.resume_stream(chat_id)
        music_bot.is_paused[chat_id] = False
        await message.reply_text("▶️ Music resumed!")
    except Exception as e:
        await message.reply_text("❌ Failed to resume music!")

@bot.on_message(filters.command("skip"))
async def skip_command(client: Client, message: Message):
    """Skip music command"""
    music_bot.stats["commands_used"] += 1
    chat_id = message.chat.id
    
    if not music_bot.is_playing.get(chat_id):
        await message.reply_text("❌ No music is playing!")
        return
    
    # Check if user is admin or requester
    current_song = music_bot.now_playing.get(chat_id)
    if current_song and current_song.requested_by != message.from_user.mention:
        if not is_admin(chat_id, message.from_user.id):
            await message.reply_text("❌ You can only skip songs you requested!")
            return
    
    await message.reply_text("⏭ Skipping current song...")
    await play_next(chat_id)

@bot.on_message(filters.command("stop"))
async def stop_command(client: Client, message: Message):
    """Stop music command"""
    music_bot.stats["commands_used"] += 1
    chat_id = message.chat.id
    
    if not music_bot.is_playing.get(chat_id):
        await message.reply_text("❌ No music is playing!")
        return
    
    try:
        await calls.leave_group_call(chat_id)
        music_bot.is_playing[chat_id] = False
        music_bot.is_paused[chat_id] = False
        await message.reply_text("⏹ Music stopped!")
    except Exception as e:
        await message.reply_text("❌ Failed to stop music!")

@bot.on_message(filters.command("end"))
async def end_command(client: Client, message: Message):
    """End playback command"""
    music_bot.stats["commands_used"] += 1
    chat_id = message.chat.id
    
    if chat_id in music_bot.queues:
        music_bot.queues[chat_id].clear()
    
    if chat_id in music_bot.now_playing:
        del music_bot.now_playing[chat_id]
    
    music_bot.is_playing[chat_id] = False
    music_bot.is_paused[chat_id] = False
    
    try:
        await calls.leave_group_call(chat_id)
    except:
        pass
    
    music_bot.save_data()
    await message.reply_text("🎵 Playback ended and queue cleared!")

@bot.on_message(filters.command("clear"))
async def clear_command(client: Client, message: Message):
    """Clear queue command"""
    music_bot.stats["commands_used"] += 1
    chat_id = message.chat.id
    
    if chat_id not in music_bot.queues or not music_bot.queues[chat_id]:
        await message.reply_text("❌ Queue is already empty!")
        return
    
    queue_count = len(music_bot.queues[chat_id])
    music_bot.queues[chat_id].clear()
    music_bot.save_data()
    
    await message.reply_text(f"🧹 Cleared {queue_count} songs from queue!")

# 🛠 UTILITY COMMANDS

@bot.on_message(filters.command("ping"))
async def ping_command(client: Client, message: Message):
    """Ping command to check latency"""
    music_bot.stats["commands_used"] += 1
    start = datetime.now()
    msg = await message.reply_text("🏓 Pong!")
    end = datetime.now()
    latency = (end - start).microseconds / 1000
    
    await msg.edit_text(f"🏓 Pong!\n📡 Latency: `{latency}ms`\n"
                       f"💾 RAM: `{sys.getsizeof(music_bot) // 1024}KB`")

@bot.on_message(filters.command("reboot"))
async def reboot_command(client: Client, message: Message):
    """Reboot bot command (Owner only)"""
    if not is_owner(message.from_user.id):
        await message.reply_text("❌ This command is for bot owner only!")
        return
    
    music_bot.stats["commands_used"] += 1
    await message.reply_text("🔄 Rebooting bot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@bot.on_message(filters.command("broadcast"))
async def broadcast_command(client: Client, message: Message):
    """Broadcast message to all groups (Owner only)"""
    if not is_owner(message.from_user.id):
        await message.reply_text("❌ This command is for bot owner only!")
        return
    
    if len(message.command) < 2:
        await message.reply_text("❌ Please provide a message to broadcast!")
        return
    
    music_bot.stats["commands_used"] += 1
    broadcast_text = " ".join(message.command[1:])
    
    await message.reply_text("📢 Starting broadcast...")
    
    # Get all chats where bot is member
    success = 0
    failed = 0
    
    async for dialog in client.get_dialogs():
        if dialog.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            try:
                await client.send_message(
                    dialog.chat.id,
                    f"📢 **Broadcast Message**\n\n{broadcast_text}"
                )
                success += 1
                await asyncio.sleep(0.5)  # Prevent flooding
            except:
                failed += 1
    
    await message.reply_text(f"✅ Broadcast completed!\n"
                           f"✅ Success: {success}\n"
                           f"❌ Failed: {failed}")

# 🛡 ADMIN/MODERATION COMMANDS

@bot.on_message(filters.command("mute"))
async def mute_command(client: Client, message: Message):
    """Mute a user in chat"""
    music_bot.stats["commands_used"] += 1
    
    if not is_admin(message.chat.id, message.from_user.id):
        await message.reply_text("❌ You need to be admin to use this command!")
        return
    
    if not message.reply_to_message:
        await message.reply_text("❌ Please reply to a user's message to mute them!")
        return
    
    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    try:
        await client.restrict_chat_member(
            chat_id,
            target_user.id,
            ChatPermissions(can_send_messages=False)
        )
        await message.reply_text(f"🔇 User {target_user.mention} has been muted!")
    except Exception as e:
        await message.reply_text("❌ Failed to mute user!")

@bot.on_message(filters.command("unmute"))
async def unmute_command(client: Client, message: Message):
    """Unmute a user in chat"""
    music_bot.stats["commands_used"] += 1
    
    if not is_admin(message.chat.id, message.from_user.id):
        await message.reply_text("❌ You need to be admin to use this command!")
        return
    
    if not message.reply_to_message:
        await message.reply_text("❌ Please reply to a user's message to unmute them!")
        return
    
    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    try:
        await client.restrict_chat_member(
            chat_id,
            target_user.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        await message.reply_text(f"🔊 User {target_user.mention} has been unmuted!")
    except Exception as e:
        await message.reply_text("❌ Failed to unmute user!")

@bot.on_message(filters.command("ban"))
async def ban_command(client: Client, message: Message):
    """Ban a user from chat"""
    music_bot.stats["commands_used"] += 1
    
    if not is_admin(message.chat.id, message.from_user.id):
        await message.reply_text("❌ You need to be admin to use this command!")
        return
    
    if not message.reply_to_message:
        await message.reply_text("❌ Please reply to a user's message to ban them!")
        return
    
    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    try:
        await client.ban_chat_member(chat_id, target_user.id)
        
        # Add to banned list
        if chat_id not in music_bot.banned_users:
            music_bot.banned_users[chat_id] = []
        music_bot.banned_users[chat_id].append(target_user.id)
        
        await message.reply_text(f"🚫 User {target_user.mention} has been banned!")
    except Exception as e:
        await message.reply_text("❌ Failed to ban user!")

@bot.on_message(filters.command("unban"))
async def unban_command(client: Client, message: Message):
    """Unban a user from chat"""
    music_bot.stats["commands_used"] += 1
    
    if not is_admin(message.chat.id, message.from_user.id):
        await message.reply_text("❌ You need to be admin to use this command!")
        return
    
    if not message.reply_to_message:
        await message.reply_text("❌ Please reply to a user's message to unban them!")
        return
    
    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    try:
        await client.unban_chat_member(chat_id, target_user.id)
        
        # Remove from banned list
        if chat_id in music_bot.banned_users:
            if target_user.id in music_bot.banned_users[chat_id]:
                music_bot.banned_users[chat_id].remove(target_user.id)
        
        await message.reply_text(f"✅ User {target_user.mention} has been unbanned!")
    except Exception as e:
        await message.reply_text("❌ Failed to unban user!")

@bot.on_message(filters.command("kick"))
async def kick_command(client: Client, message: Message):
    """Kick a user from chat"""
    music_bot.stats["commands_used"] += 1
    
    if not is_admin(message.chat.id, message.from_user.id):
        await message.reply_text("❌ You need to be admin to use this command!")
        return
    
    if not message.reply_to_message:
        await message.reply_text("❌ Please reply to a user's message to kick them!")
        return
    
    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    try:
        await client.ban_chat_member(chat_id, target_user.id)
        await asyncio.sleep(1)
        await client.unban_chat_member(chat_id, target_user.id)
        
        await message.reply_text(f"👢 User {target_user.mention} has been kicked!")
    except Exception as e:
        await message.reply_text("❌ Failed to kick user!")

@bot.on_message(filters.command("stats"))
async def stats_command(client: Client, message: Message):
    """Show bot statistics"""
    music_bot.stats["commands_used"] += 1
    
    uptime = datetime.now() - music_bot.stats["start_time"]
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    stats_text = (
        "📊 **Bot Statistics**\n\n"
        f"• **Uptime**: {hours}h {minutes}m {seconds}s\n"
        f"• **Songs Played**: {music_bot.stats['songs_played']}\n"
        f"• **Commands Used**: {music_bot.stats['commands_used']}\n"
        f"• **Users Served**: {len(music_bot.stats['users_served'])}\n"
        f"• **Active Chats**: {len(music_bot.queues)}\n"
        f"• **Total in Queue**: {sum(len(q) for q in music_bot.queues.values())}\n\n"
        "⚙️ **System Info**\n"
        f"• Python: {sys.version.split()[0]}\n"
        f"• Platform: {sys.platform}"
    )
    
    await message.reply_text(stats_text)

# ============================================================================
# MUSIC PLAYBACK FUNCTIONS
# ============================================================================

async def play_next(chat_id: int):
    """Play next song in queue"""
    if chat_id not in music_bot.queues or not music_bot.queues[chat_id]:
        music_bot.is_playing[chat_id] = False
        return
    
    # Get next song
    queue_item = music_bot.queues[chat_id].pop(0)
    music_bot.now_playing[chat_id] = queue_item
    music_bot.is_playing[chat_id] = True
    music_bot.is_paused[chat_id] = False
    
    # Save queue
    music_bot.save_data()
    
    # Download audio
    status_msg = await bot.send_message(chat_id, f"⬇️ Downloading: **{queue_item.title}**...")
    audio_path = await download_audio(queue_item.url, chat_id)
    
    if not audio_path:
        await status_msg.edit_text(f"❌ Failed to download: {queue_item.title}")
        await play_next(chat_id)
        return
    
    try:
        # Join voice chat if not already joined
        try:
            await calls.join_group_call(
                chat_id,
                AudioPiped(audio_path)
            )
        except:
            # If already joined, change stream
            await calls.change_stream(
                chat_id,
                AudioPiped(audio_path)
            )
        
        music_bot.stats["songs_played"] += 1
        
        # Update status message
        await status_msg.edit_text(
            f"🎶 **Now Playing**\n"
            f"📝 **Title**: {queue_item.title}\n"
            f"⏱ **Duration**: `{queue_item.duration}`\n"
            f"👤 **Requested by**: {queue_item.requested_by}\n"
            f"📊 **Queue**: {len(music_bot.queues.get(chat_id, []))} songs"
        )
        
        # Wait for song to finish
        # In production, you'd listen for StreamAudioEnded event
        # For simplicity, we'll estimate duration
        try:
            # Parse duration (mm:ss or hh:mm:ss)
            duration_parts = queue_item.duration.split(':')
            if len(duration_parts) == 3:
                hours, minutes, seconds = map(int, duration_parts)
                total_seconds = hours * 3600 + minutes * 60 + seconds
            elif len(duration_parts) == 2:
                minutes, seconds = map(int, duration_parts)
                total_seconds = minutes * 60 + seconds
            else:
                total_seconds = 180  # Default 3 minutes
            
            await asyncio.sleep(total_seconds + 2)  # Add buffer
        except:
            await asyncio.sleep(180)  # Default 3 minutes
        
        # Clean up file
        try:
            os.remove(audio_path)
        except:
            pass
        
        # Play next song
        await play_next(chat_id)
        
    except Exception as e:
        logger.error(f"Play error: {e}")
        await status_msg.edit_text(f"❌ Error playing: {queue_item.title}")
        
        # Clean up file
        try:
            os.remove(audio_path)
        except:
            pass
        
        # Try next song
        await play_next(chat_id)

# ============================================================================
# CALLBACK QUERY HANDLER
# ============================================================================

@bot.on_callback_query()
async def callback_handler(client: Client, callback_query):
    """Handle inline keyboard buttons"""
    data = callback_query.data
    chat_id = callback_query.message.chat.id
    user_id = callback_query.from_user.id
    
    if data == "help":
        await callback_query.message.edit_text(
            Config.HELP_TEXT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back")]
            ])
        )
    
    elif data == "play_help":
        await callback_query.message.edit_text(
            "🎵 **How to Play Music**\n\n"
            "1. Add me to your group\n"
            "2. Make me admin with voice chat permissions\n"
            "3. Start a voice chat\n"
            "4. Use `/play song_name` to play music\n\n"
            "Example: `/play Believer Imagine Dragons`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back")]
            ])
        )
    
    elif data == "back":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 Commands", callback_data="help"),
             InlineKeyboardButton("🎵 Play Music", callback_data="play_help")],
            [InlineKeyboardButton("👥 Support", url="https://t.me/+0000000000"),
             InlineKeyboardButton("📢 Channel", url="https://t.me/+0000000000")]
        ])
        
        await callback_query.message.edit_text(
            f"👋 Hello {callback_query.from_user.mention}!\n\n"
            "I'm a Music Bot for Telegram Voice Chats.\n"
            "I can play music from YouTube in your group's voice chat.\n\n"
            "Use /help to see all available commands.",
            reply_markup=keyboard
        )
    
    await callback_query.answer()

# ============================================================================
# EVENT HANDLERS
# ============================================================================

@calls.on_stream_end()
async def stream_end_handler(_, update: Update):
    """Handle stream end event"""
    if isinstance(update, StreamAudioEnded):
        chat_id = update.chat_id
        await play_next(chat_id)

# ============================================================================
# STARTUP AND SHUTDOWN
# ============================================================================

async def startup():
    """Bot startup tasks"""
    logger.info("Starting Music Bot...")
    
    # Create necessary directories
    os.makedirs("temp", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    
    # Clean temp directory
    for file in os.listdir("temp"):
        try:
            os.remove(f"temp/{file}")
        except:
            pass
    
    logger.info("Bot started successfully!")

async def shutdown():
    """Bot shutdown tasks"""
    logger.info("Shutting down Music Bot...")
    
    # Save data
    music_bot.save_data()
    
    # Clean temp directory
    for file in os.listdir("temp"):
        try:
            os.remove(f"temp/{file}")
        except:
            pass
    
    logger.info("Bot shutdown complete!")

# ============================================================================
# MAIN FUNCTION
# ============================================================================

async def main():
    """Main function to run the bot"""
    # Startup tasks
    await startup()
    
    try:
        # Start Pyrogram client
        await bot.start()
        logger.info("Pyrogram client started")
        
        # Start PyTgCalls client
        await calls.start()
        logger.info("PyTgCalls client started")
        
        # Set bot commands
        await bot.set_bot_commands([
            ("start", "Start the bot"),
            ("play", "Play music from YouTube"),
            ("pause", "Pause current song"),
            ("resume", "Resume paused song"),
            ("skip", "Skip current song"),
            ("stop", "Stop playback"),
            ("end", "End playback and clear queue"),
            ("clear", "Clear queue"),
            ("ping", "Check bot latency"),
            ("reboot", "Reboot bot (Owner only)"),
            ("broadcast", "Broadcast message (Owner only)"),
            ("mute", "Mute a user"),
            ("unmute", "Unmute a user"),
            ("ban", "Ban a user"),
            ("unban", "Unban a user"),
            ("kick", "Kick a user"),
            ("stats", "Show bot statistics"),
        ])
        
        logger.info("Bot commands set")
        logger.info("Bot is now running...")
        
        # Keep the bot running
        await idle()
        
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        # Shutdown tasks
        await shutdown()
        await bot.stop()

if __name__ == "__main__":
    # Run the bot
    asyncio.run(main())