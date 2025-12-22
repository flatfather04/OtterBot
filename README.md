# Otter.ai Automated Transcript Downloader

Automatically download all your meeting transcripts from Otter.ai using browser automation.

## Setup

### 1. Install Python Dependencies

```bash
cd z:\OtterBot
pip install -r requirements.txt
playwright install chromium
```

### 2. First-Time Login

Run the script with `--login` to save your session:

```bash
python otter_downloader.py --login
```

A browser will open. Log in to your Otter.ai account, then press Enter in the terminal.

## Usage

### Download All Transcripts

```bash
python otter_downloader.py
```

The script will:
1. Load your saved session
2. Scroll to load all conversations
3. Download each transcript to `./downloads/`
4. Save progress (resume-friendly if interrupted)

### Options

| Flag | Description |
|------|-------------|
| `--login` | Manual login mode (saves session for reuse) |
| `--reset` | Clear download progress and start fresh |

## Configuration

Edit `config.py` to customize:

- `EXPORT_FORMATS` - File formats to download (`txt`, `docx`, `pdf`, `srt`)
- `HEADLESS` - Run browser without visible window
- `DELAY_BETWEEN_DOWNLOADS` - Rate limiting between downloads

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Session expired" | Run `--login` again |
| Script hangs on scroll | Increase `SCROLL_WAIT_TIME` in config.py |
| Downloads fail | Try with `HEADLESS = False` to debug |

## Output

Transcripts are saved to `./downloads/` as:
```
Meeting_Title_abc123.txt
```
