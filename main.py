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
import re

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

# Client setup
client = TelegramClient('telegram_session', API_ID, API_HASH)

# 添加一个全局字典来保存每个schedule文件对应的数据
# 使用字典以支持多个任务并行处理时各自有独立的缓存
_schedule_cache = {}

def get_message_type(message):
    """Determine the type of message based on its media content"""
    if message.text and not message.media:
        return "text"
    
    if message.media:
        if isinstance(message.media, MessageMediaPhoto):
            return "image"
        elif isinstance(message.media, MessageMediaWebPage):
            return "webpage"
        elif isinstance(message.media, MessageMediaDocument):
            for attribute in message.media.document.attributes:
                if isinstance(attribute, DocumentAttributeVideo):
                    return "video"
                elif isinstance(attribute, DocumentAttributeAudio):
                    if attribute.voice:
                        return "voice"
                    elif attribute.voice_note:
                        return "voice_note"
                    else:
                        return "audio"
            # Check filename for document type
            for attribute in message.media.document.attributes:
                if isinstance(attribute, DocumentAttributeFilename):
                    filename = attribute.file_name.lower()
                    if any(filename.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                        return "image"
                    elif any(filename.endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.mkv']):
                        return "video"
                    elif any(filename.endswith(ext) for ext in ['.mp3', '.wav', '.ogg', '.flac']):
                        return "audio"
            return "document"
    
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

async def list_dialogs(limit=100):
    """List all dialogs (chats and channels)
    
    This function automatically fetches all dialogs by making multiple requests
    with appropriate offset values. The limit parameter controls how many dialogs
    are fetched in each request.
    """
    total_count = 0
    
    # For pagination
    offset_date = None
    offset_id = 0
    offset_peer = InputPeerEmpty()
    
    print(f"Fetching all dialogs (in batches of {limit})...")
    print("\nDialogs:")
    
    while True:
        try:
            result = await client(GetDialogsRequest(
                offset_date=offset_date,
                offset_id=offset_id,
                offset_peer=offset_peer,
                limit=limit,
                hash=0
            ))
            
            # If no dialogs returned, we've reached the end
            if not result.dialogs:
                print("No more dialogs found.")
                break
                
            # Process and display each dialog immediately
            for dialog in result.dialogs:
                try:
                    entity = await client.get_entity(dialog.peer)
                    name = getattr(entity, 'title', getattr(entity, 'first_name', ''))
                    print(f"ID: {entity.id} - Name: {name}")
                    total_count += 1
                except Exception as e:
                    print(f"Error getting entity: {e}")
            
            print(f"Fetched {len(result.dialogs)} more dialogs. Total so far: {total_count}")
            
            # If we got fewer dialogs than requested, we've reached the end
            if len(result.dialogs) < limit:
                print("Reached the end of the dialog list.")
                break
                
            # Update offset for next iteration - use the last dialog as reference
            last_dialog = result.dialogs[-1]
            last_message = None
            
            # Find the message corresponding to the last dialog
            for message in result.messages:
                if message.peer_id == last_dialog.peer:
                    last_message = message
                    break
            
            if last_message:
                offset_date = last_message.date
                offset_id = last_message.id
            else:
                # Fallback if we can't find the corresponding message
                offset_id = last_dialog.top_message
                
            offset_peer = last_dialog.peer
            
            # Add a small delay to avoid hitting rate limits
            await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"Error fetching dialogs: {e}")
            traceback.print_exc()
            break
    
    print(f"\nFound {total_count} dialogs in total.")

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
    """Download all media files from a message, including albums"""
    downloaded_files = []
    media_count = 0
    
    # 检查消息是否有媒体
    if not message.media:
        return downloaded_files, media_count
    
    # 如果是网页预览，跳过
    if isinstance(message.media, MessageMediaWebPage):
        return downloaded_files, media_count
    
    # 获取消息类型
    message_type = get_message_type(message).lower()
    
    # 确定目标目录
    target_dir = os.path.join(channel_dir, message_type)
    os.makedirs(target_dir, exist_ok=True)
    
    # 处理主媒体文件
    if message.media:
        # 生成一个唯一的文件名前缀
        file_prefix = f"{message.id}_"
        
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
                print(f"Removed incomplete download: {temp_file}")
            except Exception as e:
                print(f"Error removing temporary file {temp_file}: {e}")
        
        if existing_files:
            # 文件已存在，跳过下载
            existing_file = existing_files[0]  # 取第一个匹配的文件
            existing_path = os.path.join(target_dir, existing_file)
            print(f"Complete file already exists: {existing_path} - Skipping download")
            downloaded_files.append(existing_path)
            media_count += 1
        else:
            # 文件不存在或只有临时文件，进行下载
            media_count += 1
            
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
                    
                    print(f"Downloaded media to {final_path}")
                    downloaded_files.append(final_path)
            except Exception as e:
                print(f"Error downloading main media from message ID {message.id}: {e}")
                traceback.print_exc()
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
                # 生成一个唯一的文件名前缀
                grouped_prefix = f"{message.id}_grouped_"
                
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
                        print(f"Removed incomplete grouped download: {grouped_temp_file}")
                    except Exception as e:
                        print(f"Error removing temporary grouped file {grouped_temp_file}: {e}")
                
                if existing_grouped_files:
                    # 文件已存在，跳过下载
                    existing_grouped_file = existing_grouped_files[0]  # 取第一个匹配的文件
                    existing_grouped_path = os.path.join(target_dir, existing_grouped_file)
                    print(f"Complete grouped file already exists: {existing_grouped_path} - Skipping download")
                    downloaded_files.append(existing_grouped_path)
                    media_count += 1
                else:
                    # 文件不存在或只有临时文件，进行下载
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
                            
                            print(f"Downloaded grouped media to {grouped_final_path}")
                            downloaded_files.append(grouped_final_path)
                            media_count += 1
                    except Exception as e:
                        print(f"Error downloading grouped media from message ID {message.id}: {e}")
                        traceback.print_exc()
                        # 清理可能存在的临时文件
                        grouped_temp_target_path = os.path.join(target_dir, f"{grouped_prefix}temp")
                        if os.path.exists(grouped_temp_target_path):
                            try:
                                os.remove(grouped_temp_target_path)
                            except:
                                pass
                 
        except Exception as e:
            print(f"Error checking for grouped media in message ID {message.id}: {e}")
            traceback.print_exc()
    
    # 不再在这里处理相册的其他消息，因为这个操作已移至process_single_channel函数中
    # 这样避免了重复处理相册，提高了效率
    
    return downloaded_files, media_count

# 添加一个函数来检查消息是否是相册，并获取相册信息
async def get_message_info(message):
    """获取消息的详细信息，包括类型和相册信息"""
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
        except Exception as e:
            print(f"Error getting album info for message ID {message.id}: {e}")
    
    return {
        "type": message_type,
        "is_album": is_album,
        "album_id": message.grouped_id if is_album else None,
        "album_size": album_size
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
                
                # 处理这个任务
                await process_single_channel(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    output_dir=output_dir,
                    sleep_ms=task_sleep_ms,
                    msg_limit=task_limit,
                    allowed_types=allowed_types
                )
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
                print(f"Only downloading media types: {', '.join(allowed_types)}")
            else:
                # 如果没有指定类型，则不下载任何内容
                print("No media types specified in task. No media will be downloaded.")
                allowed_types = []  # 空列表表示不下载任何类型
            
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
        print(f"Error in download_from_task: {e}")
        traceback.print_exc()

# 添加一个新函数处理单个频道的下载
async def process_single_channel(channel_id, channel_name, output_dir, sleep_ms=500, msg_limit=500, allowed_types=None):
    """处理单个频道的下载任务"""
    try:
        # 获取输出目录
        if not output_dir:
            output_dir = 'downloads'
        
        # 处理频道名称作为子目录
        try:
            entity = await client.get_entity(channel_id)
            print(f"Found entity: {entity.title if hasattr(entity, 'title') else channel_id}")
            
            # 如果未提供名称，则使用实体标题
            if not channel_name and hasattr(entity, 'title'):
                channel_name = entity.title
            
            # 确保频道名称是合法的文件名
            channel_name = re.sub(r'[\\/:*?"<>|]', '_', channel_name)
            channel_dir = os.path.join(output_dir, channel_name)
            
            # 创建输出目录
            os.makedirs(channel_dir, exist_ok=True)
            
            # 创建各媒体类型的子目录
            for media_type in ['text', 'image', 'video', 'voice', 'audio', 'document']:
                media_dir = os.path.join(channel_dir, media_type)
                os.makedirs(media_dir, exist_ok=True)
            
            # 获取频道的所有消息
            print(f"Downloading media from {channel_name} ({channel_id})")
            
            # 计算批处理大小
            batch_size = 100  # 默认批处理大小
            
            # 获取消息计数和计算总批次
            first_message_id = 0
            latest_message_id = 0
            
            try:
                # 获取频道的第一条（最早）消息
                first_messages = await client.get_messages(entity, limit=1, reverse=True)
                if first_messages and len(first_messages) > 0:
                    first_message_id = first_messages[0].id
                    print(f"First message ID: {first_message_id}")
                
                # 获取频道的最新消息
                latest_messages = await client.get_messages(entity, limit=1)
                if latest_messages and len(latest_messages) > 0:
                    latest_message_id = latest_messages[0].id
                    print(f"Latest message ID: {latest_message_id}")
                
                # 计算总消息数的估计值
                # 注意：实际消息数可能少于此估计值，因为消息ID可能不连续
                total_messages = latest_message_id - first_message_id + 1
                print(f"Estimated total messages (based on ID range): {total_messages}")
                
                # 使用更简单的方法尝试获取消息数量
                try:
                    # 使用client.get_messages的limit=0参数获取总消息数
                    message_count = await client.get_messages(entity, limit=0)
                    if hasattr(message_count, 'total'):
                        print(f"Actual message count: {message_count.total}")
                        # 更新total_messages为实际值
                        total_messages = message_count.total
                except Exception as e:
                    print(f"Note: Could not get exact message count: {e}")
                    # 这里不是关键错误，继续使用估计值
                    pass
            except Exception as e:
                print(f"Error getting message range: {e}")
                total_messages = None
                traceback.print_exc()
            
            # 确定起始消息ID（从最早的消息或从上次的进度开始）
            schedule_file = os.path.join(channel_dir, "schedule.json")
            
            # 获取上次处理的消息ID（优先使用内存缓存）
            last_message_id = get_last_message_id(schedule_file)
            
            # 如果有上次的进度，从上次位置继续；否则从第一条消息开始
            if last_message_id and last_message_id > first_message_id:
                current_id = last_message_id + 1  # 从下一条消息开始
                print(f"Resuming from message ID: {current_id}")
            else:
                current_id = first_message_id  # 从第一条消息开始
                print(f"Starting from the first message (ID: {current_id})")
            
            # 如果无法确定起始ID，则退出
            if current_id == 0:
                print("Could not determine a valid starting message ID")
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
                print(f"Estimated messages to process: {messages_to_process}")
                
                # 如果设置了消息限制，取较小值
                if msg_limit > 0:
                    messages_to_process = min(messages_to_process, msg_limit)
                    print(f"Limited to {messages_to_process} messages due to limit setting")
                
                # 计算总批次数
                total_batches = (messages_to_process + batch_size - 1) // batch_size if messages_to_process > 0 else 0
                print(f"Estimated total batches: {total_batches}")
            
            # 追踪处理的消息计数
            processed_count = 0
            max_id_to_process = min(latest_message_id, current_id + msg_limit - 1) if msg_limit > 0 else latest_message_id
            
            while has_more_messages and current_id <= max_id_to_process:
                # 显示当前批次和总批次
                if total_batches:
                    progress_percent = min(100.0, (batch_number / total_batches * 100))
                    print(f"Processing batch {batch_number}/{total_batches} ({progress_percent:.1f}%)")
                else:
                    print(f"Processing batch #{batch_number}")
                
                print(f"Processing messages from ID: {current_id} to approximately {min(current_id + batch_size - 1, max_id_to_process)}")
                
                try:
                    # 获取一批消息 - 使用min_id和max_id参数来指定ID范围
                    # 这样可以确保按照ID从小到大的顺序获取消息
                    min_id = current_id
                    max_id = min(current_id + batch_size - 1, max_id_to_process)
                    
                    messages = await client.get_messages(
                        entity,
                        limit=batch_size,
                        min_id=min_id - 1,  # min_id是包含的，所以减1来确保包含current_id
                        max_id=max_id       # max_id是包含的
                    )
                    
                    if not messages or len(messages) == 0:
                        print(f"No messages found in range {min_id} to {max_id}")
                        # 继续下一个批次
                        current_id = max_id + 1
                        batch_number += 1
                        
                        # 如果已经达到或超过最大ID，则退出循环
                        if current_id > max_id_to_process:
                            print("Reached the end of message range")
                            has_more_messages = False
                        
                        continue
                    
                    # 确保消息按ID排序（从小到大，即时间从早到晚）
                    messages = sorted(messages, key=lambda m: m.id)
                    
                    print(f"Retrieved {len(messages)} messages in this batch (ID range: {messages[0].id} to {messages[-1].id})")
                    
                    # 处理这批消息
                    for message in messages:
                        print(f"Processing message ID: {message.id}")
                        # 如果消息已经处理过，跳过
                        if message.id in processed_messages:
                            print(f"Skipping already processed message ID {message.id}")
                            continue
                        
                        # 如果消息已经作为相册的一部分被处理过，也跳过
                        if message.id in processed_album_message_ids:
                            print(f"Skipping message ID {message.id} - already processed as part of an album")
                            processed_messages.add(message.id)
                            processed_count += 1
                            # 更新进度（同时更新内存和文件）
                            update_last_message_id(schedule_file, message.id)
                            continue
                        
                        # 将消息标记为已处理
                        processed_messages.add(message.id)
                        processed_count += 1
                        
                        # 获取消息详细信息
                        message_info = await get_message_info(message)
                        message_type = message_info["type"]

                        print(f"Message type: {message_type}")
                        
                        # 检查当前消息类型是否在允许下载的类型列表中
                        # 如果不在allowed_types中且不是"all"，则跳过此消息
                        if "all" not in allowed_types and message_type not in allowed_types:
                            print(f"Skipping message ID {message.id} of type {message_type} (not in allowed types: {', '.join(allowed_types)})")
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
                            print(f"Processing album {album_id} for the first time")
                            
                            # 获取相册中的所有消息
                            try:
                                # 获取消息所在的会话
                                chat_entity = message.chat if hasattr(message, 'chat') else message.peer_id
                                
                                # 获取消息ID附近的消息
                                album_messages = await client.get_messages(
                                    entity=chat_entity,
                                    offset_id=message.id,
                                    limit=100  # 增加限制以获取更多可能的相册消息
                                )
                                
                                # 筛选出同一相册的消息
                                same_album_messages = [msg for msg in album_messages if 
                                                    hasattr(msg, 'grouped_id') and 
                                                    msg.grouped_id == album_id]
                                
                                print(f"Found {len(same_album_messages)} messages in album {album_id}")
                                
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
                                print(f"Error retrieving album messages for album {album_id}: {e}")
                                traceback.print_exc()
                        elif is_album and album_id:
                            print(f"Found another message (ID: {message.id}) from album {album_id} - already processed")
                        
                        # 处理媒体文件
                        if message_type not in ["text", "unknown"]:
                            # 确保媒体类型目录存在
                            media_type_dir = os.path.join(channel_dir, message_type)
                            os.makedirs(media_type_dir, exist_ok=True)
                            
                            # 下载媒体文件
                            try:
                                temp_dir_prefix = f"{channel_name}_{channel_id}"
                                downloaded_files, media_count = await download_all_media_from_message(
                                    message, channel_dir, temp_dir_prefix
                                )
                                
                                if media_count > 0:
                                    total_downloaded += media_count
                                    print(f"Downloaded {media_count} media files from message ID {message.id}")
                                else:
                                    print(f"No media files found in message ID {message.id}")
                            except Exception as e:
                                print(f"Error downloading media from message ID {message.id}: {e}")
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
                                print(f"Saved text message ID {message.id} to {text_file_path}")
                                total_downloaded += 1  # 计入总下载数
                            except Exception as e:
                                print(f"Error saving text from message ID {message.id}: {e}")
                                traceback.print_exc()
                        else:
                            print(f"Skipping message ID {message.id} of type {message_type}")
                        
                        # 更新进度（同时更新内存和文件）
                        update_last_message_id(schedule_file, message.id)
                        
                        # 如果有该批次的其他相册消息，立即处理它们以避免后续重复获取
                        for album_msg in album_messages_for_batch:
                            if album_msg.id in processed_messages:
                                continue  # 跳过已处理过的消息
                            
                            # 标记此相册消息为已处理
                            processed_messages.add(album_msg.id)
                            processed_count += 1
                            
                            # 获取消息类型
                            album_msg_info = await get_message_info(album_msg)
                            album_msg_type = album_msg_info["type"]
                            
                            # 处理相册消息的媒体
                            if album_msg_type not in ["text", "unknown"]:
                                try:
                                    # 确保目标目录存在
                                    album_media_dir = os.path.join(channel_dir, album_msg_type)
                                    os.makedirs(album_media_dir, exist_ok=True)
                                    
                                    # 下载相册消息的媒体
                                    album_downloaded_files, album_media_count = await download_all_media_from_message(
                                        album_msg, channel_dir, temp_dir_prefix
                                    )
                                    
                                    if album_media_count > 0:
                                        total_downloaded += album_media_count
                                        print(f"Downloaded {album_media_count} media files from album message ID {album_msg.id}")
                                    else:
                                        print(f"No media files found in album message ID {album_msg.id}")
                                except Exception as e:
                                    print(f"Error downloading media from album message ID {album_msg.id}: {e}")
                                    traceback.print_exc()
                            else:
                                print(f"Skipping album message ID {album_msg.id} of type {album_msg_type}")
                            
                            # 更新进度
                            update_last_message_id(schedule_file, album_msg.id)
                        
                        # 进度显示
                        if messages_to_process:
                            progress = min(100.0, (processed_count / messages_to_process * 100))
                            print(f"Overall progress: {processed_count}/{messages_to_process} messages ({progress:.1f}%)")
                        
                        # 暂停以避免请求过快
                        await asyncio.sleep(sleep_ms / 1000)
                        
                        # 如果达到消息限制，则退出
                        if msg_limit > 0 and processed_count >= msg_limit:
                            print(f"Reached message limit of {msg_limit}")
                            has_more_messages = False
                            break
                    
                    # 更新下一批次的起始ID
                    # 使用当前批次最后一条消息的ID + 1
                    last_id_in_batch = messages[-1].id
                    current_id = last_id_in_batch + 1
                    
                    # 更新批次号
                    batch_number += 1
                    
                    # 检查是否已经处理完所有消息
                    if current_id > max_id_to_process:
                        print("Reached the end of message range")
                        has_more_messages = False
                    
                except Exception as e:
                    print(f"Error processing batch: {e}")
                    traceback.print_exc()
                    # 等待一会儿然后继续
                    await asyncio.sleep(5)
                    # 尝试继续下一个批次
                    current_id += batch_size
            
            print(f"Total downloaded media files: {total_downloaded}")
            
        except Exception as e:
            print(f"Error processing channel {channel_id}: {e}")
            traceback.print_exc()
    
    except Exception as e:
        print(f"Error in process_single_channel: {e}")
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
        for media_type in ['text', 'image', 'video', 'voice', 'audio', 'document']:
            media_dir = os.path.join(download_dir, media_type)
            os.makedirs(media_dir, exist_ok=True)
        
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
            # 获取消息类型
            message_type = get_message_type(message)
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
                text_filename = f"{message.id}_{message_datetime}.txt"
                text_file_path = os.path.join(text_dir, text_filename)
                
                try:
                    with open(text_file_path, 'w', encoding='utf-8') as f:
                        f.write(message.text)
                    print(f"Saved text message ID {message.id} to {text_file_path}")
                    downloaded_files += 1
                except Exception as e:
                    print(f"Error saving text from message ID {message.id}: {e}")
            
            # 处理带媒体的消息
            elif message.media:
                try:
                    # 确定目标目录
                    media_type_dir = os.path.join(download_dir, message_type)
                    
                    # 生成一个唯一的文件名前缀
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
    print("\nValid types: text, image, video, audio, voice, document (or 'all' for all types)")

async def main():
    """Main function"""
    if len(sys.argv) < 2:
        print_help()
        return
    
    command = sys.argv[1].lower()
    
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
                    print("Invalid sleep value. Using default 500ms.")
                    i += 2
            elif sys.argv[i] == "--limit":
                try:
                    limit = int(sys.argv[i + 1])
                    # Remove these arguments to simplify further parsing
                    sys.argv.pop(i)
                    sys.argv.pop(i)
                    continue  # Don't increment i as we've removed elements
                except (ValueError, IndexError):
                    print("Invalid limit value. Using default 500 messages.")
                    i += 2
            elif sys.argv[i] == "--type":
                try:
                    type_value = sys.argv[i + 1]
                    if type_value and type_value.lower() != "all":
                        allowed_types = [t.strip().lower() for t in type_value.split(',') if t.strip()]
                    # Remove these arguments to simplify further parsing
                    sys.argv.pop(i)
                    sys.argv.pop(i)
                    continue  # Don't increment i as we've removed elements
                except (IndexError):
                    print("Invalid type value. Using default (all types).")
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
            print("Invalid download command")
            print_help()
    elif command == "help":
        print_help()
    else:
        print("Unknown command")
        print_help()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
