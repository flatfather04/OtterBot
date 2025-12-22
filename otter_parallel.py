"""
Otter.ai Parallel Transcript Downloader - FAST VERSION

This script downloads transcripts in PARALLEL using multiple browser contexts
for 3-4x faster downloads compared to sequential processing.

Usage:
    python otter_parallel.py [--workers N]
"""

import json
import time
import argparse
import re
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeout

from config import (
    OTTER_EMAIL, OTTER_PASSWORD,
    DOWNLOAD_DIR, SESSION_FILE,
    OTTER_BASE_URL, OTTER_LOGIN_URL, OTTER_CONVERSATIONS_URL,
    PAGE_LOAD_TIMEOUT, SCROLL_WAIT_TIME,
    HEADLESS, SLOW_MO
)


# ============================================================================
# LOGGING SETUP
# ============================================================================
LOG_FILE = Path(__file__).parent / "otter_parallel.log"
STATE_FILE = Path(__file__).parent / ".otter_state.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | [%(threadName)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Thread-safe state lock
state_lock = Lock()


# ============================================================================
# STATE MANAGEMENT (Thread-Safe)
# ============================================================================
class ParallelState:
    """Thread-safe state management for parallel downloads."""
    
    def __init__(self):
        self.state_file = STATE_FILE
        self.state = self._load_state()
        self.lock = Lock()
        self.download_count = 0
        self.total_to_download = 0
    
    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {
            "meetings": {},
            "successful_downloads": [],
            "failed_downloads": [],
        }
    
    def save(self):
        with self.lock:
            self.state["last_run"] = datetime.now().isoformat()
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
    
    def is_downloaded(self, meeting_id: str) -> bool:
        with self.lock:
            return meeting_id in self.state["successful_downloads"]
    
    def mark_success(self, meeting_id: str, path: Path, file_size: int):
        with self.lock:
            if meeting_id not in self.state["successful_downloads"]:
                self.state["successful_downloads"].append(meeting_id)
            if meeting_id in self.state.get("meetings", {}):
                self.state["meetings"][meeting_id]["status"] = "success"
                self.state["meetings"][meeting_id]["download_path"] = str(path)
                self.state["meetings"][meeting_id]["file_size"] = file_size
                self.state["meetings"][meeting_id]["method_used"] = "text_extraction"
            self.download_count += 1
            self.save()
    
    def mark_failure(self, meeting_id: str, error: str):
        with self.lock:
            if meeting_id not in self.state["failed_downloads"]:
                self.state["failed_downloads"].append(meeting_id)
            if meeting_id in self.state.get("meetings", {}):
                self.state["meetings"][meeting_id]["status"] = "failed"
                self.state["meetings"][meeting_id]["error"] = error
            self.save()
    
    def get_pending_meetings(self) -> List[Dict]:
        """Get all meetings that haven't been downloaded yet."""
        pending = []
        with self.lock:
            for meeting_id, meeting in self.state.get("meetings", {}).items():
                if meeting_id not in self.state["successful_downloads"]:
                    pending.append(meeting)
        return pending
    
    def get_progress(self) -> tuple:
        with self.lock:
            downloaded = len(self.state["successful_downloads"])
            total = len(self.state.get("meetings", {}))
            return downloaded, total


def sanitize_filename(title: str, max_length: int = 100) -> str:
    """Create a safe filename from the title."""
    safe = re.sub(r'[<>:"/\\|?*\n\r]', '_', title)
    safe = re.sub(r'_+', '_', safe)
    return safe[:max_length].strip('_')


def download_single_transcript(meeting: Dict, state: ParallelState, worker_id: int) -> bool:
    """
    Download a single transcript using its own browser context.
    This function is designed to run in a thread.
    """
    meeting_id = meeting['id']
    title = meeting.get('title', 'Unknown')[:50]
    url = meeting['url']
    
    if state.is_downloaded(meeting_id):
        return True
    
    logger.info(f"[Worker-{worker_id}] Downloading: {title}...")
    
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,  # Always headless for parallel
                args=['--disable-gpu', '--no-sandbox']
            )
            
            # Load session if exists
            context_options = {}
            if SESSION_FILE.exists():
                try:
                    with open(SESSION_FILE, 'r') as f:
                        session_data = json.load(f)
                    context_options['storage_state'] = session_data
                except:
                    pass
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            
            try:
                # Navigate to transcript page - minimal wait
                page.goto(url, timeout=30000, wait_until='commit')
                time.sleep(2)  # Minimal wait for content
                
                # Extract transcript text
                transcript_text = extract_transcript_text(page, meeting)
                
                if transcript_text and len(transcript_text) > 100:
                    # Save to file
                    filename = f"{sanitize_filename(title)}_{meeting_id[:15]}.txt"
                    save_path = DOWNLOAD_DIR / filename
                    
                    with open(save_path, 'w', encoding='utf-8') as f:
                        f.write(transcript_text)
                    
                    file_size = save_path.stat().st_size
                    state.mark_success(meeting_id, save_path, file_size)
                    
                    downloaded, total = state.get_progress()
                    logger.info(f"[Worker-{worker_id}] ✓ Downloaded [{downloaded}/{total}]: {title[:40]} ({file_size} bytes)")
                    return True
                else:
                    state.mark_failure(meeting_id, "No transcript content found")
                    logger.warning(f"[Worker-{worker_id}] ✗ No content: {title[:40]}")
                    return False
                    
            finally:
                context.close()
                browser.close()
                
    except Exception as e:
        state.mark_failure(meeting_id, str(e))
        logger.error(f"[Worker-{worker_id}] ✗ Error for {title[:40]}: {str(e)[:100]}")
        return False


def extract_transcript_text(page: Page, meeting: Dict) -> str:
    """Extract transcript text from the page."""
    try:
        # Method 1: Look for transcript container
        selectors = [
            '[class*="transcript"]',
            '[class*="speech"]',
            '.otterTranscript',
            '[data-testid*="transcript"]',
            'main',
        ]
        
        content_parts = []
        
        for selector in selectors:
            try:
                elements = page.query_selector_all(selector)
                for el in elements:
                    text = el.inner_text()
                    if text and len(text) > 50:
                        content_parts.append(text)
            except:
                continue
        
        # If we found content, format it
        if content_parts:
            # Combine and deduplicate
            all_text = '\n'.join(content_parts)
            
            # Create formatted output
            header = f"""Meeting: {meeting.get('title', 'Unknown').split(chr(10))[0]}
URL: {meeting['url']}
Downloaded: {datetime.now().isoformat()}
Method: parallel_text_extraction
{'='*60}

"""
            return header + all_text
        
        # Method 2: Get all visible text
        body_text = page.evaluate('() => document.body.innerText')
        if body_text and len(body_text) > 200:
            header = f"""Meeting: {meeting.get('title', 'Unknown').split(chr(10))[0]}
URL: {meeting['url']}
Downloaded: {datetime.now().isoformat()}
Method: parallel_body_extraction
{'='*60}

"""
            return header + body_text
        
        return ""
        
    except Exception as e:
        logger.debug(f"Extraction error: {e}")
        return ""


def run_parallel_download(num_workers: int = 4):
    """Run parallel downloads with multiple workers."""
    
    logger.info("=" * 60)
    logger.info("OTTER.AI PARALLEL DOWNLOADER")
    logger.info("=" * 60)
    logger.info(f"Workers: {num_workers}")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    
    # Ensure download directory exists
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    
    # Load state
    state = ParallelState()
    
    # Get pending meetings
    pending = state.get_pending_meetings()
    
    if not pending:
        logger.info("No pending meetings to download!")
        downloaded, total = state.get_progress()
        logger.info(f"Already downloaded: {downloaded}/{total}")
        return
    
    logger.info(f"Pending downloads: {len(pending)}")
    state.total_to_download = len(pending)
    
    downloaded_before, total = state.get_progress()
    logger.info(f"Already downloaded: {downloaded_before}/{total}")
    
    start_time = time.time()
    success_count = 0
    fail_count = 0
    
    # Run parallel downloads
    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="Worker") as executor:
        futures = {}
        
        for i, meeting in enumerate(pending):
            worker_id = i % num_workers
            future = executor.submit(download_single_transcript, meeting, state, worker_id)
            futures[future] = meeting
        
        # Process completed downloads
        for future in as_completed(futures):
            meeting = futures[future]
            try:
                result = future.result()
                if result:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(f"Future error: {e}")
    
    # Final stats
    elapsed = time.time() - start_time
    downloaded_after, total = state.get_progress()
    
    logger.info("=" * 60)
    logger.info("DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Time elapsed: {elapsed/60:.1f} minutes")
    logger.info(f"Downloaded this session: {success_count}")
    logger.info(f"Failed this session: {fail_count}")
    logger.info(f"Total downloaded: {downloaded_after}/{total}")
    logger.info(f"Speed: {success_count/(elapsed/60):.1f} transcripts/minute")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Otter.ai Parallel Downloader")
    parser.add_argument('--workers', type=int, default=4, help='Number of parallel workers (default: 4)')
    args = parser.parse_args()
    
    run_parallel_download(num_workers=args.workers)
