"""
Configuration for Otter.ai Transcript Downloader - Production Settings
"""
import os
from pathlib import Path

# Otter.ai Credentials (for automated login)
OTTER_EMAIL = "xdpriyesh@gmail.com"
OTTER_PASSWORD = "Priyesh@123"

# Base directory for downloads
DOWNLOAD_DIR = Path(__file__).parent / "downloads"

# Session storage file (for persisting login)
SESSION_FILE = Path(__file__).parent / ".otter_session.json"

# Progress tracking file (for resuming interrupted downloads)
PROGRESS_FILE = Path(__file__).parent / ".download_progress.json"

# Otter.ai URLs
OTTER_BASE_URL = "https://otter.ai"
OTTER_LOGIN_URL = "https://otter.ai/signin"
OTTER_CONVERSATIONS_URL = "https://otter.ai/my-notes"

# Export formats to download (options: txt, docx, pdf, srt)
EXPORT_FORMATS = ["txt"]

# Timeouts (in milliseconds) - PRODUCTION VALUES (increased for reliability)
PAGE_LOAD_TIMEOUT = 60000  # 60 seconds for page loads
SCROLL_WAIT_TIME = 2000    # 2 seconds between scrolls
DOWNLOAD_WAIT_TIME = 30000 # 30 seconds wait for download

# Rate limiting - ULTRA FAST MODE
DELAY_BETWEEN_DOWNLOADS = 0.5  # Minimal delay

# Browser settings - MAXIMUM SPEED
HEADLESS = True   # Headless for speed
SLOW_MO = 0       # No slowdown


