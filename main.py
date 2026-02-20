import os
import sys
import subprocess
import tempfile
import yaml
import traceback
import json
import asyncio
import hashlib
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty, MessageMediaPhoto, MessageMediaDocument, InputMessagesFilterEmpty
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio, DocumentAttributeFilename
from telethon.tl.types import MessageMediaWebPage
from telethon.tl.types import MessageService
try:
    from telethon.tl.types import MessageMediaContact, MessageMediaGeo, MessageMediaVenue, MessageMediaGame
    from telethon.tl.types import MessageMediaInvoice, MessageMediaGeoLive, MessageMediaPoll, MessageMediaDice
    EXTENDED_MEDIA_SUPPORT = True
except ImportError:
    # 对于较老版本的telethon，这些类型可能不存在
    EXTENDED_MEDIA_SUPPORT = False
from telethon.errors import FloodWaitError, SecurityError, UnauthorizedError, BadRequestError
import re
import time
import logging
import random

# 设置日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 设置Telethon的日志级别为WARNING，减少更新相关的日志
telethon_logger = logging.getLogger('telethon')
telethon_logger.setLevel(logging.WARNING)

# 设置应用程序的日志级别
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 全局变量，用于追踪连续错误和重试
consecutive_errors = 0
MAX_CONSECUTIVE_ERRORS = 5
SESSION_FILENAME = 'telegram_session'
SESSION_BACKUP_DIR = 'session_backups'

# Load configuration from config.yml
def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yml')
    try:
        with open(config_path, 'r') as config_file:
            config = yaml.safe_load(config_file)
        return config
    except Exception as e:
        print(f"Error loading config file: {e}")
        print("Please make sure config.yml exists in the project directory and contains valid API credentials.")
        sys.exit(1)

# Get API credentials from config
config = load_config()
API_ID = config.get('api_id')
API_HASH = config.get('api_hash')

# Validate API credentials
if not API_ID or not API_HASH or API_ID == 12345 or API_HASH == "your_api_hash_here":
    print("Error: Invalid API credentials in config.yml")
    print("Please update config.yml with your actual API ID and API Hash from https://my.telegram.org/")
    sys.exit(1)

# Client setup - modified to be a function so we can recreate it
def create_client():
    return TelegramClient(SESSION_FILENAME, API_ID, API_HASH)

# Initial client instance
client = create_client()

# 添加会话备份和恢复函数
def backup_session():
    """备份当前会话文件"""
    try:
        # 确保备份目录存在
        os.makedirs(SESSION_BACKUP_DIR, exist_ok=True)
        
        # 创建带时间戳的备份文件名
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_filename = f"{SESSION_FILENAME}_{timestamp}"
        backup_path = os.path.join(SESSION_BACKUP_DIR, backup_filename)
        
        # 复制会话文件
        if os.path.exists(f"{SESSION_FILENAME}.session"):
            import shutil
            shutil.copy2(f"{SESSION_FILENAME}.session", f"{backup_path}.session")
            logger.info(f"Successfully backed up session to {backup_path}.session")
            return True
    except Exception as e:
        logger.error(f"Failed to backup session: {e}")
        return False
    return False

def reset_session():
    """重置会话文件并创建新客户端"""
    global client
    
    try:
        # 先备份当前会话
        backup_session()
        
        # 关闭当前客户端
        if client and client.is_connected():
            logger.info("Disconnecting current client...")
            client.disconnect()
        
        # 重命名当前会话文件（如果存在）
        session_file = f"{SESSION_FILENAME}.session"
        if os.path.exists(session_file):
            os.rename(session_file, f"{session_file}.old")
            logger.info(f"Renamed current session file to {session_file}.old")
        
        # 创建新客户端
        logger.info("Creating new client with fresh session...")
        client = create_client()
        
        # 返回新创建的客户端
        return client
    except Exception as e:
        logger.error(f"Error resetting session: {e}")
        # 如果重置失败，尝试恢复原来的客户端
        client = create_client()
        return client

async def handle_connection_error(e, retry_count=0, max_retries=3, retry_delay=5):
    """处理连接错误，包括安全错误和未授权错误"""
    global consecutive_errors, client
    
    logger.error(f"Connection error: {e}")
    consecutive_errors += 1
    
    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS or isinstance(e, (SecurityError, UnauthorizedError)):
        logger.warning(f"Too many consecutive errors ({consecutive_errors}) or critical error detected. Resetting session...")
        # 重置会话
        backup_session()
        client = reset_session()
        consecutive_errors = 0
        
        # 尝试重新连接
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning("Session reset, but user is not authorized. Need to login again.")
                await qr_login()
            else:
                logger.info("Successfully reconnected after session reset.")
            return True
        except Exception as reconnect_error:
            logger.error(f"Failed to reconnect after session reset: {reconnect_error}")
            return False
    
    # 对于其他错误，如果重试次数未达到最大值，则等待后重试
    if retry_count < max_retries:
        retry_delay_with_jitter = retry_delay + (random.random() * 2)
        logger.info(f"Retrying in {retry_delay_with_jitter:.2f} seconds (attempt {retry_count + 1}/{max_retries})...")
        await asyncio.sleep(retry_delay_with_jitter)
        return True
    else:
        logger.error(f"Failed after {max_retries} retries.")
        return False

# 添加一个全局字典来保存每个schedule文件对应的数据
# 使用字典以支持多个任务并行处理时各自有独立的缓存
_schedule_cache = {}

def get_message_type(message):
    """Determine the type of message based on its media content"""
    # 首先检查是否是服务消息
    if isinstance(message, MessageService):
        logger.info(f"DEBUG: Message {message.id} is a service message")
        return "service"
    
    # 检查转发消息
    is_forwarded = hasattr(message, 'forward') and message.forward is not None
    
    # 添加详细的调试信息
    if is_forwarded:
        logger.info(f"DEBUG: Message {message.id} is forwarded")
        logger.info(f"DEBUG: Forward info: {message.forward}")
        logger.info(f"DEBUG: Message has media: {hasattr(message, 'media') and message.media is not None}")
        if hasattr(message, 'media') and message.media:
            logger.info(f"DEBUG: Media type: {type(message.media).__name__}")
            logger.info(f"DEBUG: Media content: {message.media}")
    
    # 对于转发消息，我们仍然需要检查媒体内容
    # 转发消息的媒体内容通常仍然在message.media中
    
    # 首先检查是否有媒体内容
    if message.media:
        logger.info(f"DEBUG: Message {message.id} has media of type: {type(message.media).__name__}")
        
        if isinstance(message.media, MessageMediaPhoto):
            if is_forwarded:
                logger.info(f"DEBUG: Forwarded message {message.id} contains photo")
            return "video"
        elif isinstance(message.media, MessageMediaWebPage):
            if is_forwarded:
                logger.info(f"DEBUG: Forwarded message {message.id} contains webpage")
            return "webpage"
        elif isinstance(message.media, MessageMediaDocument):
            if is_forwarded:
                logger.info(f"DEBUG: Forwarded message {message.id} contains document")
                logger.info(f"DEBUG: Document attributes: {message.media.document.attributes}")
                logger.info(f"DEBUG: Document mime_type: {getattr(message.media.document, 'mime_type', 'None')}")
            
            for attribute in message.media.document.attributes:
                if isinstance(attribute, DocumentAttributeVideo):
                    if is_forwarded:
                        logger.info(f"DEBUG: Forwarded message {message.id} identified as video")
                    return "video"
                elif isinstance(attribute, DocumentAttributeAudio):
                    if attribute.voice:
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} identified as voice")
                        return "voice"
                    elif attribute.voice_note:
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} identified as voice_note")
                        return "voice_note"
                    else:
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} identified as audio")
                        return "audio"
            # Check filename for document type
            for attribute in message.media.document.attributes:
                if isinstance(attribute, DocumentAttributeFilename):
                    filename = attribute.file_name.lower()
                    if is_forwarded:
                        logger.info(f"DEBUG: Forwarded message {message.id} filename: {filename}")
                    if any(filename.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} identified as video by filename")
                        return "video"
                    elif any(filename.endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.mkv']):
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} identified as video by filename")
                        return "video"
                    elif any(filename.endswith(ext) for ext in ['.mp3', '.wav', '.ogg', '.flac']):
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} identified as audio by filename")
                        return "audio"
            if is_forwarded:
                logger.info(f"DEBUG: Forwarded message {message.id} identified as generic document")
            return "document"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaContact):
            logger.info(f"DEBUG: Message {message.id} contains contact")
            return "contact"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaGeo):
            logger.info(f"DEBUG: Message {message.id} contains location")
            return "location"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaVenue):
            logger.info(f"DEBUG: Message {message.id} contains venue")
            return "location"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaGeoLive):
            logger.info(f"DEBUG: Message {message.id} contains live location")
            return "location"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaPoll):
            logger.info(f"DEBUG: Message {message.id} contains poll")
            return "poll"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaGame):
            logger.info(f"DEBUG: Message {message.id} contains game")
            return "game"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaInvoice):
            logger.info(f"DEBUG: Message {message.id} contains invoice/payment")
            return "invoice"
        elif EXTENDED_MEDIA_SUPPORT and isinstance(message.media, MessageMediaDice):
            logger.info(f"DEBUG: Message {message.id} contains dice")
            return "dice"
        else:
            # 这里是关键：未知的媒体类型
            logger.warning(f"DEBUG: Message {message.id} has UNKNOWN media type: {type(message.media).__name__}")
            logger.warning(f"DEBUG: Full media object: {message.media}")
            logger.warning(f"DEBUG: Media attributes: {dir(message.media)}")
            if is_forwarded:
                logger.info(f"DEBUG: Forwarded message {message.id} has unknown media type: {type(message.media)}")
    else:
        logger.info(f"DEBUG: Message {message.id} has no media")
        if is_forwarded:
            logger.info(f"DEBUG: Forwarded message {message.id} has no media")
    
    # 如果没有媒体但有文本内容，返回text
    if message.text:
        logger.info(f"DEBUG: Message {message.id} has text content, length: {len(message.text)}")
        if is_forwarded:
            logger.info(f"DEBUG: Forwarded message {message.id} identified as text")
        return "text"
    
    # 如果到这里，说明既没有识别的媒体类型，也没有文本
    logger.warning(f"DEBUG: Message {message.id} - NO media and NO text, returning unknown")
    logger.warning(f"DEBUG: Message {message.id} full object: {message}")
    logger.warning(f"DEBUG: Message {message.id} attributes: {dir(message)}")
    
    if is_forwarded:
        logger.info(f"DEBUG: Forwarded message {message.id} type unknown")
    return "unknown"

def load_task_file(task_file):
    """Load task configuration from YAML file"""
    try:
        with open(task_file, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading task file: {e}")
        sys.exit(1)

def get_last_message_id(schedule_file):
    """从schedule文件获取上次处理的最后一条消息ID，优先使用内存缓存中的数据"""
    # 首先检查内存缓存中是否有数据
    if schedule_file in _schedule_cache:
        return _schedule_cache[schedule_file]
    
    # 如果内存中没有，则从文件中读取
    if os.path.exists(schedule_file):
        try:
            with open(schedule_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_id = data.get('last_message_id', 0)
                # 更新内存缓存
                _schedule_cache[schedule_file] = last_id
                return last_id
        except Exception as e:
            print(f"Error reading schedule file: {e}")
            # 如果读取失败，默认返回0并更新缓存
            _schedule_cache[schedule_file] = 0
            return 0
    else:
        # 文件不存在，返回0并更新缓存
        _schedule_cache[schedule_file] = 0
        return 0

def update_last_message_id(schedule_file, message_id):
    """更新schedule文件和内存缓存中的最后处理消息ID"""
    # 首先更新内存缓存
    _schedule_cache[schedule_file] = message_id
    
    # 然后更新文件
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(schedule_file), exist_ok=True)
        
        # 写入文件
        with open(schedule_file, 'w', encoding='utf-8') as f:
            json.dump({'last_message_id': message_id}, f)
            # 确保数据写入磁盘
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"Error updating schedule file: {e}")
        traceback.print_exc()

async def qr_login():
    """Login using QR code"""
    print("Generating QR code for login...")
    
    # Create a temporary file for the QR code
    qr_file = os.path.join(tempfile.gettempdir(), 'telegram_login_qr.png')
    
    # Generate QR code
    qr_login = await client.qr_login()
    
    # Get QR code as bytes and save to file
    qr_code = qr_login.get_qr()
    with open(qr_file, 'wb') as f:
        f.write(qr_code)
    
    print(f"QR code saved to {qr_file}")
    
    # Open the QR code with system default viewer
    if sys.platform == "darwin":  # macOS
        subprocess.run(["open", qr_file])
    elif sys.platform == "win32":  # Windows
        os.startfile(qr_file)
    elif sys.platform == "linux":  # Linux
        subprocess.run(["xdg-open", qr_file])
    
    # Wait for user to scan QR code
    print("Please scan the QR code with your Telegram app")
    await qr_login.wait()
    print("Login successful!")

async def list_dialogs(limit=None):
    """List all dialogs (chats and channels)
    
    This function automatically fetches all dialogs using Telethon's built-in 
    get_dialogs method, which is highly optimized and caches entity data.
    """
    from rich.console import Console
    from rich.table import Table
    from telethon.tl.types import Channel, Chat, User
    
    console = Console()
    
    # 获取任务列表中的配置的所有 ID
    task_ids = set()
    for task_file_name in ["task.yml", "task1.yml", "task1 copy.yml"]:
        if os.path.exists(task_file_name):
            try:
                task_data = load_task_file(task_file_name)
                if task_data and 'tasks' in task_data and isinstance(task_data['tasks'], list):
                    for task in task_data['tasks']:
                        if 'channel_id' in task: task_ids.add(str(task['channel_id']))
                        elif 'id' in task: task_ids.add(str(task['id']))
                elif task_data: # single task format
                    if 'channel_id' in task_data: task_ids.add(str(task_data['channel_id']))
                    elif 'id' in task_data: task_ids.add(str(task_data['id']))
            except Exception as e:
                logger.debug(f"Error reading {task_file_name}: {e}")
    
    dialog_rows = []
    
    with console.status("[bold green]Fetching all dialogs (fast mode)...[/]") as status:
        try:
            # 采用高层 get_dialogs API 获取，它能够隐式完成自动实体提取，大幅降低请求耗时
            # Limit=None 指获取所有，强制设置 limit 为 None 确保获取全部结果
            dialogs = await client.get_dialogs(limit=None)
            
            for dialog in dialogs:
                entity = dialog.entity
                name = dialog.name
                
                # 判断类型
                if isinstance(entity, Channel):
                    entity_type = "Supergroup" if getattr(entity, 'megagroup', False) else "Channel"
                elif isinstance(entity, Chat):
                    entity_type = "Group"
                elif isinstance(entity, User):
                    # 按照用户要求，去掉User类型的，只保留group/supergroup/channel
                    continue
                else:
                    entity_type = "Unknown"
                
                entity_id_str = str(dialog.id)
                raw_id = str(getattr(entity, 'id', ''))
                
                in_task_mark = "✅" if (entity_id_str in task_ids or raw_id in task_ids) else ""
                
                dialog_rows.append((in_task_mark, entity_id_str, entity_type, name))
                
        except Exception as e:
            logger.error(f"Error fetching dialogs: {e}")
            traceback.print_exc()
    
    # 采用交互式翻页展示
    import sys
    import tty
    import termios
    import asyncio
    
    def get_keypress():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch += sys.stdin.read(2)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch

    page_size = 30
    total_pages = max(1, (len(dialog_rows) + page_size - 1) // page_size)
    current_page = 0
    loop = asyncio.get_event_loop()
    
    while True:
        console.clear()
        
        table = Table(title=f"Telegram Dialogs (Total Found: {len(dialog_rows)}) - Page {current_page + 1}/{total_pages}", show_header=True, header_style="bold magenta")
        table.add_column("In Task", justify="center", style="green", width=8)
        table.add_column("ID", justify="left", style="cyan")
        table.add_column("Type", justify="left", style="yellow")
        table.add_column("Name", justify="left", style="white")
        
        start_idx = current_page * page_size
        end_idx = min(start_idx + page_size, len(dialog_rows))
        
        for i in range(start_idx, end_idx):
            table.add_row(*dialog_rows[i])
            
        console.print(table)
        console.print("\n[bold cyan]Use ← (Left) / → (Right) arrows to navigate. Press 'q' or Ctrl+C to quit.[/]")
        
        try:
            key = await loop.run_in_executor(None, get_keypress)
        except Exception:
            break
            
        if key.lower() == 'q' or key == '\x03':  # q or Ctrl+C
            console.clear()
            break
        elif key in ('\x1b[D', '\x1b[A'):  # Left / Up
            if current_page > 0:
                current_page -= 1
        elif key in ('\x1b[C', '\x1b[B'):  # Right / Down
            if current_page < total_pages - 1:
                current_page += 1

# 添加文件哈希计算函数
# def calculate_file_hash(file_path):
#     """计算文件的SHA-256哈希值"""
#     sha256_hash = hashlib.sha256()
#     with open(file_path, "rb") as f:
#         # 分块读取文件以处理大文件
#         for byte_block in iter(lambda: f.read(4096), b""):
#             sha256_hash.update(byte_block)
#     return sha256_hash.hexdigest()

# 添加一个函数来处理消息中的所有媒体文件
async def download_all_media_from_message(message, channel_dir, temp_dir_prefix):
    """Download all media files from a message, including albums and forwarded messages"""
    global consecutive_errors
    max_retries = 3  # 最大重试次数
    downloaded_files = []
    media_count = 0
    
    try:
        # 检查是否为转发消息
        is_forwarded = hasattr(message, 'forward') and message.forward is not None
        if is_forwarded:
            logger.info(f"Message ID {message.id} is a forwarded message")
        
        # 检查消息是否有媒体
        if not message.media:
            if is_forwarded:
                logger.info(f"Forwarded message ID {message.id} has no media content")
            return downloaded_files, media_count
        
        # 如果是网页预览，跳过
        if isinstance(message.media, MessageMediaWebPage):
            logger.info(f"Skipping webpage preview in message ID {message.id}")
            return downloaded_files, media_count
        
        # 获取消息类型
        message_type = get_message_type(message).lower()
        
        # 如果是转发消息，在日志中标记
        if is_forwarded:
            logger.info(f"Processing forwarded message ID {message.id} of type {message_type}")
        
        # 确定目标目录
        target_dir = os.path.join(channel_dir, message_type)
        os.makedirs(target_dir, exist_ok=True)
        
        # 处理主媒体文件
        if message.media:
            # 生成一个唯一的文件名前缀，为转发消息添加标记
            file_prefix = f"{message.id}_"
            if is_forwarded:
                file_prefix = f"{message.id}_fwd_"
            
            # 检查目标目录中是否已经存在以该前缀开头的完整文件（不包含"temp"的文件）
            # 修复：确保文件名中不包含"temp"，而不仅仅是不以"temp"结尾
            existing_files = [f for f in os.listdir(target_dir) 
                             if f.startswith(file_prefix) and "temp" not in f]
            
            # 清理可能存在的临时文件（未完成的下载）
            # 修复：查找所有包含"temp"的文件
            temp_files = [f for f in os.listdir(target_dir) 
                         if f.startswith(file_prefix) and "temp" in f]
            for temp_file in temp_files:
                try:
                    os.remove(os.path.join(target_dir, temp_file))
                    logger.info(f"Removed incomplete download: {temp_file}")
                except Exception as e:
                    logger.error(f"Error removing temporary file {temp_file}: {e}")
            
            if existing_files:
                # 文件已存在，跳过下载
                existing_file = existing_files[0]  # 取第一个匹配的文件
                existing_path = os.path.join(target_dir, existing_file)
                logger.info(f"Complete file already exists: {existing_path} - Skipping download")
                downloaded_files.append(existing_path)
                media_count += 1
            else:
                # 文件不存在或只有临时文件，进行下载
                media_count += 1
                
                # 添加重试机制
                for retry_count in range(max_retries):
                    try:
                        # 直接下载到目标目录，使用临时文件名
                        temp_target_path = os.path.join(target_dir, f"{file_prefix}temp")
                        downloaded_path = await client.download_media(message, temp_target_path)
                        
                        if downloaded_path:
                            # 计算文件哈希值
                            # file_hash = calculate_file_hash(downloaded_path)
                            file_hash = "nohash"
                            
                            # 获取文件扩展名
                            _, file_extension = os.path.splitext(downloaded_path)
                            if not file_extension:
                                file_extension = '.bin'
                            
                            # 创建最终文件名，使用哈希值
                            final_path = os.path.join(target_dir, f"{file_prefix}{file_hash[:16]}{file_extension}")
                            
                            # 重命名文件到最终名称
                            os.rename(downloaded_path, final_path)
                            
                            if is_forwarded:
                                logger.info(f"Downloaded forwarded media to {final_path}")
                            else:
                                logger.info(f"Downloaded media to {final_path}")
                            downloaded_files.append(final_path)
                            consecutive_errors = 0  # 重置连续错误计数
                            break  # 成功下载，跳出重试循环
                        else:
                            logger.warning(f"Failed to download media from message ID {message.id} (attempt {retry_count + 1})")
                            if retry_count < max_retries - 1:
                                await asyncio.sleep(1 + retry_count)  # 指数退避
                    except SecurityError as e:
                        consecutive_errors += 1
                        logger.error(f"Security error downloading media from message ID {message.id}: {e}")
                        # 如果是安全错误，尝试重置会话
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            logger.warning(f"Too many consecutive security errors ({consecutive_errors}), resetting session...")
                            await handle_connection_error(e, retry_count, max_retries)
                        # 如果不是最后一次重试，等待后继续
                        if retry_count < max_retries - 1:
                            await asyncio.sleep(2 * (retry_count + 1))
                    except Exception as e:
                        logger.error(f"Error downloading media from message ID {message.id} (attempt {retry_count + 1}): {e}")
                        traceback.print_exc()
                        # 如果不是最后一次重试，等待后继续
                        if retry_count < max_retries - 1:
                            await asyncio.sleep(1 + retry_count)  # 指数退避
                            
                # 清理可能存在的临时文件
                temp_target_path = os.path.join(target_dir, f"{file_prefix}temp")
                if os.path.exists(temp_target_path):
                    try:
                        os.remove(temp_target_path)
                    except:
                        pass
        
        # 处理消息中的多媒体内容 - 改进版
        if hasattr(message, 'media') and hasattr(message.media, 'document'):
            # 检查是否有多个媒体 (检查document中的attributes)
            try:
                # 对于包含多个图片的消息，Telegram通常会将它们作为document中的属性保存
                # 我们需要遍历这些属性，查找表示多个媒体的特征
                
                # 检查文档是否有GroupedMedia属性
                has_grouped_media = False
                
                # 获取文档的MIME类型
                mime_type = message.media.document.mime_type if hasattr(message.media.document, 'mime_type') else ''
                
                # 检查文档是否有对应多媒体的属性
                for attribute in message.media.document.attributes:
                    if hasattr(attribute, 'file_name') and attribute.file_name:
                        # 可能是包含多个媒体的文件
                        if mime_type.startswith('image/'): # or mime_type.startswith('video/'):
                            has_grouped_media = True
                            break
                
                if has_grouped_media:
                    # 生成一个唯一的文件名前缀，为转发消息添加标记
                    grouped_prefix = f"{message.id}_grouped_"
                    if is_forwarded:
                        grouped_prefix = f"{message.id}_fwd_grouped_"
                    
                    # 检查目标目录中是否已经存在以该前缀开头的文件
                    # 修复：确保文件名中不包含"temp"，而不仅仅是不以"temp"结尾
                    existing_grouped_files = [f for f in os.listdir(target_dir) 
                                             if f.startswith(grouped_prefix) and "temp" not in f]
                    
                    # 清理可能存在的临时文件（未完成的下载）
                    # 修复：查找所有包含"temp"的文件
                    grouped_temp_files = [f for f in os.listdir(target_dir) 
                                         if f.startswith(grouped_prefix) and "temp" in f]
                    for grouped_temp_file in grouped_temp_files:
                        try:
                            os.remove(os.path.join(target_dir, grouped_temp_file))
                            logger.info(f"Removed incomplete grouped download: {grouped_temp_file}")
                        except Exception as e:
                            logger.error(f"Error removing temporary grouped file {grouped_temp_file}: {e}")
                    
                    if existing_grouped_files:
                        # 文件已存在，跳过下载
                        existing_grouped_file = existing_grouped_files[0]  # 取第一个匹配的文件
                        existing_grouped_path = os.path.join(target_dir, existing_grouped_file)
                        logger.info(f"Complete grouped file already exists: {existing_grouped_path} - Skipping download")
                        downloaded_files.append(existing_grouped_path)
                        media_count += 1
                    else:
                        # 文件不存在或只有临时文件，进行下载
                        # 添加重试机制
                        for retry_count in range(max_retries):
                            try:
                                grouped_temp_target_path = os.path.join(target_dir, f"{grouped_prefix}temp")
                                grouped_downloaded_path = await client.download_media(message.media.document, grouped_temp_target_path)
                                
                                if grouped_downloaded_path:
                                    # grouped_file_hash = calculate_file_hash(grouped_downloaded_path)
                                    grouped_file_hash = "nohash"
                                    
                                    _, grouped_file_extension = os.path.splitext(grouped_downloaded_path)
                                    if not grouped_file_extension:
                                        grouped_file_extension = '.bin'
                                    
                                    grouped_final_path = os.path.join(
                                        target_dir, 
                                        f"{grouped_prefix}{grouped_file_hash[:16]}{grouped_file_extension}"
                                    )
                                    
                                    os.rename(grouped_downloaded_path, grouped_final_path)
                                    
                                    if is_forwarded:
                                        logger.info(f"Downloaded forwarded grouped media to {grouped_final_path}")
                                    else:
                                        logger.info(f"Downloaded grouped media to {grouped_final_path}")
                                    downloaded_files.append(grouped_final_path)
                                    media_count += 1
                                    consecutive_errors = 0  # 重置连续错误计数
                                    break  # 成功下载，跳出重试循环
                                else:
                                    logger.warning(f"Failed to download grouped media from message ID {message.id} (attempt {retry_count + 1})")
                                    if retry_count < max_retries - 1:
                                        await asyncio.sleep(1 + retry_count)  # 指数退避
                            except SecurityError as e:
                                consecutive_errors += 1
                                logger.error(f"Security error downloading grouped media from message ID {message.id}: {e}")
                                # 如果是安全错误，尝试重置会话
                                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                                    logger.warning(f"Too many consecutive security errors ({consecutive_errors}), resetting session...")
                                    await handle_connection_error(e, retry_count, max_retries)
                                # 如果不是最后一次重试，等待后继续
                                if retry_count < max_retries - 1:
                                    await asyncio.sleep(2 * (retry_count + 1))
                            except Exception as e:
                                logger.error(f"Error downloading grouped media from message ID {message.id} (attempt {retry_count + 1}): {e}")
                                traceback.print_exc()
                                # 如果不是最后一次重试，等待后继续
                                if retry_count < max_retries - 1:
                                    await asyncio.sleep(1 + retry_count)  # 指数退避
                                    
                        # 清理可能存在的临时文件
                        grouped_temp_target_path = os.path.join(target_dir, f"{grouped_prefix}temp")
                        if os.path.exists(grouped_temp_target_path):
                            try:
                                os.remove(grouped_temp_target_path)
                            except:
                                pass
                 
            except Exception as e:
                logger.error(f"Error checking for grouped media in message ID {message.id}: {e}")
                traceback.print_exc()
        
        return downloaded_files, media_count
        
    except SecurityError as e:
        consecutive_errors += 1
        logger.error(f"Security error in download_all_media_from_message for message ID {message.id if hasattr(message, 'id') else 'unknown'}: {e}")
        # 如果是安全错误，尝试处理它
        await handle_connection_error(e)
        return downloaded_files, media_count
    except Exception as e:
        logger.error(f"Error in download_all_media_from_message for message ID {message.id if hasattr(message, 'id') else 'unknown'}: {e}")
        traceback.print_exc()
        return downloaded_files, media_count

# 添加一个函数来检查消息是否是相册，并获取相册信息
async def get_message_info(message):
    """获取消息的详细信息，包括类型和相册信息"""
    global consecutive_errors
    try:
        message_type = get_message_type(message).lower()
        is_album = hasattr(message, 'grouped_id') and message.grouped_id is not None
        album_size = 0
        
        # 如果是相册，尝试获取相册大小
        if is_album:
            try:
                # 获取相册ID
                album_id = message.grouped_id
                # 尝试获取相册中的消息数量
                # 注意：这里不直接获取消息内容，只是尝试获取数量信息
                # 由于之前的错误，我们不使用client.get_messages(ids=album_id)
                # 相册消息通常在时间上接近，可以尝试获取时间范围内的消息
                # 这只是一个估计，可能不准确
                album_size = "未知"  # 默认值
            except SecurityError as e:
                # 安全错误，增加连续错误计数
                consecutive_errors += 1
                logger.error(f"Security error getting album info for message ID {message.id}: {e}")
                # 不抛出异常，继续使用已知信息
            except Exception as e:
                logger.error(f"Error getting album info for message ID {message.id}: {e}")
        
        consecutive_errors = 0  # 成功完成，重置连续错误计数
        
        return {
            "type": message_type,
            "is_album": is_album,
            "album_id": message.grouped_id if is_album else None,
            "album_size": album_size
        }
    except Exception as e:
        logger.error(f"Error in get_message_info for message ID {message.id if hasattr(message, 'id') else 'unknown'}: {e}")
        # 返回默认信息以避免中断处理流程
        return {
            "type": "unknown",
            "is_album": False,
            "album_id": None,
            "album_size": 0
        }

async def download_from_task(task_file, output_dir, sleep_ms=500, msg_limit=500):
    """Download media from a specified task file"""
    try:
        # 加载任务文件
        task_config = load_task_file(task_file)
        
        # 检查是否有tasks列表
        if 'tasks' in task_config and isinstance(task_config['tasks'], list):
            print(f"Found {len(task_config['tasks'])} tasks in the task file")
            
            # 处理每个任务
            completed_tasks = 0
            for task_index, task in enumerate(task_config['tasks']):
                print(f"\n{'='*50}")
                print(f"Processing task {task_index+1}/{len(task_config['tasks'])}")
                print(f"{'='*50}")
                
                # 获取目标频道（兼容多种键名）
                channel_id = None
                channel_name = None
                
                # 获取频道ID
                if 'channel_id' in task:
                    channel_id = task['channel_id']
                elif 'id' in task:
                    channel_id = task['id']
                else:
                    print(f"Error: No channel ID found in task #{task_index+1}. Skipping.")
                    continue
                
                # 获取频道名称
                if 'name' in task:
                    channel_name = task['name']
                else:
                    channel_name = f"channel_{channel_id}"
                
                print(f"Using channel ID: {channel_id}, name: {channel_name}")
                
                # 获取媒体类型限制
                allowed_types = []  # 默认为空列表
                if 'type' in task and task['type']:
                    # 解析类型配置，确保所有类型都转换为小写并去除空白
                    allowed_types = [t.strip().lower() for t in task['type'].split(',') if t.strip()]
                    # image 已合并到 video 目录，将 image 类型映射为 video
                    allowed_types = ['video' if t == 'image' else t for t in allowed_types]
                    allowed_types = list(dict.fromkeys(allowed_types))  # 去重并保持顺序
                    print(f"Only downloading media types: {', '.join(allowed_types)}")
                
                # 获取消息限制
                task_limit = msg_limit
                if 'limit' in task and task['limit'] > 0:
                    task_limit = min(msg_limit, task['limit'])
                    if task_limit != msg_limit:
                        print(f"Using task-specific limit: {task_limit}")
                
                # 获取睡眠时间
                task_sleep_ms = sleep_ms
                if 'sleep_ms' in task and task['sleep_ms'] > 0:
                    task_sleep_ms = task['sleep_ms']
                    if task_sleep_ms != sleep_ms:
                        print(f"Using task-specific sleep: {task_sleep_ms}ms")
                
                # 处理频道名称作为子目录
                safe_channel_name = re.sub(r'[\\/:*?"<>|]', '_', channel_name)
                channel_dir = os.path.join(output_dir, safe_channel_name)
                
                # 检查此任务是否已完成
                schedule_file = os.path.join(channel_dir, "schedule.json")
                
                # 仅当目录已存在时才检查任务状态
                if os.path.exists(channel_dir):
                    try:
                        # 获取上次处理的消息ID
                        last_message_id = get_last_message_id(schedule_file)
                        
                        # 检查是否能获取到实体
                        retry_count = 0
                        max_retries = 3
                        entity = None
                        
                        while retry_count < max_retries:
                            try:
                                entity = await client.get_entity(channel_id)
                                break
                            except Exception as e:
                                retry_count += 1
                                if retry_count < max_retries:
                                    await asyncio.sleep(2 * retry_count)
                                else:
                                    logger.error(f"Could not get entity for channel {channel_id}: {e}")
                        
                        if entity:
                            # 获取频道的最新消息ID
                            retry_count = 0
                            latest_message_id = 0
                            
                            while retry_count < max_retries:
                                try:
                                    latest_messages = await client.get_messages(entity, limit=1)
                                    if latest_messages and len(latest_messages) > 0:
                                        latest_message_id = latest_messages[0].id
                                        logger.info(f"Latest message ID: {latest_message_id}")
                                    break
                                except Exception as e:
                                    retry_count += 1
                                    if retry_count < max_retries:
                                        await asyncio.sleep(2 * retry_count)
                                    else:
                                        logger.error(f"Could not get latest message for channel {channel_id}: {e}")
                            
                            # 检查任务是否已完成
                            if last_message_id >= latest_message_id and latest_message_id > 0:
                                logger.info(f"Task for channel {channel_name} already completed!")
                                logger.info(f"Last processed message ID: {last_message_id}, Latest message ID: {latest_message_id}")
                                logger.info("Skipping this channel as it's already fully downloaded.")
                                completed_tasks += 1
                                continue
                    except Exception as e:
                        logger.error(f"Error checking task status for channel {channel_name}: {e}")
                        # 继续处理，即使检查失败
                
                # 处理这个任务
                await process_single_channel(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    output_dir=output_dir,
                    sleep_ms=task_sleep_ms,
                    msg_limit=task_limit,
                    allowed_types=allowed_types
                )
            
            logger.info(f"Task processing complete. {completed_tasks} tasks were already completed.")
        else:
            # 兼容单任务格式（直接在顶层有channel_id/id）
            # 获取目标频道（兼容多种键名）
            channel_id = None
            if 'channel_id' in task_config:
                channel_id = task_config['channel_id']
            elif 'id' in task_config:
                channel_id = task_config['id']
            else:
                print("Error: No channel ID found in task file. Please use 'id' or 'channel_id' key.")
                return
            
            channel_name = task_config.get('name', f"channel_{channel_id}")
            print(f"Using channel ID: {channel_id}, name: {channel_name}")
            
            # 获取消息限制
            if 'limit' in task_config and task_config['limit'] > 0:
                msg_limit = min(msg_limit, task_config['limit'])
            
            # 获取睡眠时间
            if 'sleep_ms' in task_config and task_config['sleep_ms'] > 0:
                sleep_ms = task_config['sleep_ms']
            
            # 获取媒体类型限制
            allowed_types = []  # 默认为空列表
            if 'type' in task_config and task_config['type']:
                # 解析类型配置，确保所有类型都转换为小写并去除空白
                allowed_types = [t.strip().lower() for t in task_config['type'].split(',') if t.strip()]
                # image 已合并到 video 目录，将 image 类型映射为 video
                allowed_types = ['video' if t == 'image' else t for t in allowed_types]
                allowed_types = list(dict.fromkeys(allowed_types))  # 去重并保持顺序
                print(f"Only downloading media types: {', '.join(allowed_types)}")
            else:
                # 如果没有指定类型，则不下载任何内容
                print("No media types specified in task. No media will be downloaded.")
                allowed_types = []  # 空列表表示不下载任何类型
            
            # 处理频道名称作为子目录
            safe_channel_name = re.sub(r'[\\/:*?"<>|]', '_', channel_name)
            channel_dir = os.path.join(output_dir, safe_channel_name)
            
            # 检查此任务是否已完成
            schedule_file = os.path.join(channel_dir, "schedule.json")
            
            # 仅当目录已存在时才检查任务状态
            if os.path.exists(channel_dir):
                try:
                    # 获取上次处理的消息ID
                    last_message_id = get_last_message_id(schedule_file)
                    
                    # 尝试获取实体
                    retry_count = 0
                    max_retries = 3
                    entity = None
                    
                    while retry_count < max_retries:
                        try:
                            entity = await client.get_entity(channel_id)
                            break
                        except Exception as e:
                            retry_count += 1
                            if retry_count < max_retries:
                                await asyncio.sleep(2 * retry_count)
                            else:
                                logger.error(f"Could not get entity for channel {channel_id}: {e}")
                    
                    if entity:
                        # 获取频道的最新消息ID
                        retry_count = 0
                        latest_message_id = 0
                        
                        while retry_count < max_retries:
                            try:
                                latest_messages = await client.get_messages(entity, limit=1)
                                if latest_messages and len(latest_messages) > 0:
                                    latest_message_id = latest_messages[0].id
                                    logger.info(f"Latest message ID: {latest_message_id}")
                                break
                            except Exception as e:
                                retry_count += 1
                                if retry_count < max_retries:
                                    await asyncio.sleep(2 * retry_count)
                                else:
                                    logger.error(f"Could not get latest message for channel {channel_id}: {e}")
                        
                        # 检查任务是否已完成
                        if last_message_id >= latest_message_id and latest_message_id > 0:
                            logger.info(f"Task for channel {channel_name} already completed!")
                            logger.info(f"Last processed message ID: {last_message_id}, Latest message ID: {latest_message_id}")
                            logger.info("Skipping this channel as it's already fully downloaded.")
                            return
                except Exception as e:
                    logger.error(f"Error checking task status for channel {channel_name}: {e}")
                    # 继续处理，即使检查失败
            
            # 处理这个任务
            await process_single_channel(
                channel_id=channel_id,
                channel_name=channel_name,
                output_dir=output_dir,
                sleep_ms=sleep_ms,
                msg_limit=msg_limit,
                allowed_types=allowed_types
            )
    
    except Exception as e:
        logger.error(f"Error in download_from_task: {e}")
        traceback.print_exc()

# 添加一个新函数处理单个频道的下载
async def process_single_channel(channel_id, channel_name, output_dir, sleep_ms=500, msg_limit=500, allowed_types=None):
    """处理单个频道的下载任务"""
    global consecutive_errors
    try:
        # 获取输出目录
        if not output_dir:
            output_dir = 'downloads'
        
        # 处理频道名称作为子目录
        try:
            # 重试获取实体的机制
            retry_count = 0
            max_retries = 3
            entity = None
            
            while retry_count < max_retries:
                try:
                    entity = await client.get_entity(channel_id)
                    consecutive_errors = 0  # 重置连续错误计数
                    logger.info(f"Found entity: {entity.title if hasattr(entity, 'title') else channel_id}")
                    break
                except (SecurityError, UnauthorizedError) as e:
                    logger.error(f"Security or auth error while getting entity: {e}")
                    if await handle_connection_error(e, retry_count, max_retries):
                        retry_count += 1
                        continue
                    else:
                        raise
                except Exception as e:
                    logger.error(f"Error getting entity: {e}")
                    retry_count += 1
                    if retry_count < max_retries:
                        await asyncio.sleep(2 * retry_count)  # 指数退避
                    else:
                        raise
            
            if not entity:
                raise Exception(f"Failed to get entity for channel ID {channel_id} after {max_retries} retries")
            
            # 如果未提供名称，则使用实体标题
            if not channel_name and hasattr(entity, 'title'):
                channel_name = entity.title
            
            # 确保频道名称是合法的文件名
            channel_name = re.sub(r'[\\/:*?"<>|]', '_', channel_name)
            channel_dir = os.path.join(output_dir, channel_name)
            
            # 创建输出目录
            os.makedirs(channel_dir, exist_ok=True)
            
            # 创建各媒体类型的子目录
            for media_type in ['text', 'video', 'voice', 'audio', 'document']:
                media_dir = os.path.join(channel_dir, media_type)
                os.makedirs(media_dir, exist_ok=True)

            # 获取频道的所有消息
            logger.info(f"Downloading media from {channel_name} ({channel_id})")
            
            # 计算批处理大小
            batch_size = 100  # 默认批处理大小
            
            # 获取消息计数和计算总批次
            first_message_id = 0
            latest_message_id = 0
            
            try:
                # 获取频道的第一条（最早）消息 - 添加重试机制
                retry_count = 0
                while retry_count < max_retries:
                    try:
                        first_messages = await client.get_messages(entity, limit=1, reverse=True)
                        consecutive_errors = 0  # 重置连续错误计数
                        if first_messages and len(first_messages) > 0:
                            first_message_id = first_messages[0].id
                            logger.info(f"First message ID: {first_message_id}")
                        break
                    except (SecurityError, UnauthorizedError) as e:
                        logger.error(f"Security error getting first message: {e}")
                        if await handle_connection_error(e, retry_count, max_retries):
                            retry_count += 1
                            continue
                        else:
                            raise
                    except Exception as e:
                        logger.error(f"Error getting first message: {e}")
                        retry_count += 1
                        if retry_count < max_retries:
                            await asyncio.sleep(2 * retry_count)
                        else:
                            raise
                
                # 获取频道的最新消息 - 添加重试机制
                retry_count = 0
                while retry_count < max_retries:
                    try:
                        latest_messages = await client.get_messages(entity, limit=1)
                        consecutive_errors = 0  # 重置连续错误计数
                        if latest_messages and len(latest_messages) > 0:
                            latest_message_id = latest_messages[0].id
                            logger.info(f"Latest message ID: {latest_message_id}")
                        break
                    except (SecurityError, UnauthorizedError) as e:
                        logger.error(f"Security error getting latest message: {e}")
                        if await handle_connection_error(e, retry_count, max_retries):
                            retry_count += 1
                            continue
                        else:
                            raise
                    except Exception as e:
                        logger.error(f"Error getting latest message: {e}")
                        retry_count += 1
                        if retry_count < max_retries:
                            await asyncio.sleep(2 * retry_count)
                        else:
                            raise
                
                # 计算总消息数的估计值
                # 注意：实际消息数可能少于此估计值，因为消息ID可能不连续
                total_messages = latest_message_id - first_message_id + 1
                logger.info(f"Estimated total messages (based on ID range): {total_messages}")
                
                # 使用更简单的方法尝试获取消息数量
                try:
                    # 使用client.get_messages的limit=0参数获取总消息数
                    retry_count = 0
                    message_count = None
                    
                    while retry_count < max_retries and message_count is None:
                        try:
                            message_count = await client.get_messages(entity, limit=0)
                            consecutive_errors = 0  # 重置连续错误计数
                            if hasattr(message_count, 'total'):
                                logger.info(f"Actual message count: {message_count.total}")
                                # 更新total_messages为实际值
                                total_messages = message_count.total
                        except (SecurityError, UnauthorizedError) as e:
                            logger.error(f"Security error getting message count: {e}")
                            if await handle_connection_error(e, retry_count, max_retries):
                                retry_count += 1
                                continue
                            else:
                                break
                        except Exception as e:
                            logger.error(f"Note: Could not get exact message count: {e}")
                            # 这里不是关键错误，继续使用估计值
                            break
                except Exception as e:
                    logger.error(f"Error with message count: {e}")
                    # 这里不是关键错误，继续使用估计值
                    pass
            except Exception as e:
                logger.error(f"Error getting message range: {e}")
                total_messages = None
                traceback.print_exc()
            
            # 确定起始消息ID（从最早的消息或从上次的进度开始）
            schedule_file = os.path.join(channel_dir, "schedule.json")
            
            # 获取上次处理的消息ID（优先使用内存缓存）
            last_message_id = get_last_message_id(schedule_file)
            
            # 检查任务是否已完成
            if last_message_id >= latest_message_id and latest_message_id > 0:
                logger.info(f"Task already completed! Last processed message ID ({last_message_id}) >= Latest message ID ({latest_message_id})")
                logger.info("Skipping this channel as it's already fully downloaded.")
                return
            
            # 如果有上次的进度，从上次位置继续；否则从第一条消息开始
            if last_message_id and last_message_id > first_message_id:
                current_id = last_message_id + 1  # 从下一条消息开始
                logger.info(f"Resuming from message ID: {current_id}")
            else:
                current_id = first_message_id  # 从第一条消息开始
                logger.info(f"Starting from the first message (ID: {current_id})")
            
            # 如果无法确定起始ID，则退出
            if current_id == 0:
                logger.warning("Could not determine a valid starting message ID")
                return
            
            # 初始化变量
            total_downloaded = 0
            batch_number = 1
            has_more_messages = True
            
            # 创建一个集合来存储已经处理过的相册ID
            processed_albums = set()
            
            # 创建一个集合来存储已经处理过的消息ID，避免重复处理
            processed_messages = set()
            
            # 创建一个集合来存储已经作为相册一部分处理过的消息ID
            processed_album_message_ids = set()
            
            # 计算总批次数和进度跟踪
            total_batches = None
            messages_to_process = None
            
            if total_messages is not None and current_id <= latest_message_id:
                # 计算还需要处理的消息数
                messages_to_process = latest_message_id - current_id + 1
                logger.info(f"Estimated messages to process: {messages_to_process}")
                
                # 如果设置了消息限制，取较小值
                if msg_limit > 0:
                    messages_to_process = min(messages_to_process, msg_limit)
                    logger.info(f"Limited to {messages_to_process} messages due to limit setting")
                
                # 计算总批次数
                total_batches = (messages_to_process + batch_size - 1) // batch_size if messages_to_process > 0 else 0
                logger.info(f"Estimated total batches: {total_batches}")
            
            # 追踪处理的消息计数
            processed_count = 0
            max_id_to_process = min(latest_message_id, current_id + msg_limit - 1) if msg_limit > 0 else latest_message_id
            
            # 改用传统的消息获取方法，而不是基于ID范围
            logger.info(f"Starting message processing from message ID {current_id}")
            
            # 使用min_id来获取消息，从current_id开始
            min_id = current_id - 1  # min_id是排他的，所以减1以包含current_id
            
            while has_more_messages and processed_count < msg_limit:
                # 显示当前批次和总批次
                if total_batches:
                    progress_percent = min(100.0, (batch_number / total_batches * 100))
                    logger.info(f"Processing batch {batch_number}/{total_batches} ({progress_percent:.1f}%)")
                else:
                    logger.info(f"Processing batch #{batch_number}")
                
                logger.info(f"Getting messages with min_id: {min_id}, limit: {batch_size}")
                
                try:
                    # 获取一批消息 - 使用min_id和limit
                    # 这种方法会获取ID大于min_id的消息，按ID降序排列
                    retry_count = 0
                    messages = None
                    
                    while retry_count < max_retries and messages is None:
                        try:
                            messages = await client.get_messages(
                                entity,
                                limit=batch_size,
                                min_id=min_id
                            )
                            consecutive_errors = 0  # 成功获取消息，重置连续错误计数
                        except (SecurityError, UnauthorizedError) as e:
                            logger.error(f"Security error getting messages with min_id {min_id}: {e}")
                            if await handle_connection_error(e, retry_count, max_retries):
                                retry_count += 1
                                continue
                            else:
                                # 如果重试失败，尝试跳过当前批次
                                logger.warning(f"Skipping batch with min_id {min_id} after repeated errors")
                                messages = []
                                break
                        except Exception as e:
                            logger.error(f"Error getting messages with min_id {min_id}: {e}")
                            retry_count += 1
                            if retry_count < max_retries:
                                await asyncio.sleep(2 * retry_count)  # 指数退避
                            else:
                                # 如果重试失败，尝试跳过当前批次
                                logger.warning(f"Skipping batch with min_id {min_id} after {max_retries} retries")
                                messages = []
                    
                    if not messages or len(messages) == 0:
                        logger.info(f"No more messages found with min_id {min_id}")
                        has_more_messages = False
                        break
                    
                    # 过滤掉已经处理过的消息
                    valid_messages = []
                    for msg in messages:
                        if msg.id not in processed_messages:
                            valid_messages.append(msg)
                    
                    # 如果没有有效消息，停止处理
                    if not valid_messages:
                        logger.info("No new valid messages found")
                        has_more_messages = False
                        break
                    
                    # 确保消息按ID排序（从小到大，即时间从早到晚）
                    valid_messages = sorted(valid_messages, key=lambda m: m.id)
                    
                    logger.info(f"Retrieved {len(valid_messages)} valid messages in this batch (ID range: {valid_messages[0].id} to {valid_messages[-1].id})")
                    
                    # 更新min_id为当前批次最大的消息ID
                    min_id = valid_messages[-1].id
                    
                    # 处理这批消息
                    for message in valid_messages:
                        logger.info(f"Processing message ID: {message.id}")
                        
                        # 检查是否为转发消息并添加详细调试信息
                        is_forwarded = hasattr(message, 'forward') and message.forward is not None
                        if is_forwarded:
                            logger.info(f"DEBUG: Found forwarded message {message.id}")
                            logger.info(f"DEBUG: Message text: {message.text[:100] if message.text else 'None'}...")
                            logger.info(f"DEBUG: Message media exists: {message.media is not None}")
                            if message.media:
                                logger.info(f"DEBUG: Media type: {type(message.media).__name__}")
                        
                        # 如果消息已经处理过，跳过
                        if message.id in processed_messages:
                            logger.info(f"Skipping already processed message ID {message.id}")
                            continue
                        
                        # 如果消息已经作为相册的一部分被处理过，也跳过
                        if message.id in processed_album_message_ids:
                            logger.info(f"Skipping message ID {message.id} - already processed as part of an album")
                            processed_messages.add(message.id)
                            processed_count += 1
                            # 更新进度（同时更新内存和文件）
                            update_last_message_id(schedule_file, message.id)
                            continue
                        
                        # 将消息标记为已处理
                        processed_messages.add(message.id)
                        processed_count += 1
                        
                        # 获取消息详细信息 - 添加重试机制
                        retry_count = 0
                        message_info = None
                        
                        while retry_count < max_retries and message_info is None:
                            try:
                                message_info = await get_message_info(message)
                                consecutive_errors = 0  # 重置连续错误计数
                            except (SecurityError, UnauthorizedError) as e:
                                logger.error(f"Security error getting message info for ID {message.id}: {e}")
                                if await handle_connection_error(e, retry_count, max_retries):
                                    retry_count += 1
                                    continue
                                else:
                                    # 如果重试失败，使用默认值
                                    message_info = {"type": "unknown", "is_album": False}
                                    break
                            except Exception as e:
                                logger.error(f"Error getting message info for ID {message.id}: {e}")
                                retry_count += 1
                                if retry_count < max_retries:
                                    await asyncio.sleep(2 * retry_count)
                                else:
                                    # 如果重试失败，使用默认值
                                    message_info = {"type": "unknown", "is_album": False}
                        
                        message_type = message_info["type"]
                        logger.info(f"Message type: {message_type}")
                        
                        # 为转发消息添加额外的调试信息
                        if is_forwarded:
                            logger.info(f"DEBUG: Forwarded message {message.id} type determined as: {message_type}")
                            if message_type == "unknown":
                                logger.warning(f"DEBUG: Forwarded message {message.id} could not determine type - this might be why it's not downloading")
                        
                        # 检查当前消息类型是否在允许下载的类型列表中
                        # 如果不在allowed_types中且不是"all"，则跳过此消息
                        if "all" not in allowed_types and message_type not in allowed_types:
                            logger.info(f"Skipping message ID {message.id} of type {message_type} (not in allowed types: {', '.join(allowed_types)})")
                            # 更新进度（同时更新内存和文件）
                            update_last_message_id(schedule_file, message.id)
                            continue
                        
                        # 检查是否是相册消息
                        is_album = message_info.get("is_album", False)
                        album_id = message_info.get("album_id")
                        
                        # 预先标记哪些相册消息已经处理过
                        album_messages_for_batch = []
                        
                        # 如果是相册消息并且这个相册还没有处理过，提前获取整个相册
                        if is_album and album_id and album_id not in processed_albums:
                            processed_albums.add(album_id)
                            logger.info(f"Processing album {album_id} for the first time")
                            
                            # 获取相册中的所有消息
                            try:
                                # 获取消息所在的会话
                                chat_entity = message.chat if hasattr(message, 'chat') else message.peer_id
                                
                                # 获取消息ID附近的消息 - 添加重试机制
                                retry_count = 0
                                album_messages = None
                                
                                while retry_count < max_retries and album_messages is None:
                                    try:
                                        album_messages = await client.get_messages(
                                            entity=chat_entity,
                                            offset_id=message.id,
                                            limit=100  # 增加限制以获取更多可能的相册消息
                                        )
                                        consecutive_errors = 0  # 重置连续错误计数
                                    except (SecurityError, UnauthorizedError) as e:
                                        logger.error(f"Security error getting album messages for album {album_id}: {e}")
                                        if await handle_connection_error(e, retry_count, max_retries):
                                            retry_count += 1
                                            continue
                                        else:
                                            # 如果重试失败，使用空列表
                                            album_messages = []
                                            break
                                    except Exception as e:
                                        logger.error(f"Error getting album messages for album {album_id}: {e}")
                                        retry_count += 1
                                        if retry_count < max_retries:
                                            await asyncio.sleep(2 * retry_count)
                                        else:
                                            # 如果重试失败，使用空列表
                                            album_messages = []
                                
                                # 筛选出同一相册的消息
                                same_album_messages = [msg for msg in album_messages if 
                                                    hasattr(msg, 'grouped_id') and 
                                                    msg.grouped_id == album_id]
                                
                                logger.info(f"Found {len(same_album_messages)} messages in album {album_id}")
                                
                                # 收集所有相册消息ID，稍后标记为已处理
                                album_message_ids = [msg.id for msg in same_album_messages]
                                album_messages_for_batch = same_album_messages
                                
                                # 先将当前消息从相册消息中排除（因为我们已经在处理它了）
                                album_messages_for_batch = [msg for msg in album_messages_for_batch if msg.id != message.id]
                                
                                # 更新相册消息ID集合
                                for album_msg_id in album_message_ids:
                                    if album_msg_id != message.id:  # 不要标记当前正在处理的消息
                                        processed_album_message_ids.add(album_msg_id)
                            except Exception as e:
                                logger.error(f"Error retrieving album messages for album {album_id}: {e}")
                                traceback.print_exc()
                        elif is_album and album_id:
                            logger.info(f"Found another message (ID: {message.id}) from album {album_id} - already processed")
                        
                        # 处理媒体文件
                        if message_type not in ["text", "unknown"]:
                            # 确保媒体类型目录存在
                            media_type_dir = os.path.join(channel_dir, message_type)
                            os.makedirs(media_type_dir, exist_ok=True)
                            
                            # 下载媒体文件
                            try:
                                temp_dir_prefix = f"{channel_name}_{channel_id}"
                                
                                # 添加重试机制下载媒体
                                retry_count = 0
                                download_success = False
                                
                                while retry_count < max_retries and not download_success:
                                    try:
                                        downloaded_files, media_count = await download_all_media_from_message(
                                            message, channel_dir, temp_dir_prefix
                                        )
                                        consecutive_errors = 0  # 重置连续错误计数
                                        download_success = True
                                        
                                        if media_count > 0:
                                            total_downloaded += media_count
                                            logger.info(f"Downloaded {media_count} media files from message ID {message.id}")
                                        else:
                                            logger.info(f"No media files found in message ID {message.id}")
                                    except (SecurityError, UnauthorizedError) as e:
                                        logger.error(f"Security error downloading media from message ID {message.id}: {e}")
                                        if await handle_connection_error(e, retry_count, max_retries):
                                            retry_count += 1
                                            continue
                                        else:
                                            logger.warning(f"Failed to download media from message ID {message.id} after security error")
                                            break
                                    except Exception as e:
                                        logger.error(f"Error downloading media from message ID {message.id}: {e}")
                                        retry_count += 1
                                        if retry_count < max_retries:
                                            await asyncio.sleep(2 * retry_count)
                                        else:
                                            logger.warning(f"Failed to download media from message ID {message.id} after {max_retries} retries")
                            except Exception as e:
                                logger.error(f"Error downloading media from message ID {message.id}: {e}")
                                traceback.print_exc()
                        elif message_type == "text" and message.text:
                            # 保存文本消息到text目录 - 由于上面已经检查过类型，这里不需要再判断
                            text_dir = os.path.join(channel_dir, "text")
                            os.makedirs(text_dir, exist_ok=True)
                            
                            # 创建文本文件名（使用消息ID和时间戳）
                            message_datetime = message.date.strftime("%Y%m%d_%H%M%S") if hasattr(message, 'date') else "unknown_date"
                            text_filename = f"{message.id}_{message_datetime}.txt"
                            text_file_path = os.path.join(text_dir, text_filename)
                            
                            try:
                                # 保存文本内容到文件
                                with open(text_file_path, 'w', encoding='utf-8') as f:
                                    f.write(message.text)
                                logger.info(f"Saved text message ID {message.id} to {text_file_path}")
                                total_downloaded += 1  # 计入总下载数
                            except Exception as e:
                                logger.error(f"Error saving text from message ID {message.id}: {e}")
                                traceback.print_exc()
                        else:
                            logger.info(f"Skipping message ID {message.id} of type {message_type}")
                        
                        # 更新进度（同时更新内存和文件）
                        update_last_message_id(schedule_file, message.id)
                        
                        # 如果有该批次的其他相册消息，立即处理它们以避免后续重复获取
                        for album_msg in album_messages_for_batch:
                            if album_msg.id in processed_messages:
                                continue  # 跳过已处理过的消息
                            
                            # 标记此相册消息为已处理
                            processed_messages.add(album_msg.id)
                            processed_count += 1
                            
                            # 获取消息类型 - 带重试
                            retry_count = 0
                            album_msg_info = None
                            
                            while retry_count < max_retries and album_msg_info is None:
                                try:
                                    album_msg_info = await get_message_info(album_msg)
                                    consecutive_errors = 0  # 重置连续错误计数
                                except (SecurityError, UnauthorizedError) as e:
                                    logger.error(f"Security error getting album message info for ID {album_msg.id}: {e}")
                                    if await handle_connection_error(e, retry_count, max_retries):
                                        retry_count += 1
                                        continue
                                    else:
                                        # 如果重试失败，使用默认值
                                        album_msg_info = {"type": "unknown", "is_album": True}
                                        break
                                except Exception as e:
                                    logger.error(f"Error getting album message info for ID {album_msg.id}: {e}")
                                    retry_count += 1
                                    if retry_count < max_retries:
                                        await asyncio.sleep(2 * retry_count)
                                    else:
                                        # 如果重试失败，使用默认值
                                        album_msg_info = {"type": "unknown", "is_album": True}
                            
                            album_msg_type = album_msg_info["type"]
                            
                            # 处理相册消息的媒体
                            if album_msg_type not in ["text", "unknown"]:
                                try:
                                    # 确保目标目录存在
                                    album_media_dir = os.path.join(channel_dir, album_msg_type)
                                    os.makedirs(album_media_dir, exist_ok=True)
                                    
                                    # 下载相册消息的媒体 - 带重试
                                    retry_count = 0
                                    album_download_success = False
                                    
                                    while retry_count < max_retries and not album_download_success:
                                        try:
                                            album_downloaded_files, album_media_count = await download_all_media_from_message(
                                                album_msg, channel_dir, temp_dir_prefix
                                            )
                                            consecutive_errors = 0  # 重置连续错误计数
                                            album_download_success = True
                                            
                                            if album_media_count > 0:
                                                total_downloaded += album_media_count
                                                logger.info(f"Downloaded {album_media_count} media files from album message ID {album_msg.id}")
                                            else:
                                                logger.info(f"No media files found in album message ID {album_msg.id}")
                                        except (SecurityError, UnauthorizedError) as e:
                                            logger.error(f"Security error downloading media from album message ID {album_msg.id}: {e}")
                                            if await handle_connection_error(e, retry_count, max_retries):
                                                retry_count += 1
                                                continue
                                            else:
                                                logger.warning(f"Failed to download media from album message ID {album_msg.id} after security error")
                                                break
                                        except Exception as e:
                                            logger.error(f"Error downloading media from album message ID {album_msg.id}: {e}")
                                            retry_count += 1
                                            if retry_count < max_retries:
                                                await asyncio.sleep(2 * retry_count)
                                            else:
                                                logger.warning(f"Failed to download media from album message ID {album_msg.id} after {max_retries} retries")
                                except Exception as e:
                                    logger.error(f"Error downloading media from album message ID {album_msg.id}: {e}")
                                    traceback.print_exc()
                            else:
                                logger.info(f"Skipping album message ID {album_msg.id} of type {album_msg_type}")
                            
                            # 更新进度
                            update_last_message_id(schedule_file, album_msg.id)
                        
                        # 进度显示
                        if messages_to_process:
                            progress = min(100.0, (processed_count / messages_to_process * 100))
                            logger.info(f"Overall progress: {processed_count}/{messages_to_process} messages ({progress:.1f}%)")
                        
                        # 暂停以避免请求过快
                        await asyncio.sleep(sleep_ms / 1000)
                        
                        # 如果达到消息限制，则退出
                        if msg_limit > 0 and processed_count >= msg_limit:
                            logger.info(f"Reached message limit of {msg_limit}")
                            has_more_messages = False
                            break
                    
                    # 更新下一批次的起始ID
                    # 使用当前批次最后一条消息的ID + 1
                    last_id_in_batch = messages[-1].id
                    current_id = last_id_in_batch + 1
                    
                    # 更新批次号
                    batch_number += 1
                    
                    # 检查是否需要继续处理更多消息
                    if processed_count >= msg_limit:
                        logger.info("Reached message processing limit")
                        has_more_messages = False
                    
                except Exception as e:
                    logger.error(f"Error processing batch: {e}")
                    traceback.print_exc()
                    # 等待一会儿然后继续
                    await asyncio.sleep(5)
                    # 更新min_id以尝试继续
                    min_id = max(1, min_id - batch_size)
            
            logger.info(f"Total downloaded media files: {total_downloaded}")
            
        except Exception as e:
            logger.error(f"Error processing channel {channel_id}: {e}")
            traceback.print_exc()
    
    except Exception as e:
        logger.error(f"Error in process_single_channel: {e}")
        traceback.print_exc()

async def download_media(chat_id, limit=10, sleep_ms=500, allowed_types=None):
    """Download media from a chat"""
    try:
        # 如果没有指定允许的类型，默认允许所有类型
        if allowed_types is None:
            allowed_types = ["all"]  # 默认下载所有类型
        
        # 获取聊天实体
        entity = await client.get_entity(int(chat_id))
        chat_name = getattr(entity, 'title', getattr(entity, 'first_name', 'chat'))
        
        # 创建下载目录
        download_dir = f"downloads/{chat_name}"
        os.makedirs(download_dir, exist_ok=True)
        
        # 为每种媒体类型创建子目录
        for media_type in ['text', 'video', 'voice', 'audio', 'document']:
            media_dir = os.path.join(download_dir, media_type)
            os.makedirs(media_dir, exist_ok=True)
            
        # 检查是否已完成下载
        schedule_file = os.path.join(download_dir, "schedule.json")
        
        # 如果目录已存在，检查是否已完成下载
        if os.path.exists(schedule_file):
            try:
                # 获取上次处理的消息ID
                last_message_id = get_last_message_id(schedule_file)
                
                # 获取频道的最新消息ID
                latest_messages = await client.get_messages(entity, limit=1)
                if latest_messages and len(latest_messages) > 0:
                    latest_message_id = latest_messages[0].id
                    logger.info(f"Latest message ID: {latest_message_id}")
                    
                    # 检查是否已完成
                    if last_message_id >= latest_message_id and latest_message_id > 0:
                        logger.info(f"Download for {chat_name} already completed!")
                        logger.info(f"Last processed message ID: {last_message_id}, Latest message ID: {latest_message_id}")
                        logger.info("Skipping as messages are already fully downloaded.")
                        return
            except Exception as e:
                logger.error(f"Error checking download status: {e}")
                # 继续处理，即使检查失败
        
        print(f"Downloading media from {chat_name}...")
        print(f"Using sleep interval of {sleep_ms} milliseconds between messages")
        
        # 获取消息
        # 注意：我们不再只获取带媒体的消息，因为我们也可能需要处理文本消息
        messages = await client.get_messages(entity, limit=limit)
        
        # 按ID升序排序以顺序处理
        messages = sorted(messages, key=lambda m: m.id)
        
        # 跟踪统计信息
        total_files = len(messages)
        skipped_files = 0
        downloaded_files = 0
        
        for message in messages:
            # 检查是否为转发消息
            is_forwarded = hasattr(message, 'forward') and message.forward is not None
            
            # 获取消息类型
            message_type = get_message_type(message)
            
            # 为转发消息添加日志信息
            if is_forwarded:
                print(f"Processing forwarded message ID {message.id} of type: {message_type}")
            else:
                print(f"Processing message ID {message.id} of type: {message_type}")
            
            # 检查类型是否在允许列表中
            if "all" not in allowed_types and message_type not in allowed_types:
                print(f"Skipping message ID {message.id} of type {message_type} (not in allowed types: {', '.join(allowed_types)})")
                skipped_files += 1
                continue
            
            # 根据消息类型处理
            if message_type == "text" and message.text:
                # 保存文本消息
                text_dir = os.path.join(download_dir, "text")
                message_datetime = message.date.strftime("%Y%m%d_%H%M%S") if hasattr(message, 'date') else "unknown_date"
                
                # 为转发消息添加标记
                if is_forwarded:
                    text_filename = f"{message.id}_fwd_{message_datetime}.txt"
                else:
                    text_filename = f"{message.id}_{message_datetime}.txt"
                
                text_file_path = os.path.join(text_dir, text_filename)
                
                try:
                    with open(text_file_path, 'w', encoding='utf-8') as f:
                        f.write(message.text)
                    
                    if is_forwarded:
                        print(f"Saved forwarded text message ID {message.id} to {text_file_path}")
                    else:
                        print(f"Saved text message ID {message.id} to {text_file_path}")
                    downloaded_files += 1
                except Exception as e:
                    print(f"Error saving text from message ID {message.id}: {e}")
            
            # 处理带媒体的消息
            elif message.media:
                try:
                    # 确定目标目录
                    media_type_dir = os.path.join(download_dir, message_type)
                    
                    # 生成一个唯一的文件名前缀，为转发消息添加标记
                    if is_forwarded:
                        file_prefix = f"{message.id}_fwd_"
                    else:
                        file_prefix = f"{message.id}_"
                    
                    # 检查目标目录中是否已经存在以该前缀开头的完整文件（不包含"temp"的文件）
                    # 修复：确保文件名中不包含"temp"，而不仅仅是不以"temp"结尾
                    existing_files = [f for f in os.listdir(media_type_dir) 
                                     if f.startswith(file_prefix) and "temp" not in f]
                    
                    # 清理可能存在的临时文件（未完成的下载）
                    # 修复：查找所有包含"temp"的文件
                    temp_files = [f for f in os.listdir(media_type_dir) 
                                 if f.startswith(file_prefix) and "temp" in f]
                    for temp_file in temp_files:
                        try:
                            os.remove(os.path.join(media_type_dir, temp_file))
                            print(f"Removed incomplete download: {temp_file}")
                        except Exception as e:
                            print(f"Error removing temporary file {temp_file}: {e}")
                    
                    if existing_files:
                        # 文件已存在，跳过下载
                        existing_file = existing_files[0]  # 取第一个匹配的文件
                        existing_path = os.path.join(media_type_dir, existing_file)
                        print(f"Complete file already exists: {existing_path} - Skipping download")
                        downloaded_files += 1
                    else:
                        # 文件不存在或只有临时文件，进行下载
                        # 直接下载到目标目录，使用临时文件名
                        temp_target_path = os.path.join(media_type_dir, f"{file_prefix}temp")
                        downloaded_path = await client.download_media(message, temp_target_path)
                        
                        if downloaded_path:
                            # 计算文件哈希值
                            # file_hash = calculate_file_hash(downloaded_path)
                            file_hash = "nohash"
                            
                            # 获取文件扩展名
                            _, file_extension = os.path.splitext(downloaded_path)
                            if not file_extension:
                                file_extension = '.bin'
                            
                            # 创建最终文件名，使用哈希值
                            final_path = os.path.join(media_type_dir, f"{file_prefix}{file_hash[:16]}{file_extension}")
                            
                            # 重命名文件到最终名称
                            os.rename(downloaded_path, final_path)
                            
                            if is_forwarded:
                                print(f"Downloaded forwarded media to {final_path}")
                            else:
                                print(f"Downloaded to {final_path}")
                            downloaded_files += 1
                except Exception as e:
                    print(f"Error downloading message ID {message.id}: {e}")
                    traceback.print_exc()
                    # 清理可能存在的临时文件
                    temp_target_path = os.path.join(media_type_dir, f"{file_prefix}temp")
                    if os.path.exists(temp_target_path):
                        try:
                            os.remove(temp_target_path)
                        except:
                            pass
            else:
                print(f"Skipping message ID {message.id} - no media or text content")
                skipped_files += 1
            
            # 消息间休眠以避免速率限制
            await asyncio.sleep(sleep_ms / 1000)
        
        print(f"\nDownload summary:")
        print(f"Total messages processed: {total_files}")
        print(f"Files downloaded: {downloaded_files}")
        print(f"Files skipped: {skipped_files}")
    
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()

def print_help():
    """Print help message"""
    print("Usage:")
    print("  python main.py login - Login using QR code")
    print("  python main.py list - List all dialogs")
    print("  python main.py download <chat_id> [limit] [--sleep <ms>] [--type <types>] - Download media from a chat")
    print("  python main.py download --file <task_file> --out <output_dir> [--sleep <ms>] [--limit <count>] [--type <types>] - Download based on task file")
    print("  python main.py help - Show this help message")
    print("\nOptions:")
    print("  --sleep <ms>  - Sleep time in milliseconds between message downloads (default: 500)")
    print("  --limit <count> - Number of messages to retrieve for download commands (default: 500)")
    print("  --type <types> - Comma-separated list of content types to download (default: all)")
    print("\nValid types:")
    print("  Media types: video (includes image), audio, voice, document")
    print("  Other types: text, contact, location, poll, game, invoice, dice")
    print("  Special: webpage, all (for all types)")
    print("\nNote: Some types like contact, location, poll, etc. are not downloadable but will be logged.")

async def main():
    """Main function"""
    global client, consecutive_errors
    try:
        if len(sys.argv) < 2:
            print_help()
            return
        
        command = sys.argv[1].lower()
        
        # 每次启动检查连接状态
        if not client.is_connected():
            await client.connect()
        
        # 检查是否需要登录
        if not await client.is_user_authorized():
            logger.info("Not authorized. Launching QR login...")
            await qr_login()
        
        if command == "login":
            await qr_login()
        elif command == "list":
            # Call list_dialogs with default batch size of 100
            await list_dialogs()
        elif command == "download":
            # Parse sleep parameter if provided
            sleep_ms = 500  # Default sleep time in milliseconds
            limit = 500  # Default number of messages to retrieve
            allowed_types = ["all"]  # 默认下载所有类型
            
            # Check for parameters anywhere in the arguments
            i = 2
            while i < len(sys.argv) - 1:
                if sys.argv[i] == "--sleep":
                    try:
                        sleep_ms = int(sys.argv[i + 1])
                        # Remove these arguments to simplify further parsing
                        sys.argv.pop(i)
                        sys.argv.pop(i)
                        continue  # Don't increment i as we've removed elements
                    except (ValueError, IndexError):
                        logger.warning("Invalid sleep value. Using default 500ms.")
                        i += 2
                elif sys.argv[i] == "--limit":
                    try:
                        limit = int(sys.argv[i + 1])
                        # Remove these arguments to simplify further parsing
                        sys.argv.pop(i)
                        sys.argv.pop(i)
                        continue  # Don't increment i as we've removed elements
                    except (ValueError, IndexError):
                        logger.warning("Invalid limit value. Using default 500 messages.")
                        i += 2
                elif sys.argv[i] == "--type":
                    try:
                        type_value = sys.argv[i + 1]
                        if type_value and type_value.lower() != "all":
                            allowed_types = [t.strip().lower() for t in type_value.split(',') if t.strip()]
                            # image 已合并到 video 目录，将 image 类型映射为 video
                            allowed_types = ['video' if t == 'image' else t for t in allowed_types]
                            allowed_types = list(dict.fromkeys(allowed_types))  # 去重并保持顺序
                        # Remove these arguments to simplify further parsing
                        sys.argv.pop(i)
                        sys.argv.pop(i)
                        continue  # Don't increment i as we've removed elements
                    except (IndexError):
                        logger.warning("Invalid type value. Using default (all types).")
                        i += 2
                else:
                    i += 1
            
            # Check if using task file
            if len(sys.argv) >= 6 and sys.argv[2] == "--file" and sys.argv[4] == "--out":
                task_file = sys.argv[3]
                output_dir = sys.argv[5]
                await download_from_task(task_file, output_dir, sleep_ms, limit)
            # Traditional download
            elif len(sys.argv) >= 3:
                chat_id = sys.argv[2]
                limit = int(sys.argv[3]) if len(sys.argv) >= 4 else 10
                await download_media(chat_id, limit, sleep_ms, allowed_types)
            else:
                logger.warning("Invalid download command")
                print_help()
        elif command == "help":
            print_help()
        else:
            logger.warning("Unknown command")
            print_help()
    except SecurityError as e:
        logger.error(f"Security error in main function: {e}")
        # 尝试重置会话
        global consecutive_errors
        consecutive_errors += 1
        
        # 如果连续错误超过阈值，重置会话
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            logger.warning("Too many consecutive errors. Resetting session...")
            client = reset_session()
            await client.connect()
            # 检查是否需要重新登录
            if not await client.is_user_authorized():
                logger.warning("Session reset, but user is not authorized. Need to login again.")
                await qr_login()
                # 重新运行命令
                logger.info("Session reset. Please run your command again.")
            else:
                logger.info("Session reset successfully. Please run your command again.")
        else:
            logger.error("Security error occurred. Try running the command again.")
        
    except Exception as e:
        logger.error(f"Error in main function: {e}")
        traceback.print_exc()
        
        # 如果是致命错误，尝试自动重置会话并推荐用户重新运行
        logger.warning("An error occurred. If you continue to experience issues, try:")
        logger.warning("1. Delete the 'telegram_session.session' file")
        logger.warning("2. Run 'python main.py login' to authorize again")
        logger.warning("3. Try your command again")

if __name__ == "__main__":
    try:
        with client:
            client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("\nProgram terminated by user.")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        traceback.print_exc()
        
        # 如果是致命错误，尝试自动重置会话并推荐用户重新运行
        logger.warning("An error occurred. If you continue to experience issues, try:")
        logger.warning("1. Delete the 'telegram_session.session' file")
        logger.warning("2. Run 'python main.py login' to authorize again")
        logger.warning("3. Try your command again")