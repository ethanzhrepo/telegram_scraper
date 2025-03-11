# Telegram Resource Scraper

A Python tool to download resources from Telegram chats and channels using the Telethon library.

## Setup

### 1. Get Telegram API Credentials

Before using this tool, you need to get your API ID and API Hash from Telegram:

1. Visit https://my.telegram.org/
2. Log in with your phone number
3. Click on "API Development Tools"
4. Create a new application
5. Copy your API ID and API Hash

### 2. Update the Configuration File

Open `config.yml` and replace the placeholder values with your actual API credentials:

```yaml
api_id: 12345  # Replace with your actual API ID
api_hash: "your_api_hash_here"  # Replace with your actual API Hash
```

### 3. Install Dependencies

#### Using pip:
```bash
pip install -r requirements.txt
```

#### Using conda:
```bash
conda env create -f environment.yml
conda activate telegram
```

## Usage

### Login with QR Code
```bash
python main.py login
```
This will generate a QR code in the `/tmp` directory and open it with your system's default image viewer. Scan the QR code with your Telegram mobile app to log in.

### List All Dialogs (Chats and Channels)
```bash
python main.py list
```
This will display all your chats and channels with their IDs.

### Download Media from a Chat
```bash
python main.py download <chat_id> [limit] [--sleep <ms>]
```
- `<chat_id>`: The ID of the chat or channel (obtained from the list command)
- `[limit]`: (Optional) Maximum number of messages to process (default: 10)
- `--sleep <ms>`: (Optional) Sleep time in milliseconds between message downloads (default: 500)

### Download Media Using Task File
```bash
python main.py download --file <task_file> --out <output_dir> [--sleep <ms>] [--limit <count>]
```
- `<task_file>`: Path to a YAML task file that defines what to download
- `<output_dir>`: Directory where downloaded media will be saved
- `--sleep <ms>`: (Optional) Sleep time in milliseconds between message downloads (default: 500)
- `--limit <count>`: (Optional) Number of messages to retrieve (default: 500, 0 means all messages)

#### Task File Format
Create a YAML file with the following structure:
```yaml
tasks:
  - name: task-name
    id: channel_id
    type: "image"  # Type of media to download (image, video, document, etc.)
```

### Help
```bash
python main.py help
```
Displays available commands and their usage.

## Notes

- The first time you run the program, it will create a `telegram_session` file to store your session.
- Downloaded media will be saved in a `downloads` directory, organized by chat name and media type.
- This tool handles both individual media files and album media (grouped images/videos).
- Media files are named using a pattern that includes the message ID and a hash of the file content to avoid duplicates.
- The program can efficiently process large channels by downloading messages in batches and tracking progress.

## Command Options

| Option | Description |
|--------|-------------|
| `--sleep <ms>` | Sleep time in milliseconds between message downloads (default: 500) |
| `--limit <count>` | Number of messages to retrieve (default: 500, 0 means all messages) |

## Requirements

- Python 3.12
- Telethon 1.39.0
- PyYAML 6.0.1 