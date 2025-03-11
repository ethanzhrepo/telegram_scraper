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

### First-time Login Notes

When logging in for the first time:

1. You will be prompted to enter your phone number
2. A verification code will be sent to your Telegram account (check your Telegram messages)
3. Enter the verification code when prompted
4. You may receive a security alert in your Telegram app - approve the login request


### Session Management

After successful login, a `telegram_session.session` file will be created in your working directory. This session file allows you to:

- Skip the login process in future runs
- Maintain your authenticated session across multiple uses
- Continue using the tool without re-authenticating

If you want to stop using this account:

1. Go to Telegram's official client
2. Navigate to Settings > Privacy and Security > Active Sessions
3. Find and terminate the session created by this tool
4. Delete the `telegram_session.session` file from your working directory


### List All Dialogs (Chats and Channels)
```bash
python main.py list [--limit <count>]
```
This will display all your chats and channels with their IDs.
- `--limit <count>`: (Optional) Maximum number of dialogs to retrieve (default: 100)

### Download Media from a Chat
```bash
python main.py download <chat_id> [limit] [--sleep <ms>] [--type <types>]
```
- `<chat_id>`: The ID of the chat or channel (obtained from the list command)
- `[limit]`: (Optional) Maximum number of messages to process (default: 10)
- `--sleep <ms>`: (Optional) Sleep time in milliseconds between message downloads (default: 500)
- `--type <types>`: (Optional) Comma-separated list of content types to download (default: all)

### Download Media Using Task File
```bash
python main.py download --file <task_file> --out <output_dir> [--sleep <ms>] [--limit <count>] [--type <types>]
```
- `<task_file>`: Path to a YAML task file that defines what to download
- `<output_dir>`: Directory where downloaded media will be saved
- `--sleep <ms>`: (Optional) Sleep time in milliseconds between message downloads (default: 500)
- `--limit <count>`: (Optional) Number of messages to retrieve (default: 500, 0 means all messages)
- `--type <types>`: (Optional) Comma-separated list of content types to download (overrides task file settings)

#### Task File Format
Create a YAML file with the following structure:
```yaml
tasks:
  - name: task-name
    id: channel_id
    type: "image,video"  # Type of media to download (comma-separated list)
```

**Important notes about the task file:**
- The `type` field is required. If not specified, no media will be downloaded.
- Valid types include: `text`, `image`, `video`, `voice`, `audio`, `document`
- Multiple types can be specified by separating with commas: `"image,video,document"`
- Only media types specified in the `type` field will be downloaded
- Each task can have its own set of types to download

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
- All content is organized into appropriate subdirectories by type:
  - `text`: Text messages are saved as .txt files
  - `image`: Photos and image files
  - `video`: Video files
  - `voice`: Voice messages
  - `audio`: Audio files
  - `document`: Documents and other file types

## Command Options

| Option | Description |
|--------|-------------|
| `--sleep <ms>` | Sleep time in milliseconds between message downloads (default: 500) |
| `--limit <count>` | Number of messages/dialogs to retrieve (default varies by command) |
| `--type <types>` | Comma-separated list of content types to download (default: all) |

## Requirements

- Python 3.12
- Telethon 1.39.0
- PyYAML 6.0.1 

## Generated by AI Assistant

This tool was created with assistance from an AI programming assistant to help streamline the process of downloading media from Telegram channels and chats.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## TODO 
