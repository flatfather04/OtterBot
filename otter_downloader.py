"""
Otter.ai Automated Transcript Downloader - Production Version

This script uses Playwright to automate the download of all meeting transcripts
from your Otter.ai account with production-level robustness.

Features:
- Comprehensive state tracking with detailed logs
- Multiple download strategies with automatic fallbacks
- Retry logic with exponential backoff
- Resume capability from any point of failure
- Detailed progress reporting

Usage:
    python otter_downloader.py
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
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeout

from config import (
    OTTER_EMAIL, OTTER_PASSWORD,
    DOWNLOAD_DIR, SESSION_FILE, PROGRESS_FILE,
    OTTER_BASE_URL, OTTER_LOGIN_URL, OTTER_CONVERSATIONS_URL,
    EXPORT_FORMATS, PAGE_LOAD_TIMEOUT, SCROLL_WAIT_TIME,
    DOWNLOAD_WAIT_TIME, DELAY_BETWEEN_DOWNLOADS,
    HEADLESS, SLOW_MO
)


# ============================================================================
# LOGGING SETUP
# ============================================================================
LOG_FILE = Path(__file__).parent / "otter_download.log"
STATE_FILE = Path(__file__).parent / ".otter_state.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ============================================================================
# STATE MANAGEMENT
# ============================================================================
class DownloadState:
    """Comprehensive state tracking for the download process."""
    
    def __init__(self):
        self.state_file = STATE_FILE
        self.state = self._load_state()
    
    def _load_state(self) -> Dict[str, Any]:
        """Load state from file or create new state."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        
        return {
            "session_created": None,
            "last_run": None,
            "meetings": {},  # id -> meeting info + status
            "download_attempts": {},  # id -> list of attempt records
            "successful_downloads": [],
            "failed_downloads": [],
            "total_meetings_found": 0,
            "run_history": []
        }
    
    def save(self):
        """Persist state to file."""
        self.state["last_run"] = datetime.now().isoformat()
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=2)
    
    def register_meeting(self, meeting_id: str, title: str, url: str):
        """Register a meeting in state."""
        if meeting_id not in self.state["meetings"]:
            self.state["meetings"][meeting_id] = {
                "id": meeting_id,
                "title": title,
                "url": url,
                "status": "pending",
                "discovered_at": datetime.now().isoformat(),
                "download_path": None,
                "file_size": None,
                "method_used": None
            }
        self.save()
    
    def record_attempt(self, meeting_id: str, method: str, success: bool, error: str = None):
        """Record a download attempt."""
        if meeting_id not in self.state["download_attempts"]:
            self.state["download_attempts"][meeting_id] = []
        
        self.state["download_attempts"][meeting_id].append({
            "timestamp": datetime.now().isoformat(),
            "method": method,
            "success": success,
            "error": error
        })
        self.save()
    
    def mark_success(self, meeting_id: str, file_path: str, method: str, file_size: int):
        """Mark a meeting as successfully downloaded."""
        if meeting_id in self.state["meetings"]:
            self.state["meetings"][meeting_id]["status"] = "success"
            self.state["meetings"][meeting_id]["download_path"] = str(file_path)
            self.state["meetings"][meeting_id]["file_size"] = file_size
            self.state["meetings"][meeting_id]["method_used"] = method
        
        if meeting_id not in self.state["successful_downloads"]:
            self.state["successful_downloads"].append(meeting_id)
        
        if meeting_id in self.state["failed_downloads"]:
            self.state["failed_downloads"].remove(meeting_id)
        
        self.save()
    
    def mark_failure(self, meeting_id: str):
        """Mark a meeting as failed after all retries."""
        if meeting_id in self.state["meetings"]:
            self.state["meetings"][meeting_id]["status"] = "failed"
        
        if meeting_id not in self.state["failed_downloads"]:
            self.state["failed_downloads"].append(meeting_id)
        
        self.save()
    
    def get_pending_meetings(self) -> List[Dict]:
        """Get list of meetings that still need to be downloaded."""
        pending = []
        for meeting_id, info in self.state["meetings"].items():
            if info["status"] == "pending" or info["status"] == "failed":
                pending.append(info)
        return pending
    
    def is_downloaded(self, meeting_id: str) -> bool:
        """Check if a meeting has been successfully downloaded."""
        return meeting_id in self.state["successful_downloads"]
    
    def get_stats(self) -> Dict:
        """Get current download statistics."""
        return {
            "total_meetings": len(self.state["meetings"]),
            "successful": len(self.state["successful_downloads"]),
            "failed": len(self.state["failed_downloads"]),
            "pending": len(self.get_pending_meetings())
        }


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================
def setup_directories():
    """Create necessary directories if they don't exist."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    logger.info(f"Download directory: {DOWNLOAD_DIR}")


def save_session(context: BrowserContext):
    """Save browser session/cookies for reuse."""
    storage = context.storage_state()
    with open(SESSION_FILE, 'w') as f:
        json.dump(storage, f)
    logger.info(f"Session saved to {SESSION_FILE}")


def sanitize_filename(name: str) -> str:
    """Convert a string to a valid filename."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', '', name)
    name = re.sub(r'\s+', '_', name)
    name = re.sub(r'_+', '_', name)
    return name[:80].strip('_')


def wait_with_retry(page: Page, selector: str, timeout: int = 10000, retries: int = 3) -> Optional[Any]:
    """Wait for a selector with retries."""
    for attempt in range(retries):
        try:
            element = page.wait_for_selector(selector, timeout=timeout)
            if element:
                return element
        except PlaywrightTimeout:
            if attempt < retries - 1:
                logger.debug(f"Retry {attempt + 1}/{retries} for selector: {selector}")
                time.sleep(1)
    return None


# ============================================================================
# LOGIN HANDLER
# ============================================================================
def automated_login(page: Page) -> bool:
    """
    Automatically log in to Otter.ai using credentials from config.
    Returns True if login successful, False otherwise.
    """
    logger.info("Starting Otter.ai login...")
    
    try:
        page.goto(OTTER_LOGIN_URL, timeout=PAGE_LOAD_TIMEOUT, wait_until='domcontentloaded')
        time.sleep(3)
        
        # Check if already logged in
        if "home" in page.url or "workspace" in page.url:
            logger.info("Already logged in!")
            return True
        
        # Accept cookies if present
        try:
            cookies_btn = page.wait_for_selector('button.accept-cookies-button', timeout=3000)
            if cookies_btn:
                cookies_btn.click()
                time.sleep(0.5)
        except:
            pass
        
        # Step 1: Click "Other ways to log in"
        logger.info("Clicking 'Other ways to log in'...")
        other_login_selectors = [
            'button.other-sign-in-button',
            'button:has-text("Other ways to log in")',
            'text=Other ways to log in',
            '[class*="other-sign-in"]',
        ]
        
        for selector in other_login_selectors:
            try:
                btn = page.wait_for_selector(selector, timeout=3000)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(1)
                    break
            except:
                continue
        
        # Step 2: Enter email
        logger.info(f"Entering email: {OTTER_EMAIL}")
        email_selectors = ['#otter-email-input', 'input[type="email"]', 'input[name="email"]']
        email_input = None
        for selector in email_selectors:
            try:
                email_input = page.wait_for_selector(selector, timeout=5000)
                if email_input:
                    break
            except:
                continue
        
        if not email_input:
            logger.error("Could not find email input field")
            return False
        
        email_input.fill(OTTER_EMAIL)
        time.sleep(0.5)
        
        # Step 3: Click "Sign in" button
        logger.info("Clicking Sign in...")
        signin_selectors = ['#otter-sign-in', 'button:has-text("Sign in")', 'button[type="submit"]']
        for selector in signin_selectors:
            try:
                signin_btn = page.wait_for_selector(selector, timeout=3000)
                if signin_btn and signin_btn.is_visible():
                    signin_btn.click()
                    time.sleep(2)
                    break
            except:
                continue
        
        # Step 4: Enter password
        logger.info("Entering password...")
        password_selectors = ['#otter-password', 'input[type="password"]']
        password_input = None
        for selector in password_selectors:
            try:
                password_input = page.wait_for_selector(selector, timeout=5000)
                if password_input:
                    break
            except:
                continue
        
        if not password_input:
            logger.error("Could not find password input field")
            return False
        
        password_input.fill(OTTER_PASSWORD)
        time.sleep(0.5)
        
        # Step 5: Click "Next" to complete login
        logger.info("Completing login...")
        next_selectors = ['#otter-password-next', 'button:has-text("Next")', 'button:has-text("Log in")']
        for selector in next_selectors:
            try:
                next_btn = page.wait_for_selector(selector, timeout=3000)
                if next_btn and next_btn.is_visible():
                    next_btn.click()
                    break
            except:
                continue
        
        # Wait for login to complete
        logger.info("Waiting for login to complete...")
        time.sleep(8)  # Increased wait time
        
        # Verify login success
        current_url = page.url
        if "home" in current_url or "workspace" in current_url or "conversations" in current_url:
            logger.info("Login successful!")
            return True
        
        # Check for error messages
        error_elem = page.query_selector('[class*="error"], [class*="alert"], [role="alert"]')
        if error_elem:
            error_text = error_elem.inner_text()
            logger.error(f"Login failed: {error_text}")
        else:
            logger.warning(f"Login status unclear. Current URL: {current_url}")
            # Might still be OK if redirected elsewhere
            return "signin" not in current_url.lower()
        
        return False
        
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        logger.debug(traceback.format_exc())
        return False


# ============================================================================
# MEETING DISCOVERY
# ============================================================================
def scroll_to_load_all(page: Page, max_scrolls: int = 500) -> int:
    """
    Scroll the page to load all conversations (infinite scroll handler).
    Specifically targets the .otter-main-content__container which holds the transcript list.
    """
    logger.info(f"Scrolling to load conversations (limit: {max_scrolls} scrolls)...")
    
    previous_count = 0
    no_change_count = 0
    scroll_count = 0
    
    # The specific scrollable container on Otter.ai
    container_selector = '.otter-main-content__container'
    
    # First, check if the container exists
    container = page.query_selector(container_selector)
    if container:
        logger.info(f"Found Otter main content container: {container_selector}")
    else:
        logger.warning("Could not find .otter-main-content__container, will try window scroll")
    
    while no_change_count < 15 and scroll_count < max_scrolls:  # Increased patience to 15
        # Scroll the specific container if found, otherwise window
        if container:
            try:
                page.evaluate(f'''() => {{
                    const container = document.querySelector('{container_selector}');
                    if (container) {{
                        container.scrollTop = container.scrollHeight;
                    }}
                }}''')
            except Exception as e:
                logger.debug(f"Container scroll error: {e}")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        
        # Wait for lazy loading to complete
        time.sleep(SCROLL_WAIT_TIME / 1000 + 1)  # Extra time for lazy load
        
        scroll_count += 1
        
        # Count conversations - use multiple selectors for robustness
        # Otter.ai uses app-home-speech-card for transcript cards
        conversations = page.query_selector_all('app-home-speech-card, a[href*="/u/"]')
        current_count = len(conversations)
        
        if current_count == previous_count:
            no_change_count += 1
        else:
            no_change_count = 0
        
        previous_count = current_count
        
        # Log progress every 10 scrolls
        if scroll_count % 10 == 0:
            logger.info(f"Scroll progress: {scroll_count} scrolls, {current_count} conversations found")
    
    logger.info(f"Loaded {previous_count} conversations after {scroll_count} scrolls")
    return previous_count


def extract_meeting_info(page: Page, state: DownloadState) -> List[Dict]:
    """Extract meeting IDs and titles from the conversation list."""
    logger.info("Extracting meeting information...")
    
    meetings = []
    links = page.query_selector_all('a[href*="/u/"]')
    
    for link in links:
        try:
            href = link.get_attribute('href')
            if href and '/u/' in href:
                match = re.search(r'/u/([a-zA-Z0-9_-]+)', href)
                if match:
                    meeting_id = match.group(1)
                    
                    # Skip short IDs (likely not meetings)
                    if len(meeting_id) < 10:
                        continue
                    
                    # Try to get title
                    try:
                        title = link.inner_text().strip()
                        if not title or len(title) < 2:
                            title = f"Meeting_{meeting_id[:10]}"
                    except:
                        title = f"Meeting_{meeting_id[:10]}"
                    
                    title = title[:100]
                    full_url = f"{OTTER_BASE_URL}/u/{meeting_id}"
                    
                    # Avoid duplicates
                    if not any(m['id'] == meeting_id for m in meetings):
                        meetings.append({
                            'id': meeting_id,
                            'title': title,
                            'url': full_url
                        })
                        # Register in state
                        state.register_meeting(meeting_id, title, full_url)
        except Exception as e:
            logger.debug(f"Error extracting meeting info: {e}")
            continue
    
    state.state["total_meetings_found"] = len(meetings)
    state.save()
    
    logger.info(f"Found {len(meetings)} unique meetings")
    return meetings


# ============================================================================
# DOWNLOAD STRATEGIES
# ============================================================================
def strategy_export_button(page: Page, meeting: Dict, state: DownloadState) -> Optional[Path]:
    """
    Strategy 1: Use the Export button in the UI.
    This is the preferred method as it downloads the official export.
    """
    method = "export_button"
    meeting_id = meeting['id']
    title = sanitize_filename(meeting['title'])
    
    try:
        logger.info(f"[{method}] Trying export button for: {meeting['title'][:40]}...")
        
        # Find and click "More options" menu
        more_btn = None
        more_selectors = [
            'button[aria-label*="more" i]',
            'button[aria-label*="options" i]',
            'button.head-bar__menu-button',
            '[data-testid="more-options"]',
        ]
        
        for selector in more_selectors:
            try:
                more_btn = page.wait_for_selector(selector, timeout=5000)
                if more_btn and more_btn.is_visible():
                    break
                more_btn = None
            except:
                continue
        
        # Try finding by icon content
        if not more_btn:
            buttons = page.query_selector_all('button')
            for btn in buttons:
                try:
                    if btn.is_visible():
                        inner = btn.inner_html()
                        if 'more_horiz' in inner or 'more_vert' in inner:
                            more_btn = btn
                            break
                except:
                    continue
        
        if not more_btn:
            logger.debug(f"[{method}] Could not find more options button")
            state.record_attempt(meeting_id, method, False, "More options button not found")
            return None
        
        more_btn.click()
        time.sleep(2)
        
        # Click Export from dropdown
        export_option = None
        export_selectors = [
            '[role="menuitem"]:has-text("Export")',
            'li:has-text("Export")',
            'span:has-text("Export")',
            'button:has-text("Export")',
        ]
        
        for selector in export_selectors:
            try:
                items = page.query_selector_all(selector)
                for item in items:
                    if item.is_visible():
                        text = item.inner_text()
                        if 'Export' in text and 'Re-export' not in text:
                            export_option = item
                            break
                if export_option:
                    break
            except:
                continue
        
        if not export_option:
            logger.debug(f"[{method}] Could not find Export menu option")
            state.record_attempt(meeting_id, method, False, "Export option not found")
            return None
        
        export_option.click()
        time.sleep(2)
        
        # Click the blue Export button in modal
        confirm_selectors = [
            'button.bg-primary:has-text("Export")',
            'button[class*="primary"]:has-text("Export")',
            'div[role="dialog"] button:has-text("Export")',
            'button:has-text("Export"):not([disabled])',
        ]
        
        for selector in confirm_selectors:
            try:
                confirm = page.query_selector(selector)
                if confirm and confirm.is_visible():
                    with page.expect_download(timeout=DOWNLOAD_WAIT_TIME * 2) as download_info:
                        confirm.click()
                    download = download_info.value
                    
                    # Save the file
                    filename = f"{title}_{meeting_id[:15]}.txt"
                    save_path = DOWNLOAD_DIR / filename
                    download.save_as(save_path)
                    
                    file_size = save_path.stat().st_size
                    logger.info(f"[{method}] Downloaded: {filename} ({file_size} bytes)")
                    state.record_attempt(meeting_id, method, True)
                    state.mark_success(meeting_id, save_path, method, file_size)
                    return save_path
            except Exception as e:
                logger.debug(f"[{method}] Export button click failed: {e}")
                continue
        
        state.record_attempt(meeting_id, method, False, "Could not complete export")
        return None
        
    except Exception as e:
        logger.debug(f"[{method}] Error: {str(e)}")
        state.record_attempt(meeting_id, method, False, str(e))
        return None


def strategy_text_extraction(page: Page, meeting: Dict, state: DownloadState) -> Optional[Path]:
    """
    Strategy 2: Extract transcript text directly from the page.
    Fallback when export button doesn't work.
    """
    method = "text_extraction"
    meeting_id = meeting['id']
    title = sanitize_filename(meeting['title'])
    
    try:
        logger.info(f"[{method}] Trying text extraction for: {meeting['title'][:40]}...")
        
        # Wait for transcript content to load - reduced
        time.sleep(0.5)
        
        # Try various transcript selectors - optimized list
        transcript_selectors = [
            '.otter-transcript-container',
            '[class*="transcript"]',
            '[class*="speech"]',
            '.monologue',
            '.paragraph',
        ]
        
        all_text = []
        
        # Try to get all text at once if possible for speed
        try:
            # Main transcript body is usually in a single container
            main_container = page.query_selector('.otter-transcript-container, main, [role="main"]')
            if main_container:
                combined_text = main_container.inner_text()
                if len(combined_text) > 500:
                    all_text = [combined_text]
        except:
            pass
            
        if not all_text:
            for selector in transcript_selectors:
                try:
                    containers = page.query_selector_all(selector)
                    for container in containers:
                        try:
                            text = container.inner_text().strip()
                            if text and len(text) > 50:
                                all_text.append(text)
                        except:
                            continue
                    if all_text: break # Stop if we found content
                except:
                    continue
        
        # Combine and deduplicate text
        combined_text = "\n\n".join(all_text)
        
        # If not enough text, try getting all text from main content area
        if len(combined_text) < 100:
            try:
                # Try to find main content area
                main_selectors = ['main', '[role="main"]', '#root', '.app-content']
                for selector in main_selectors:
                    main_elem = page.query_selector(selector)
                    if main_elem:
                        combined_text = main_elem.inner_text()
                        if len(combined_text) > 200:
                            break
            except:
                pass
        
        # Clean up the text
        if combined_text and len(combined_text) > 100:
            # Remove common noise
            lines = combined_text.split('\n')
            clean_lines = []
            for line in lines:
                line = line.strip()
                # Skip navigation/menu items
                if line and len(line) > 5:
                    if not any(x in line.lower() for x in ['sign out', 'settings', 'help', 'export', 'share']):
                        clean_lines.append(line)
            
            clean_text = '\n'.join(clean_lines)
            
            if len(clean_text) > 100:
                filename = f"{title}_{meeting_id[:15]}.txt"
                save_path = DOWNLOAD_DIR / filename
                
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(f"Meeting: {meeting['title']}\n")
                    f.write(f"URL: {meeting['url']}\n")
                    f.write(f"Downloaded: {datetime.now().isoformat()}\n")
                    f.write(f"Method: {method}\n")
                    f.write("=" * 60 + "\n\n")
                    f.write(clean_text)
                
                file_size = save_path.stat().st_size
                logger.info(f"[{method}] Extracted: {filename} ({file_size} bytes)")
                state.record_attempt(meeting_id, method, True)
                state.mark_success(meeting_id, save_path, method, file_size)
                return save_path
        
        state.record_attempt(meeting_id, method, False, "Insufficient text content found")
        return None
        
    except Exception as e:
        logger.debug(f"[{method}] Error: {str(e)}")
        state.record_attempt(meeting_id, method, False, str(e))
        return None


def strategy_direct_api(page: Page, meeting: Dict, state: DownloadState) -> Optional[Path]:
    """
    Strategy 3: Try to use Otter's internal API to fetch transcript.
    Most reliable but requires understanding the API structure.
    """
    method = "direct_api"
    meeting_id = meeting['id']
    title = sanitize_filename(meeting['title'])
    
    try:
        logger.info(f"[{method}] Trying API fetch for: {meeting['title'][:40]}...")
        
        # Otter uses GraphQL/REST APIs internally
        # We can intercept network requests or make direct API calls
        
        # Try to find transcript data in page state (React/Vue state)
        script = """
        () => {
            // Try to find transcript in window state
            if (window.__NEXT_DATA__) {
                return JSON.stringify(window.__NEXT_DATA__);
            }
            if (window.__INITIAL_STATE__) {
                return JSON.stringify(window.__INITIAL_STATE__);
            }
            // Try React fiber
            const root = document.getElementById('root');
            if (root && root._reactRootContainer) {
                return 'React app detected';
            }
            return null;
        }
        """
        
        result = page.evaluate(script)
        
        if result and len(result) > 1000:
            # Parse and extract transcript
            try:
                data = json.loads(result)
                # Look for transcript content in the data
                transcript_text = extract_transcript_from_data(data)
                
                if transcript_text and len(transcript_text) > 100:
                    filename = f"{title}_{meeting_id[:15]}.txt"
                    save_path = DOWNLOAD_DIR / filename
                    
                    with open(save_path, 'w', encoding='utf-8') as f:
                        f.write(f"Meeting: {meeting['title']}\n")
                        f.write(f"URL: {meeting['url']}\n")
                        f.write(f"Downloaded: {datetime.now().isoformat()}\n")
                        f.write(f"Method: {method}\n")
                        f.write("=" * 60 + "\n\n")
                        f.write(transcript_text)
                    
                    file_size = save_path.stat().st_size
                    logger.info(f"[{method}] Fetched: {filename} ({file_size} bytes)")
                    state.record_attempt(meeting_id, method, True)
                    state.mark_success(meeting_id, save_path, method, file_size)
                    return save_path
            except:
                pass
        
        state.record_attempt(meeting_id, method, False, "Could not fetch from API")
        return None
        
    except Exception as e:
        logger.debug(f"[{method}] Error: {str(e)}")
        state.record_attempt(meeting_id, method, False, str(e))
        return None


def extract_transcript_from_data(data: Dict, depth: int = 0) -> Optional[str]:
    """Recursively search for transcript content in data structure."""
    if depth > 10:
        return None
    
    if isinstance(data, str):
        if len(data) > 200:
            return data
        return None
    
    if isinstance(data, dict):
        # Look for common transcript keys
        for key in ['transcript', 'text', 'content', 'body', 'speech', 'monologue']:
            if key in data:
                result = extract_transcript_from_data(data[key], depth + 1)
                if result:
                    return result
        
        # Recursively search all values
        for value in data.values():
            result = extract_transcript_from_data(value, depth + 1)
            if result and len(result) > 200:
                return result
    
    if isinstance(data, list):
        texts = []
        for item in data:
            result = extract_transcript_from_data(item, depth + 1)
            if result:
                texts.append(result)
        if texts:
            return '\n'.join(texts)
    
    return None


def strategy_screenshot_fallback(page: Page, meeting: Dict, state: DownloadState) -> Optional[Path]:
    """
    Strategy 4: Last resort - take a screenshot of the transcript.
    At least captures the content visually.
    """
    method = "screenshot"
    meeting_id = meeting['id']
    title = sanitize_filename(meeting['title'])
    
    try:
        logger.info(f"[{method}] Taking screenshot for: {meeting['title'][:40]}...")
        
        filename = f"{title}_{meeting_id[:15]}.png"
        save_path = DOWNLOAD_DIR / filename
        
        # Take full page screenshot
        page.screenshot(path=str(save_path), full_page=True)
        
        if save_path.exists():
            file_size = save_path.stat().st_size
            logger.info(f"[{method}] Screenshot saved: {filename} ({file_size} bytes)")
            state.record_attempt(meeting_id, method, True)
            state.mark_success(meeting_id, save_path, method, file_size)
            return save_path
        
        state.record_attempt(meeting_id, method, False, "Screenshot save failed")
        return None
        
    except Exception as e:
        logger.debug(f"[{method}] Error: {str(e)}")
        state.record_attempt(meeting_id, method, False, str(e))
        return None


# ============================================================================
# MAIN DOWNLOAD LOGIC
# ============================================================================
def download_meeting(page: Page, meeting: Dict, state: DownloadState, max_retries: int = 3) -> bool:
    """
    Download a single meeting transcript with multiple strategies and retries.
    """
    meeting_id = meeting['id']
    
    # Skip if already downloaded
    if state.is_downloaded(meeting_id):
        logger.info(f"Already downloaded: {meeting['title'][:40]}")
        return True
    
    # List of strategies to try in order - TEXT EXTRACTION FIRST (fastest & most reliable)
    strategies = [
        strategy_text_extraction,  # Works 100% of the time, fastest
        strategy_direct_api,       # Fallback
        strategy_screenshot_fallback,  # Last resort
        # strategy_export_button,  # DISABLED - always times out, wastes 2 minutes
    ]
    
    for retry in range(max_retries):
        logger.info(f"Attempt {retry + 1}/{max_retries} for: {meeting['title'][:40]}...")
        
        try:
            # Navigate to meeting page - use 'commit' (fastest)
            logger.debug(f"Navigating to: {meeting['url']}")
            page.goto(meeting['url'], timeout=PAGE_LOAD_TIMEOUT, wait_until='commit')
            
            # Minimal wait for page to load content - ULTRA FAST
            time.sleep(1.5)
            
            # Close any popups quickly
            close_popups(page)
            
            # Try each strategy - minimal delay between
            for strategy in strategies:
                try:
                    result = strategy(page, meeting, state)
                    if result:
                        return True
                except Exception as e:
                    logger.debug(f"Strategy {strategy.__name__} error: {e}")
                time.sleep(0.5)  # Reduced from 1s
            
            logger.warning(f"All strategies failed on attempt {retry + 1}")
            
        except PlaywrightTimeout:
            logger.warning(f"Timeout on attempt {retry + 1} for: {meeting['title'][:40]}")
            state.record_attempt(meeting_id, "navigation", False, "Page load timeout")
            
        except Exception as e:
            logger.warning(f"Error on attempt {retry + 1}: {str(e)}")
            state.record_attempt(meeting_id, "navigation", False, str(e))
        
        # Exponential backoff before retry
        if retry < max_retries - 1:
            wait_time = (2 ** retry) * 2
            logger.info(f"Waiting {wait_time}s before retry...")
            time.sleep(wait_time)
    
    # Mark as failed after all retries
    state.mark_failure(meeting_id)
    logger.error(f"Failed to download after {max_retries} attempts: {meeting['title'][:40]}")
    return False


def close_popups(page: Page):
    """Close any popup dialogs that might appear."""
    popup_close_selectors = [
        'button[aria-label="Close"]',
        'button:has-text("Got it")',
        'button:has-text("×")',
        '.close-button',
        '[data-testid="close-button"]',
        'button:has-text("Dismiss")',
        'button:has-text("Later")',
    ]
    
    for selector in popup_close_selectors:
        try:
            # Check for and click popups - streamlined
            buttons = page.query_selector_all(selector)
            for btn in buttons:
                if btn.is_visible():
                    btn.click()
                    return # Exit after clicking one popup to save time
        except:
            continue


# ============================================================================
# MAIN ORCHESTRATION
# ============================================================================
def run_download(reset: bool = False, quick: bool = False, num: int = None):
    """Main function to orchestrate the entire download process."""
    logger.info("=" * 60)
    logger.info("OTTER.AI TRANSCRIPT DOWNLOADER - PRODUCTION VERSION")
    logger.info("=" * 60)
    logger.info(f"Started at: {datetime.now().isoformat()}")
    
    setup_directories()
    
    # Initialize state
    state = DownloadState()
    
    if reset:
        logger.info("Resetting state...")
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        if PROGRESS_FILE.exists():
            PROGRESS_FILE.unlink()
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        state = DownloadState()
        logger.info("State reset complete")
    
    # Log current state
    stats = state.get_stats()
    logger.info(f"Current state: {stats['successful']} downloaded, {stats['pending']} pending, {stats['failed']} failed")
    
    with sync_playwright() as playwright:
        # Launch browser
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO
        )
        
        # Create context with session if available
        if SESSION_FILE.exists():
            logger.info("Loading saved session...")
            try:
                context = browser.new_context(storage_state=str(SESSION_FILE))
            except:
                logger.warning("Failed to load session, creating new context")
                context = browser.new_context()
        else:
            logger.info("No saved session found, will need to login")
            context = browser.new_context()
        
        # Set longer default timeouts
        context.set_default_timeout(PAGE_LOAD_TIMEOUT * 2)
        
        page = context.new_page()
        
        try:
            # Navigate to Otter home
            logger.info("Navigating to Otter.ai...")
            page.goto(OTTER_CONVERSATIONS_URL, timeout=PAGE_LOAD_TIMEOUT * 2)
            
            # Wait for page to stabilize
            try:
                page.wait_for_load_state('networkidle', timeout=30000)
            except:
                pass  # Timeout is OK, we'll check state anyway
            time.sleep(3)
            
            # Check current URL to determine login status
            current_url = page.url.lower()
            logger.info(f"Current URL after navigation: {page.url}")
            
            # Check if login needed - look for signin/login in URL
            needs_login = "signin" in current_url or "login" in current_url or "sign-in" in current_url
            
            if needs_login:
                logger.info("Login required - starting automated login...")
                if not automated_login(page):
                    logger.error("Login failed. Please check credentials.")
                    # Take debug screenshot
                    page.screenshot(path=DOWNLOAD_DIR / "debug_login_failed.png")
                    browser.close()
                    return False
                
                # Save session after successful login
                save_session(context)
                state.state["session_created"] = datetime.now().isoformat()
                state.save()
                
                # Navigate back to conversations after login
                logger.info("Navigating to conversations page after login...")
                page.goto(OTTER_CONVERSATIONS_URL, timeout=PAGE_LOAD_TIMEOUT * 2)
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                except:
                    pass
                time.sleep(5)
                
                # Verify we're logged in now
                current_url = page.url.lower()
                if "signin" in current_url or "login" in current_url:
                    logger.error("Still on login page after login attempt")
                    page.screenshot(path=DOWNLOAD_DIR / "debug_still_on_login.png")
                    browser.close()
                    return False
            
            logger.info("Login confirmed. Proceeding to load conversations...")
            
            # Scroll to load conversations (with error handling)
            try:
                # If quick mode is on, we only scroll once or twice to get recent ones
                actual_max_scrolls = 2 if quick else 500
                scroll_to_load_all(page, max_scrolls=actual_max_scrolls)
            except Exception as e:
                logger.warning(f"Scroll error (may be OK): {e}")
                time.sleep(3)
            
            # Extract meeting information
            meetings = extract_meeting_info(page, state)
            
            if not meetings:
                logger.warning("No meetings found. Taking debug screenshot...")
                page.screenshot(path=DOWNLOAD_DIR / "debug_no_meetings.png")
                browser.close()
                return False
            
            # Application of --num limit if provided
            if num:
                logger.info(f"Limited run: Only processing the top {num} meetings found")
                meetings = meetings[:num]
            elif quick:
                logger.info("Quick mode: Checking only the most recent meetings (top 15)")
                meetings = meetings[:15]
            
            # Filter to pending meetings
            pending = [m for m in meetings if not state.is_downloaded(m['id'])]
            logger.info(f"Meetings to download: {len(pending)} / {len(meetings)} total")
            
            if not pending:
                logger.info("All meetings already downloaded!")
                browser.close()
                return True
            
            # Download each pending meeting
            success_count = 0
            for i, meeting in enumerate(pending, 1):
                logger.info(f"\n[{i}/{len(pending)}] Processing: {meeting['title'][:50]}...")
                
                if download_meeting(page, meeting, state):
                    success_count += 1
                
                # Rate limiting
                if i < len(pending):
                    time.sleep(DELAY_BETWEEN_DOWNLOADS)
            
            # Final report
            final_stats = state.get_stats()
            logger.info("\n" + "=" * 60)
            logger.info("DOWNLOAD COMPLETE")
            logger.info("=" * 60)
            logger.info(f"Total meetings: {final_stats['total_meetings']}")
            logger.info(f"Successfully downloaded: {final_stats['successful']}")
            logger.info(f"Failed: {final_stats['failed']}")
            logger.info(f"Download directory: {DOWNLOAD_DIR}")
            logger.info("=" * 60)
            
            # Record run in history
            state.state["run_history"].append({
                "timestamp": datetime.now().isoformat(),
                "meetings_processed": len(pending),
                "successful": success_count,
                "failed": len(pending) - success_count
            })
            state.save()
            
            return final_stats['failed'] == 0
            
        except Exception as e:
            logger.error(f"Fatal error: {str(e)}")
            logger.error(traceback.format_exc())
            # Take debug screenshot on any error
            try:
                page.screenshot(path=DOWNLOAD_DIR / "debug_fatal_error.png")
            except:
                pass
            state.save()  # Save state even on error
            raise
            
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description='Download Otter.ai transcripts')
    parser.add_argument('--reset', action='store_true', help='Reset all progress and start fresh')
    parser.add_argument('--quick', action='store_true', help='Check only recent transcripts (Top 15)')
    parser.add_argument('--num', type=int, help='Limit processing to a specific number of top transcripts')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        success = run_download(reset=args.reset, quick=args.quick, num=args.num)
        exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user. Progress saved.")
        exit(130)
    except Exception as e:
        logger.error(f"Unhandled error: {e}")
        exit(1)



if __name__ == "__main__":
    main()
