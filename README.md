# OtterBot 🦦
Automatically download all your meeting transcripts from Otter.ai with production-grade reliability and speed.

## Features
- **Ultra-Fast Mode**: Downloads transcripts in ~8-10 seconds.
- **Incremental Downloads**: `--quick` mode to only pull the latest transcripts.
- **Docker Support**: Run in an isolated container without local setup.
- **Robust State Management**: Resumes where it left off; never downloads the same file twice.

## Setup

### 1. Local Installation
```bash
pip install -r requirements.txt
playwright install chromium --with-deps
```

### 2. Environment Variables
Create a `.env` file or export:
```bash
export OTTER_EMAIL='your@email.com'
export OTTER_PASSWORD='your_password'
```

## Usage

### Run Everything (Initial Backup)
```bash
python otter_downloader.py
```

### Quick Sync (Daily Catch-up)
Checks only the Top 15 recent transcripts.
```bash
python otter_downloader.py --quick
```

### Docker
**Build**:
```bash
docker build -t otterbot .
```

**Run**:
```bash
docker run -v ${PWD}:/app --env-file .env otterbot --quick
```

## Configuration
Edit `config.py` for advanced settings like timeouts, headless mode, and export formats.

## License
MIT
