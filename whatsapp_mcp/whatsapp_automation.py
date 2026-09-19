import os
import re
import time
import shutil
import signal
import threading
import base64
import json
import tempfile
from selenium import webdriver

from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager

# OpenClaw manager agent workspace — incoming WhatsApp images are placed here
# so the vision-capable manager agent can inspect them directly.
_AGENT_WORKSPACE = os.path.expanduser("~/.openclaw/workspace/manager")
_WHATSAPP_IMAGES_DIR = os.path.join(_AGENT_WORKSPACE, "whatsapp_images")

# Sidebar preview patterns that indicate an incoming image / media message.
# WhatsApp shows these in the chat-list preview when the latest message is media.
_IMAGE_PREVIEW_PATTERNS = [
    "📷", "🖼", "🎥", "📹", "🎞",
    "Photo", "Foto", "photo", "foto",
    "Video", "video", "GIF", "Sticker", "sticker",
    "image", "media",
]

_MEDIA_EMOJIS = ("📷", "🖼", "🎥", "📹", "🎞")

# Recent image content hashes per sender — used to suppress re-dispatching the
# same photo twice (the downloader grabs "the latest incoming image" in the
# chat, which is sometimes a photo we already processed).
_RECENT_IMAGE_MD5 = {}  # {sender: (md5_hex, epoch)}


def _row_suggests_media(row_html, message_text):
    """
    Heuristic: does this sidebar chat row look like it contains a media
    message?  WhatsApp shows only the CAPTION TEXT in the preview for
    media-with-caption messages, so text-pattern matching alone misses them.
    We check the row's HTML for media emojis/icons that are NOT part of the
    extracted message text itself (i.e. structural indicators, not something
    the user typed).
    """
    if not row_html:
        return False
    for e in _MEDIA_EMOJIS:
        if e in row_html and e not in message_text:
            return True
    for marker in ('data-testid="media', 'image-message', 'media-message',
                   'data-icon="image"', 'data-icon="video"'):
        if marker in row_html:
            return True
    return False


# ── WebDriver binary resolution / stale process cleanup ──────────────────────
#
# Background: webdriver_manager re-downloads the driver binary on every
# install() call and overwrites the file in ~/.wdm.  If a *previous* run left an
# orphaned geckodriver/chromedriver process alive, the kernel still has that
# binary mapped as an executing image and the overwrite fails with
# "[Errno 26] Text file busy", which killed startup entirely.  We therefore:
#   1. reap orphaned driver/browser processes before launching,
#   2. prefer an already-present driver binary (env var, PATH, or ~/.wdm cache)
#      so the common path never touches the network or rewrites the file,
#   3. only fall back to webdriver_manager download when nothing usable exists.

def _own_processes():
    """Yield (pid, cmdline) for processes owned by the current user."""
    try:
        uid = os.getuid()
    except AttributeError:  # non-POSIX
        return
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
        except Exception:
            continue
        if cmdline:
            yield pid, cmdline


def _terminate(pid, label):
    """SIGTERM then SIGKILL a pid, ignoring races."""
    for sig, wait in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 0.2)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except Exception as e:
            print(f"[driver] Could not signal {label} pid {pid}: {e}")
            return False
        time.sleep(wait)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"[driver] Reaped stale {label} (pid {pid})")
            return True
    return False


def _wait_binary_writable(path, timeout=15):
    """
    Wait until *path* can be opened for appending (i.e. no running process
    holds it as an executing image).  Returns True when writable, False on
    timeout.  Errno 26 (ETXTBSY) means "still busy" — keep polling.
    """
    if not path:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not os.path.exists(path):
                return True
            with open(path, "ab"):
                return True
        except OSError as e:
            if getattr(e, "errno", None) != 26:
                return True
            time.sleep(0.5)
    return False


def _kill_stale_drivers(session_path=None, except_pids=()):
    """
    Kill leftover geckodriver/chromedriver processes and any browser still
    holding our automation profile.  These are only ever spawned by this
    module, so terminating them cannot disturb the user's own browser — the
    browser match is additionally scoped to our session directory.

    Also reaps zombie/defunct driver children (they still pin the binary via
    mm->exe in some kernels until wait()ed by their parent) and waits for the
    ~/.wdm binary to become writable again so the caller never hits ETXTBSY.
    """
    except_pids = set(except_pids) | {os.getpid()}
    killed = 0
    for pid, cmdline in _own_processes():
        if pid in except_pids:
            continue
        low = cmdline.lower()
        # Zombies have empty cmdline; match them via /proc status.
        if not low:
            try:
                with open(f"/proc/{pid}/status") as fh:
                    status = fh.read()
                if "State:\tZ" not in status:
                    continue
                with open(f"/proc/{pid}/stat") as fh:
                    comm = fh.read().split(")", 1)[0].split("(", 1)[1]
                if comm not in ("geckodriver", "chromedriver", "firefox",
                                 "chrome", "chromium"):
                    continue
                low = comm
            except Exception:
                continue
        is_driver = "geckodriver" in low or "chromedriver" in low
        is_our_browser = bool(
            session_path
            and session_path in cmdline
            and ("firefox" in low or "chrome" in low or "chromium" in low)
        ) or (not cmdline and "firefox" in low)  # zombie firefox
        if is_driver or is_our_browser:
            label = "geckodriver/chromedriver" if is_driver else "automation browser"
            if _terminate(pid, label):
                killed += 1
    if killed:
        time.sleep(1)  # let the kernel release the executable image
    # Belt-and-braces: wait until every cached driver binary is writable so a
    # subsequent webdriver_manager.install() can never hit ETXTBSY.
    for name in ("geckodriver", "chromedriver"):
        root = os.path.expanduser(f"~/.wdm/drivers/{name}")
        if not os.path.isdir(root):
            continue
        for dirpath, _dn, fns in os.walk(root):
            for fn in fns:
                if fn == name or fn == f"{name}.exe":
                    _wait_binary_writable(os.path.join(dirpath, fn), timeout=10)
    return killed


def _usable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _cached_wdm_driver(name):
    """Newest usable driver binary already present in the ~/.wdm cache."""
    root = os.path.expanduser(f"~/.wdm/drivers/{name}")
    if not os.path.isdir(root):
        return None
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn == name or fn == f"{name}.exe":
                p = os.path.join(dirpath, fn)
                if _usable(p):
                    found.append(p)
    if not found:
        return None
    # Prefer the most recently modified (i.e. newest downloaded version)
    return max(found, key=lambda p: os.path.getmtime(p))


def _attempt_install(name, manager_cls, attempts=3):
    """
    Try webdriver_manager install() up to *attempts* times, treating ETXTBSY
    as "reap stale processes and retry" and FileNotFoundError (cache dir
    vanishing mid-install) as "recreate cache and retry".
    """
    last = None
    for i in range(1, attempts + 1):
        try:
            p = manager_cls().install()
            if _usable(p):
                return p
            last = FileNotFoundError(f"install() returned unusable path: {p!r}")
        except OSError as e:
            last = e
            if getattr(e, "errno", None) == 26:
                print(f"[driver] {name} install attempt {i} hit 'Text file busy' — "
                      "reaping stale processes and retrying")
                _kill_stale_drivers()
            elif getattr(e, "errno", None) == 2:
                print(f"[driver] {name} install attempt {i} hit missing-cache — retrying")
            else:
                raise
        # Prefer any cached binary that appeared/cleaned up between attempts
        cached = _cached_wdm_driver(name)
        if cached:
            print(f"[driver] Using cached {name}: {cached}")
            return cached
    raise last or RuntimeError(f"{name} install failed")


def _resolve_driver(name, env_var, manager_cls):
    """
    Return a path to a usable driver binary, trying cheapest options first:
      1. explicit override via environment variable
      2. system installation on PATH
      3. binary already cached in ~/.wdm (wait for it to become writable
         first so we don't hand selenium a busy path either)
      4. webdriver_manager download (ETXTBSY- and missing-cache-safe)
    """
    override = os.getenv(env_var)
    if override:
        if _usable(override):
            print(f"[driver] Using {name} from {env_var}: {override}")
            return override
        print(f"[driver] {env_var}={override!r} is not an executable file — ignoring")

    system = shutil.which(name)
    if _usable(system):
        print(f"[driver] Using system {name}: {system}")
        return system

    cached = _cached_wdm_driver(name)
    if cached:
        if _wait_binary_writable(cached):
            print(f"[driver] Using cached {name}: {cached}")
            return cached
        print(f"[driver] Cached {name} still busy — reaping stale users")
        _kill_stale_drivers()
        cached = _cached_wdm_driver(name)
        if cached and _usable(cached):
            print(f"[driver] Using cached {name} after cleanup: {cached}")
            return cached

    print(f"[driver] No local {name} found — downloading via webdriver_manager…")
    return _attempt_install(name, manager_cls)


class WhatsAppAutomation:

    def __init__(self, session_path=None):
        # The browser profile lives outside the git working tree when the
        # gateway is managing us (LOS_VAR), and beside this file otherwise,
        # which preserves the historical ./whatsapp_session location.
        #
        # An existing linked session is never abandoned: if the legacy
        # directory is populated and the new one is not, we keep using the
        # legacy path so nobody has to re-scan the QR code after upgrading.
        legacy = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "whatsapp_session"))
        if session_path is None:
            var_dir = os.environ.get("LOS_VAR")
            if var_dir:
                preferred = os.path.join(var_dir, "whatsapp_session")
                if not os.path.isdir(preferred) and os.path.isdir(legacy):
                    session_path = legacy
                else:
                    session_path = preferred
            else:
                session_path = legacy
        self.session_path = os.path.abspath(session_path)
        self.driver = None
        self.is_authenticated = False
        self.last_messages = {}  # Track last seen message per contact
        self.recently_sent = {}  # Track recently sent messages to avoid detecting them as incoming
        self.response_cooldown = {}  # Track cooldown periods after sending to prevent response loops
        self.last_messages_initialized = False  # Flag to initialize last_messages on first scan
        self.phone_to_name = {}  # Map phone numbers to contact names
        self.monitoring = False
        self.monitor_thread = None
        self.driver_lock = threading.RLock()
        self.last_error = None          # last startup failure, surfaced in the UI
        self._last_recovery_attempt = 0  # rate-limit automatic driver recovery


        # Ensure session directory exists
        os.makedirs(self.session_path, exist_ok=True)
        # Clean stale lock files
        for lock in ["parent.lock", "lock", ".parentlock"]:
            try:
                p = os.path.join(self.session_path, lock)
                if os.path.exists(p): 
                    os.remove(p)
            except: 
                pass
        
        print(f"WhatsApp automation initialized. Session: {self.session_path}")

    def _setup_driver(self):
        """Try to setup Chrome first, fallback to Firefox"""
        # Reap orphaned drivers/browsers from a previous run first. Leftovers
        # hold the driver binary (ETXTBSY) and lock the Firefox profile, which
        # makes every subsequent start fail until they are cleaned up.
        # Guard the reap in try/except: a transient /proc race must never
        # abort browser startup.
        try:
            _kill_stale_drivers(self.session_path)
        except Exception as e:
            print(f"[driver] stale-process cleanup failed (continuing anyway): {e}")
        self._clean_profile_locks()

        chrome_bins = ["/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chrome", "/usr/bin/chromium-browser"]
        chrome_available = any(os.path.exists(path) for path in chrome_bins)

        errors = []

        if chrome_available:
            try:
                print("Trying Chrome...")
                self._setup_chrome()
                return
            except Exception as e:
                print(f"Chrome failed: {e}")
                errors.append(f"Chrome: {e}")

        try:
            print("Trying Firefox...")
            self._setup_firefox()
            return
        except Exception as e:
            print(f"Firefox failed: {e}")
            errors.append(f"Firefox: {e}")

        # One retry after an aggressive cleanup — covers the case where the
        # browser died mid-launch and left its own driver/profile lock behind.
        print("Retrying browser launch after cleanup...")
        _kill_stale_drivers(self.session_path)
        self._clean_profile_locks()
        time.sleep(2)
        try:
            self._setup_firefox()
            return
        except Exception as e:
            print(f"Firefox retry failed: {e}")
            errors.append(f"Firefox retry: {e}")

        raise WebDriverException("No compatible browser found — " + "; ".join(errors))

    def _clean_profile_locks(self):
        """Remove stale Firefox/Chrome profile lock files."""
        for lock in ["parent.lock", "lock", ".parentlock",
                     "SingletonLock", "SingletonCookie", "SingletonSocket"]:
            p = os.path.join(self.session_path, lock)
            try:
                if os.path.lexists(p):
                    os.remove(p)
                    print(f"[driver] Removed stale profile lock: {lock}")
            except Exception:
                pass


    def _setup_chrome(self):
        options = ChromeOptions()
        if os.getenv("WHATSAPP_HEADLESS", "1") == "1":
            options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(f"--user-data-dir={self.session_path}")

        driver_path = _resolve_driver("chromedriver", "WHATSAPP_CHROMEDRIVER",
                                      ChromeDriverManager)
        service = ChromeService(driver_path)
        self.driver = webdriver.Chrome(service=service, options=options)
        self._init_page()


    def _setup_firefox(self):
        options = FirefoxOptions()
        if os.getenv("WHATSAPP_HEADLESS", "1") == "1":
            options.add_argument("--headless")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("-profile")
        options.add_argument(self.session_path)

        print(f"Starting Firefox with profile: {self.session_path}")
        driver_path = _resolve_driver("geckodriver", "WHATSAPP_GECKODRIVER",
                                      GeckoDriverManager)

        last_err = None
        for attempt in (1, 2):
            service = FirefoxService(driver_path)
            try:
                self.driver = webdriver.Firefox(service=service, options=options)
                self._init_page()
                return
            except OSError as e:
                last_err = e
                err_txt = str(e)
                if (getattr(e, "errno", None) == 26) or ("Text file busy" in err_txt):
                    print(f"[driver] geckodriver launch ETXTBSY (attempt {attempt}) — "
                          "reaping and retrying")
                    try:
                        _kill_stale_drivers(self.session_path)
                    except Exception:
                        pass
                    time.sleep(1)
                    continue
                raise
            except WebDriverException as wde:
                last_err = wde
                # Selenium wraps the execve failure in a WebDriverException
                if "Text file busy" in str(wde) or "Errno 26" in str(wde):
                    print(f"[driver] geckodriver launch ETXTBSY (wrapped, attempt {attempt}) — "
                          "reaping and retrying")
                    try:
                        _kill_stale_drivers(self.session_path)
                    except Exception:
                        pass
                    time.sleep(1)
                    continue
                raise
        raise WebDriverException(
            f"geckodriver could not be started after retries: {last_err}")


    def _init_page(self):
        self.driver.set_page_load_timeout(60)
        self.driver.implicitly_wait(5)
        print("Loading WhatsApp Web...")
        try:
            self.driver.get("https://web.whatsapp.com")
        except TimeoutException:
            print("Initial page load timed out, but proceeding...")

    def is_logged_in(self):
        """Thread-safe check for login status"""
        if not self.driver_lock.acquire(timeout=10):
            return self.is_authenticated
        try:
            return self._check_login_status()
        finally:
            self.driver_lock.release()

    def _check_login_status(self):
        """Internal check without lock acquisition"""
        if not self.driver: 
            return False
        
        try:
            self.driver.implicitly_wait(0)
            
            # Check for visible QR code (means not logged in)
            try:
                qr = self.driver.find_element(By.XPATH, "//canvas[@aria-label='Scan me!']")
                if qr.is_displayed():
                    if self.is_authenticated:
                        print("Logged out (QR visible)")
                    self.is_authenticated = False
                    return False
            except:
                pass

            # Check for main WhatsApp UI elements
            login_indicators = [
                "//div[@id='side']",  # Sidebar
                "//div[@id='pane-side']",  # Chat list pane
            ]
            
            for sel in login_indicators:
                if self.driver.find_elements(By.XPATH, sel):
                    if not self.is_authenticated:
                        print(f"Logged in!")
                        self.is_authenticated = True
                    return True

            return self.is_authenticated
        except:
            return self.is_authenticated
        finally:
            self.driver.implicitly_wait(5)

    def get_qr_code_image(self):
        """Get QR code as base64 image"""
        with self.driver_lock:
            if not self.driver:
                return None
            
            try:
                self.driver.implicitly_wait(0)
                qr = self.driver.find_element(By.XPATH, "//canvas[@aria-label='Scan me!']")
                if qr.is_displayed():
                    # Get canvas as base64
                    canvas_base64 = self.driver.execute_script(
                        "return arguments[0].toDataURL('image/png').substring(22);", qr
                    )
                    return canvas_base64
            except:
                pass
            finally:
                self.driver.implicitly_wait(5)
            
            return None

    def get_debug_info(self):
        """Get debug information about current state"""
        with self.driver_lock:
            if not self.driver:
                return {"error": "Driver not initialized"}
            
            try:
                info = {
                    "url": self.driver.current_url,
                    "title": self.driver.title,
                    "status": "Authenticated" if self.is_authenticated else "Not authenticated"
                }
                
                # Try to get screenshot
                try:
                    screenshot = self.driver.get_screenshot_as_base64()
                    info["screenshot"] = screenshot
                except:
                    info["screenshot"] = None
                
                return info
            except Exception as e:
                return {"error": str(e)}

    def send_message(self, phone_number, message):
        """Send a WhatsApp message"""
        with self.driver_lock:
            if not self._check_login_status():
                return {"error": "Not authenticated"}

            try:
                p_str = str(phone_number).strip()
                if not p_str:
                    return {"error": "Invalid recipient"}

                print(f"Sending message to: {p_str}")

                # Navigate using direct URL for numeric phone, else use search
                clean_num = "".join(filter(str.isdigit, p_str))
                if clean_num and len(clean_num) >= 7:
                    print(f"Using direct URL for phone number: {clean_num}")
                    self.driver.get(f"https://web.whatsapp.com/send?phone={clean_num}")
                    time.sleep(3)  # Wait for chat to load
                else:
                    print(f"Using search for name: {p_str}")
                    chat_opened = False

                    # Ensure the sidebar is visible before clicking.
                    # IMPORTANT: avoid a full page reload (driver.get) unless necessary —
                    # a reload puts WhatsApp Web back into early initialization where the
                    # app is not yet interactive (no contenteditable, clicks ignored).
                    try:
                        current_url = self.driver.current_url
                        sidebar_present = bool(self.driver.find_elements(
                            By.XPATH, "//div[@id='pane-side']"))
                        if not sidebar_present or 'web.whatsapp.com' not in current_url:
                            # Need a fresh load — give the app time to fully initialize
                            self.driver.get("https://web.whatsapp.com")
                            WebDriverWait(self.driver, 20).until(
                                EC.presence_of_element_located((By.XPATH, "//div[@id='pane-side']"))
                            )
                            # Wait for the app to become interactive: at least one
                            # contenteditable must appear anywhere on the page
                            try:
                                WebDriverWait(self.driver, 15).until(
                                    EC.presence_of_element_located(
                                        (By.XPATH, "//*[@contenteditable][@contenteditable!='false']"))
                                )
                                print("App fully initialized after reload (contenteditable present)")
                            except Exception:
                                time.sleep(5)  # last-resort wait
                        else:
                            print("Sidebar already visible — skipping page reload")
                        # Wait for the contact's span to be present in the sidebar
                        try:
                            WebDriverWait(self.driver, 8).until(
                                EC.presence_of_element_located(
                                    (By.XPATH, f"//span[@title='{p_str}']"))
                            )
                        except Exception:
                            time.sleep(2)
                        print(f"Sidebar ready for searching {p_str}")
                    except Exception as e:
                        print(f"Warning: sidebar preparation failed: {e}")

                    # Strategy 1: Use Selenium's native WebDriver click via ActionChains.
                    # JS event dispatch (element.click / dispatchEvent) does NOT open
                    # the chat in headless Firefox — the browser's coordinate-based click
                    # from Selenium is required.
                    compose_xpath = (
                        "//*[@contenteditable]"
                        "[not(ancestor::*[@id='side'])]"
                        "[not(ancestor::*[@id='pane-side'])]"
                        "[@contenteditable!='false']"
                    )
                    try:
                        span_el = self.driver.find_element(
                            By.XPATH, f"//span[@title='{p_str}']")
                        # Scroll element to the centre of the viewport
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center',inline:'center'});",
                            span_el)
                        time.sleep(0.4)
                        # ActionChains move + click uses real browser coordinates
                        ActionChains(self.driver).move_to_element(span_el).click().perform()
                        print(f"ActionChains click dispatched on span for {p_str}")
                        # Wait up to 10s for a compose box to appear outside the sidebar
                        try:
                            WebDriverWait(self.driver, 10).until(
                                EC.presence_of_element_located((By.XPATH, compose_xpath))
                            )
                            print("Compose box detected — chat opened")
                            chat_opened = True
                        except Exception:
                            # Compose box didn't appear; fall through to Strategy 2
                            print("Compose box not detected after ActionChains click — trying Selenium direct click")
                            try:
                                span_el.click()
                                WebDriverWait(self.driver, 8).until(
                                    EC.presence_of_element_located((By.XPATH, compose_xpath))
                                )
                                print("Compose box detected after direct Selenium click")
                                chat_opened = True
                            except Exception:
                                print("Direct Selenium click also failed to open chat")
                    except Exception as e:
                        print(f"Strategy 1 Selenium click failed: {e}")

                    # Strategy 2: Use the search box to find and open the chat.
                    if not chat_opened:
                        try:
                            # Try to find an already-visible search input, or click the
                            # search icon to open one.
                            search_box = self.driver.execute_script("""
                                // Try existing visible search inputs first
                                var selectors = [
                                    '[data-testid="chat-list-search"]',
                                    '[role="searchbox"]',
                                    '#side [contenteditable="true"]',
                                ];
                                for (var i = 0; i < selectors.length; i++) {
                                    var el = document.querySelector(selectors[i]);
                                    if (el && el.offsetParent !== null) return el;
                                }
                                // Try clicking search icon to reveal it
                                var iconSelectors = [
                                    '[aria-label*="Search"]',
                                    '[title*="Search"]',
                                    '#side button',
                                ];
                                for (var j = 0; j < iconSelectors.length; j++) {
                                    var icon = document.querySelector(iconSelectors[j]);
                                    if (icon) { icon.click(); break; }
                                }
                                return null;
                            """)

                            # If no box yet, poll for up to 3s for one to appear
                            if not search_box:
                                for _ in range(6):
                                    time.sleep(0.5)
                                    search_box = self.driver.execute_script("""
                                        var side = document.querySelector('#side');
                                        if (side) {
                                            var sb = side.querySelector('[contenteditable="true"]');
                                            if (sb && sb.offsetParent !== null) return sb;
                                            var rb = side.querySelector('[role="searchbox"]');
                                            if (rb && rb.offsetParent !== null) return rb;
                                        }
                                        var boxes = Array.from(document.querySelectorAll('[contenteditable="true"]'));
                                        return boxes.find(function(b) { return b.offsetParent !== null; }) || null;
                                    """)
                                    if search_box:
                                        break

                            if search_box:
                                self.driver.execute_script("arguments[0].click(); arguments[0].focus();", search_box)
                                time.sleep(0.3)
                                self.driver.execute_script("arguments[0].innerHTML = '';", search_box)
                                search_box.send_keys(p_str)
                                time.sleep(2)
                                print(f"Typed '{p_str}' in search box")

                                # Click matching result in sidebar
                                result_selectors = [
                                    f"//span[@title='{p_str}']",
                                    "//div[@id='pane-side']//*[@role='row'][1]",
                                    "//div[@id='pane-side']//div[@role='listitem'][1]",
                                    "//*[@role='row'][1]",
                                ]
                                for res_sel in result_selectors:
                                    try:
                                        result = self.driver.find_element(By.XPATH, res_sel)
                                        if result.is_displayed():
                                            result.click()
                                            chat_opened = True
                                            print(f"Clicked search result for {p_str}")
                                            break
                                    except:
                                        continue

                                if not chat_opened:
                                    search_box.send_keys(Keys.ENTER)
                                    chat_opened = True
                                    print(f"Pressed Enter to open chat for {p_str}")

                                time.sleep(3)
                            else:
                                print("Strategy 2: no search box found after waiting")

                        except Exception as e:
                            print(f"Strategy 2 (search) failed: {e}")

                    if not chat_opened:
                        return {"error": f"Could not open chat for '{p_str}': sidebar click and search both failed"}

                # Find the message input box using the same broad selector as the
                # WebDriverWait above. Matches contenteditable="" and "true" (not "false"),
                # any tag, excludes sidebar.
                print("Locating message input box...")
                input_xpath = (
                    "//*[@contenteditable]"
                    "[not(ancestor::*[@id='side'])]"
                    "[not(ancestor::*[@id='pane-side'])]"
                    "[@contenteditable!='false']"
                )
                msg_input = None
                try:
                    msg_input = self.driver.find_element(By.XPATH, input_xpath)
                    print(f"Found message input box via XPath (tag={msg_input.tag_name})")
                except Exception:
                    pass

                # JS fallback — matches ANY truthy contenteditable outside the sidebar
                if not msg_input:
                    msg_input = self.driver.execute_script("""
                        var side = document.querySelector('#side');
                        var paneSide = document.querySelector('#pane-side');
                        // querySelectorAll('[contenteditable]') matches all contenteditable elements
                        var all = Array.from(document.querySelectorAll('[contenteditable]'));
                        // Return the last truthy contenteditable that is not inside the sidebar
                        var outside = all.filter(function(el) {
                            var ce = el.getAttribute('contenteditable');
                            return ce !== 'false'
                                && (!side || !side.contains(el))
                                && (!paneSide || !paneSide.contains(el));
                        });
                        if (outside.length > 0) return outside[outside.length - 1];
                        // Last resort: dump all contenteditable info for debugging
                        return null;
                    """)
                    if msg_input:
                        print("Found message input box via JS fallback")

                if not msg_input:
                    # Log what contenteditable elements exist to aid debugging
                    try:
                        ce_info = self.driver.execute_script("""
                            return Array.from(document.querySelectorAll('[contenteditable]'))
                                .map(function(el) {
                                    return el.tagName + '[ce=' + el.getAttribute('contenteditable') + ']'
                                           + ' id=' + (el.id || '') + ' class=' + (el.className || '').slice(0,40);
                                });
                        """)
                        print(f"DEBUG contenteditable elements in DOM: {ce_info}")
                    except Exception:
                        pass
                    return {"error": "Could not find message input box"}

                # Focus and clear the input, then type via JS + send_keys for newlines
                print(f"Typing message ({len(message)} chars): {message}")
                self.driver.execute_script("arguments[0].focus();", msg_input)
                time.sleep(0.2)

                # Use clipboard-paste approach for speed and reliability
                # (ActionChains character-by-character is extremely slow for long messages)
                try:
                    # Insert text via JS input event simulation
                    escaped = message.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')
                    self.driver.execute_script("""
                        var el = arguments[0];
                        var text = arguments[1];
                        el.focus();
                        // Use execCommand for broad compatibility
                        document.execCommand('insertText', false, text);
                    """, msg_input, message)
                    time.sleep(0.5)
                    # Verify something was typed; fall back to send_keys if not
                    typed = self.driver.execute_script(
                        "return arguments[0].innerText || arguments[0].textContent || '';",
                        msg_input)
                    if not typed.strip():
                        raise ValueError("execCommand produced no text")
                    print("Typed via execCommand")
                except Exception as type_err:
                    print(f"execCommand failed ({type_err}), falling back to send_keys")
                    self.driver.execute_script(
                        "arguments[0].innerHTML = ''; arguments[0].focus();", msg_input)
                    time.sleep(0.2)
                    for char in message:
                        if char == '\n':
                            msg_input.send_keys(Keys.SHIFT, Keys.ENTER)
                        else:
                            msg_input.send_keys(char)
                time.sleep(0.5)

                # Send via button click or Enter key
                sent = False
                send_selectors = [
                    "//button[@data-testid='compose-btn-send']",
                    "//span[@data-testid='send']",
                    "//button[@aria-label='Send']",
                    "//button[@data-testid='send']",
                ]
                for sel in send_selectors:
                    try:
                        btn = self.driver.find_element(By.XPATH, sel)
                        if btn.is_displayed() and btn.is_enabled():
                            btn.click()
                            sent = True
                            print(f"Clicked send button: {sel}")
                            break
                    except:
                        continue

                if not sent:
                    print("Send button not found, pressing Enter")
                    msg_input.send_keys(Keys.RETURN)

                # Track this sent message to avoid detecting it as incoming
                # Normalize phone number to digits only for consistent keying
                contact_key = "".join(filter(str.isdigit, p_str)) or p_str
                if contact_key not in self.recently_sent:
                    self.recently_sent[contact_key] = []
                self.recently_sent[contact_key].append({
                    "message": message,
                    "timestamp": time.time()
                })
                # Keep only recent messages (last 10 per contact, max 5 minutes old)
                self.recently_sent[contact_key] = [
                    m for m in self.recently_sent[contact_key]
                    if time.time() - m["timestamp"] < 300  # 5 minutes
                ][:10]

                # Try to get the contact name from the chat header to also track under name
                try:
                    header_selectors = [
                        "//header//span[@title]",
                        "//header//span[@dir='auto']",
                        "//div[@data-testid='conversation-info-header-chat-title']",
                        "//header//h1"
                    ]
                    for sel in header_selectors:
                        try:
                            name_elements = self.driver.find_elements(By.XPATH, sel)
                            for name_element in name_elements:
                                chat_name = name_element.text.strip()
                                # Skip if it's status text like "last seen" or "online"
                                if (chat_name and chat_name != p_str and
                                    not any(skip in chat_name.lower() for skip in ['last seen', 'online', 'typing']) and
                                    len(chat_name) < 50):  # Reasonable name length
                                    # Also store under the name
                                    if chat_name not in self.recently_sent:
                                        self.recently_sent[chat_name] = []
                                    self.recently_sent[chat_name].append({
                                        "message": message,
                                        "timestamp": time.time()
                                    })
                                    # Map phone to name for future lookups
                                    self.phone_to_name[contact_key] = chat_name
                                    print(f"Also tracked sent message under name: {chat_name}")
                                    break
                            else:
                                continue
                            break
                        except:
                            pass
                except:
                    pass

                print(f"Message sent to {p_str}")
                time.sleep(2)
                return {"success": True}

            except Exception as e:
                print(f"Send error: {e}")
                import traceback
                traceback.print_exc()
                return {"error": str(e)}

    def get_new_messages(self):
        """
        Poll for new messages by reading sidebar previews.
        Do not rely on unread badges (they may not render in headless mode).
        Treat any preview text change (that doesn't start with "You:") as new.
        """
        with self.driver_lock:
            if not self._check_login_status():
                return []

            try:
                self.driver.implicitly_wait(0)
                # Wait up to 5s for the chat list to render
                try:
                    WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((By.XPATH, "//div[@id='pane-side']"))
                    )
                except Exception:
                    pass
                new_msgs = []

                # Try multiple selectors for the chat list
                chat_rows = []
                selectors = [
                    "//div[@id='pane-side']//*[@role='row']",
                    "//div[@id='pane-side']//div[@role='listitem']",
                    "//*[@role='row']",
                    "//div[@role='listitem']",
                    "//div[@data-testid='cell-frame-container']"
                ]

                matched_selector = None
                for sel in selectors:
                    rows = self.driver.find_elements(By.XPATH, sel)
                    if rows:
                        chat_rows = rows
                        matched_selector = sel
                        break

                # Initialize last_messages with current sidebar state on first scan
                if not self.last_messages_initialized and chat_rows:
                    for chat_row in chat_rows[:30]:
                        try:
                            contact_name = "Unknown"
                            try:
                                name_el = chat_row.find_element(By.XPATH, ".//span[@title]")
                                contact_name = name_el.get_attribute("title") or name_el.text
                            except:
                                try:
                                    name_el = chat_row.find_element(By.XPATH, ".//span[@dir='auto']")
                                    contact_name = name_el.text
                                except:
                                    pass

                            if not contact_name or contact_name == "Unknown":
                                continue

                            message_text = ""
                            try:
                                msg_spans = chat_row.find_elements(By.XPATH, ".//span[@dir='ltr'] | .//span[@dir='auto']")
                                for span in msg_spans:
                                    text = span.text.strip()
                                    if text and text != contact_name and len(text) > 1:
                                        message_text = text
                                        break
                            except:
                                pass

                            if contact_name and message_text:
                                self.last_messages[contact_name] = message_text
                        except:
                            pass
                    self.last_messages_initialized = True
                    print("Initialized last_messages with current sidebar state")
                    return []  # Don't process messages on first scan

                total_rows = len(chat_rows)
                if total_rows == 0:
                    # Quiet mode - don't spam logs with routine polling
                    pass
                    try:
                        snippet = (self.driver.page_source or "")[:2000]
                        ready_state = self.driver.execute_script("return document.readyState")
                        current_url = self.driver.current_url
                        title = self.driver.title
                        print(f"DOM snapshot (first 2000 chars): {snippet}")
                        print(f"Diagnostics: readyState={ready_state}, url={current_url}, title={title}")
                        js_counts = self.driver.execute_script(
                            "return {"
                            "listitems: document.querySelectorAll('[role=listitem]').length, "
                            "rows: document.querySelectorAll('[role=row]').length, "
                            "treeitems: document.querySelectorAll('[role=treeitem]').length, "
                            "grids: document.querySelectorAll('[role=grid]').length, "
                            "articles: document.querySelectorAll('article').length, "
                            "paneSide: document.querySelectorAll('#pane-side').length, "
                            "side: document.querySelectorAll('#side').length, "
                            "iframes: document.querySelectorAll('iframe').length, "
                            "chatCells: document.querySelectorAll('[data-testid*=chat], [data-testid*=cell]').length"
                            "};"
                        )
                        print(f"JS counts: {js_counts}")
                        try:
                            pane_children = self.driver.execute_script(
                                "const el=document.querySelector('#pane-side');"
                                "if(!el) return [];"
                                "return Array.from(el.children).map(c=>c.tagName+':'+(c.getAttribute('role')||'')+':'+(c.getAttribute('data-testid')||''));"
                            )
                            print(f"pane-side children: {pane_children}")
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # JS fallback for dynamic DOM
                    try:
                        js_rows = self.driver.execute_script(
                            "return Array.from(document.querySelectorAll('#pane-side [role=row], #pane-side [role=listitem], [role=row], [role=listitem]')).slice(0, 30).map(el => {"
                            "  const nameEl = el.querySelector('span[title]') || el.querySelector('span[dir=auto]');"
                            "  const name = nameEl ? nameEl.textContent.trim() : '';"
                            "  const spans = Array.from(el.querySelectorAll('span[dir=ltr], span[dir=auto]'));"
                            "  let msg = '';"
                            "  for (const s of spans) {"
                            "    const t = (s.textContent || '').trim();"
                            "    if (t && t !== name && t.length > 1) { msg = t; break; }"
                            "  }"
                            "  const html = el.innerHTML || '';"
                            "  const emojis = ['📷','🖼','🎥','📹','🎞'];"
                            "  const mediaIcon = emojis.some(e => html.includes(e) && !msg.includes(e))"
                            "    || html.includes('data-testid=\"media') || html.includes('image-message');"
                            "  return {name, msg, dataId: el.getAttribute('data-id') || '', mediaIcon: mediaIcon};"
                            "});"
                        )
                        if js_rows:
                            print(f"JS fallback rows: {len(js_rows)}")
                            for row in js_rows:
                                name = row.get("name") or ""
                                msg = row.get("msg") or ""
                                if not name or not msg:
                                    continue
                                if msg.lower().startswith("you:"):
                                    continue
                                last_seen = self.last_messages.get(name)
                                if last_seen != msg:
                                    self.last_messages[name] = msg
                                    phone_id = row.get("dataId", "")
                                    if "@" in phone_id:
                                        phone_id = phone_id.split("@")[0]
                                    elif not phone_id:
                                        phone_id = name

                                    # Check if this is a message we recently sent (avoid echo)
                                    is_our_message = False
                                    normalized_phone = "".join(filter(str.isdigit, phone_id)) if phone_id else ""
                                    for contact_key in [phone_id, normalized_phone, name, self.phone_to_name.get(normalized_phone), self.phone_to_name.get(phone_id)]:
                                        if contact_key and contact_key in self.recently_sent:
                                            for sent_msg in self.recently_sent[contact_key]:
                                                sent_text = sent_msg["message"]
                                                import re
                                                def clean_text(text):
                                                    return re.sub(r'[^\w\s]', '', text).lower().strip()
                                                sent_clean = clean_text(sent_text)
                                                msg_clean = clean_text(msg)
                                                if (msg_clean.startswith(sent_clean[:30]) or
                                                    sent_clean.startswith(msg_clean[:30]) or
                                                    msg_clean in sent_clean or
                                                    sent_clean in msg_clean or
                                                    all(word in sent_clean for word in msg_clean.split()[:3])):
                                                    if time.time() - sent_msg["timestamp"] < 120:
                                                        is_our_message = True
                                                        print(f"Skipping our own message (JS) from {name}: {msg}")
                                                        break
                                            if is_our_message:
                                                break

                                    if not is_our_message:
                                        entry = {"from": phone_id, "name": name, "message": msg}
                                        if row.get("mediaIcon"):
                                            entry["maybe_media"] = True
                                        new_msgs.append(entry)
                                        print(f"NEW MESSAGE (JS) from {name}: {msg}")
                            print(f"JS fallback result: {len(new_msgs)} new messages")
                            self._process_image_messages(new_msgs)
                            return new_msgs
                        js_debug = self.driver.execute_script(
                            "return Array.from(document.querySelectorAll('#pane-side [role=row], #pane-side [role=listitem], [role=row], [role=listitem]')).slice(0, 8).map(el => ({"
                            "  tag: el.tagName,"
                            "  role: el.getAttribute('role') || '',"
                            "  testid: el.getAttribute('data-testid') || '',"
                            "  aria: el.getAttribute('aria-label') || '',"
                            "  cls: (el.getAttribute('class') || '').slice(0,120),"
                            "  text: (el.textContent || '').trim().slice(0,120)"
                            "}));"
                        )
                        if js_debug:
                            print(f"JS row samples: {js_debug}")
                    except Exception:
                        pass
                    return []

                # Quiet mode - only log if there are new messages or errors
                if len(new_msgs) > 0:
                    print(f"Sidebar scan: {total_rows} chat rows (selector: {matched_selector})")

                for chat_row in chat_rows[:30]:
                    try:
                        # Extract contact name
                        contact_name = "Unknown"
                        try:
                            name_el = chat_row.find_element(By.XPATH, ".//span[@title]")
                            contact_name = name_el.get_attribute("title") or name_el.text
                        except:
                            try:
                                name_el = chat_row.find_element(By.XPATH, ".//span[@dir='auto']")
                                contact_name = name_el.text
                            except:
                                pass

                        if not contact_name or contact_name == "Unknown":
                            continue

                        # Extract message preview
                        message_text = ""
                        try:
                            msg_spans = chat_row.find_elements(By.XPATH, ".//span[@dir='ltr'] | .//span[@dir='auto']")
                            for span in msg_spans:
                                text = span.text.strip()
                                if text and text != contact_name and len(text) > 1:
                                    message_text = text
                                    break
                        except:
                            pass

                        if not message_text:
                            continue

                        # Skip if preview is from us
                        if message_text.lower().startswith("you:"):
                            continue

                        # Skip system/self chats
                        if contact_name.lower() in {"whatsapp", "you"}:
                            continue
                        if message_text.strip().lower() in {"(you)", "you"}:
                            continue

                        # Check if this is a new message we haven't seen
                        last_seen = self.last_messages.get(contact_name)
                        if last_seen != message_text:
                            self.last_messages[contact_name] = message_text

                            # Extract phone number if possible
                            phone_id = contact_name
                            try:
                                data_id = chat_row.get_attribute("data-id")
                                print(f"Contact {contact_name}: data-id='{data_id}'")
                                if data_id and "@" in data_id:
                                    phone_id = data_id.split("@")[0]
                                    print(f"Extracted phone: {phone_id}")
                                else:
                                    print(f"No valid data-id for {contact_name}, using name")
                            except Exception as e:
                                print(f"Error getting data-id for {contact_name}: {e}")

                            # Check if this is a message we recently sent (avoid echo)
                            is_our_message = False
                            # Normalize phone_id to digits for matching
                            normalized_phone = "".join(filter(str.isdigit, phone_id)) if phone_id else ""
                            for contact_key in [phone_id, normalized_phone, contact_name, self.phone_to_name.get(normalized_phone), self.phone_to_name.get(phone_id)]:
                                if contact_key and contact_key in self.recently_sent:
                                    for sent_msg in self.recently_sent[contact_key]:
                                        # Check if message text matches (with tolerance for preview truncation and emojis)
                                        sent_text = sent_msg["message"]
                                        # Clean text for better matching (remove emojis, punctuation, lowercase)
                                        import re
                                        def clean_text(text):
                                            return re.sub(r'[^\w\s]', '', text).lower().strip()
                                        sent_clean = clean_text(sent_text)
                                        msg_clean = clean_text(message_text)
                                        if (msg_clean.startswith(sent_clean[:30]) or
                                            sent_clean.startswith(msg_clean[:30]) or
                                            msg_clean in sent_clean or
                                            sent_clean in msg_clean or
                                            all(word in sent_clean for word in msg_clean.split()[:3])):  # First 3 words match
                                            # Also check timestamp (within last 120 seconds)
                                            if time.time() - sent_msg["timestamp"] < 120:
                                                is_our_message = True
                                                print(f"Skipping our own message from {contact_name}: {message_text}")
                                                break
                                        if is_our_message:
                                            break

                            if is_our_message:
                                continue

                            msg_entry = {
                                "from": phone_id,  # Use phone number as primary identifier
                                "name": contact_name,  # Keep name for display
                                "message": message_text
                            }
                            # Media-with-caption messages show only the caption
                            # in the preview text — check the row HTML for a
                            # media indicator so they still get downloaded.
                            try:
                                row_html = chat_row.get_attribute('outerHTML') or ''
                                if _row_suggests_media(row_html, message_text):
                                    msg_entry["maybe_media"] = True
                            except Exception:
                                pass
                            new_msgs.append(msg_entry)
                            print(f"NEW MESSAGE from {contact_name} ({phone_id}): {message_text}")
                    except Exception:
                        pass

                # Only log scan results when there are actually new messages
                if len(new_msgs) > 0:
                    print(f"Sidebar scan result: {len(new_msgs)} new messages")
                self._process_image_messages(new_msgs)
                return new_msgs

            except Exception as e:
                print(f"Polling error: {e}")
                import traceback
                traceback.print_exc()
                return []
            finally:
                self.driver.implicitly_wait(5)

    # ── Image send / receive ─────────────────────────────────────────────────

    def send_image(self, phone_number, image_path, caption=""):
        """
        Send an image (or video file) to a WhatsApp contact via phone number.

        phone_number : recipient in international format (digits accepted).
        image_path   : local filesystem path to the image / video file.
        caption      : optional text caption (may be empty string).
        """
        with self.driver_lock:
            if not self._check_login_status():
                return {"error": "Not authenticated"}

            try:
                p_str = str(phone_number).strip()
                clean_num = "".join(filter(str.isdigit, p_str))
                if not clean_num:
                    return {"error": "send_image requires a phone number (digits)"}

                abs_path = os.path.abspath(image_path)
                if not os.path.exists(abs_path):
                    return {"error": f"Image file not found: {abs_path}"}

                print(f"[image] Sending image to {clean_num}: {abs_path}")

                # ── Optional: downscale very large images ─────────────────
                # WhatsApp Web's preview renderer stalls on very large images
                # in headless Firefox, and the upload then never produces a
                # preview. Cap at 1600px on the longest side; keep the file on
                # disk untouched, write to a temp file when we resize.
                try:
                    from PIL import Image as _PILImage
                    with _PILImage.open(abs_path) as _im:
                        _w, _h = _im.size
                    _max_side = 1600
                    if max(_w, _h) > _max_side:
                        _ratio = _max_side / float(max(_w, _h))
                        _new = (int(_w * _ratio), int(_h * _ratio))
                        _resized = _PILImage.open(abs_path).convert("RGB").resize(
                            _new, _PILImage.LANCZOS)
                        _tmp_rs = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                        _resized.save(_tmp_rs, "JPEG", quality=90)
                        _tmp_rs.close()
                        abs_path = _tmp_rs.name
                        print(f"[image] Downscaled {_w}x{_h} -> {_new} for upload: {abs_path}")
                except Exception as e_rs:
                    print(f"[image] Resize check skipped: {e_rs}")

                # Open chat via direct URL (most reliable)
                self.driver.get(f"https://web.whatsapp.com/send?phone={clean_num}")
                try:
                    WebDriverWait(self.driver, 25).until(
                        EC.presence_of_element_located((By.XPATH, "//div[@id='main']"))
                    )
                except TimeoutException:
                    pass
                time.sleep(3)

                def _media_row_count():
                    """Number of message rows in the open chat that contain an
                    image/video. Class names and data-testids are obfuscated in
                    current WhatsApp Web, so counting media rows before vs.
                    after sending is the only version-proof delivery check."""
                    try:
                        return int(self.driver.execute_script("""
                            var main = document.querySelector('#main');
                            if (!main) return 0;
                            var rows = main.querySelectorAll('[role="row"]');
                            var n = 0;
                            for (var i = 0; i < rows.length; i++) {
                                if (rows[i].querySelector('img[src^="blob:"], video, img[src^="data:"]')) n++;
                            }
                            return n;
                        """) or 0)
                    except Exception:
                        return 0

                media_rows_before = _media_row_count()
                print(f"[image] Media rows in chat before send: {media_rows_before}")

                # ── Helpers ──────────────────────────────────────────────────
                def _preview_state():
                    """'media' = the media preview/editor is open, None = not.

                    IMPORTANT (current WhatsApp Web, 2026): the media editor
                    is rendered WITHOUT role="dialog" and WITHOUT any
                    data-testid attribute, so every legacy selector missed it
                    and send_image kept "failing" while the attachment was in
                    fact staged.  The reliable signature is the editor's
                    toolbar/action aria-labels:
                        "Send 1 selected", "Remove attachment", "Add file",
                        "Crop and rotate", "Turn on view once"
                    We detect those instead."""
                    return self.driver.execute_script("""
                        function visible(el) {
                            if (!el) return false;
                            var r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 &&
                                   getComputedStyle(el).visibility !== 'hidden' &&
                                   el.offsetParent !== null;
                        }
                        var labels = Array.from(document.querySelectorAll('[aria-label]'))
                            .filter(visible)
                            .map(function(e){ return e.getAttribute('aria-label') || ''; });
                        var isMedia = labels.some(function(l) {
                            return /^Send\\s+\\d+\\s+selected/i.test(l)
                                || /^Remove attachment$/i.test(l)
                                || /^Add file$/i.test(l)
                                || /^Crop and rotate$/i.test(l)
                                || /^Turn on view once$/i.test(l)
                                || /^Enviar\\s+\\d+/i.test(l);
                        });
                        if (isMedia) return 'media';
                        // Legacy fallbacks (older WhatsApp builds)
                        var cap = document.querySelector(
                            '[data-testid="media-caption-input"],'
                            + ' div[aria-label="Add a caption"]');
                        if (visible(cap)) return 'media';
                        var viewer = document.querySelector(
                            '[data-testid="media-viewer"], div[class*="media-viewer"],'
                            + ' div[role="dialog"] img[src^="blob:"]');
                        if (visible(viewer)) return 'media';
                        return null;
                    """)

                def _dump_dom_state(tag):
                    """Diagnostic: what did WhatsApp actually render?
                    Current WhatsApp Web has REMOVED data-testid attributes,
                    so this dump is the only way to find working selectors."""
                    try:
                        info = self.driver.execute_script("""
                            function vis(el) {
                                if (!el) return false;
                                var r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 && el.offsetParent !== null;
                            }
                            var dialogs = Array.from(document.querySelectorAll(
                                'div[role="dialog"], div[role="application"], [data-animate-modal-body]'));
                            var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                            var main = document.querySelector('#main');
                            return {
                                dialogs: dialogs.length,
                                dialogsVisible: dialogs.filter(vis).length,
                                dialogHTML: dialogs.length
                                    ? dialogs[dialogs.length-1].outerHTML.slice(0, 500) : '',
                                blobImgs: document.querySelectorAll('img[src^="blob:"]').length,
                                canvases: document.querySelectorAll('canvas').length,
                                fileInputs: inputs.map(function(i){
                                    return {accept: i.accept || '(none)', files: i.files ? i.files.length : -1};
                                }),
                                mainEditables: main ? main.querySelectorAll('[contenteditable="true"]').length : -1,
                                ariaButtons: Array.from(document.querySelectorAll('[aria-label]'))
                                    .filter(vis)
                                    .map(function(e){return e.getAttribute('aria-label');})
                                    .slice(0, 40)
                            };
                        """)
                        print(f"[image][dom:{tag}] {json.dumps(info)[:1500]}")
                    except Exception as e_d:
                        print(f"[image][dom:{tag}] dump failed: {e_d}")

                def _find_file_inputs(prefer_document=False):
                    """Return scored candidates as [{el, accept, s}].
                    prefer_document=True ranks '*/*'/'file' (document attach)
                    inputs first — used by the document-attach fallback."""
                    return self.driver.execute_script("""
                        var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                        var preferDoc = arguments[0];
                        function inSticker(el) {
                            var p = el;
                            while (p) {
                                var c = (p.className || '').toString().toLowerCase();
                                var t = (p.getAttribute('data-testid') || '').toString().toLowerCase();
                                var a = (p.getAttribute('aria-label') || '').toString().toLowerCase();
                                if (c.indexOf('sticker') >= 0 || t.indexOf('sticker') >= 0 || a.indexOf('sticker') >= 0) return true;
                                p = p.parentElement;
                            }
                            return false;
                        }
                        return inputs.map(function(i) {
                            var a = (i.accept || '').toLowerCase();
                            var s = 0;
                            var hasImg = a.indexOf('image') >= 0;
                            var hasVid = a.indexOf('video') >= 0;
                            if (preferDoc) {
                                // Document attach: input accepts anything, no image/video filter
                                if (!hasImg && !hasVid) s += 200;
                                else s += 5;
                            } else {
                                if (hasImg && hasVid) s += 100;
                                else if (hasImg || hasVid) s += 40;
                                else s += 2;
                            }
                            if (inSticker(i)) s -= 500;
                            return {el: i, accept: i.accept || '', s: s};
                        }).filter(function(x){ return x.s > 0; })
                          .sort(function(a,b){ return b.s - a.s; });
                    """, prefer_document)

                def _open_attach_media_menu():
                    """Click attach/clip, then Photos & Videos. Return success."""
                    attach_selectors = [
                        "//button[@data-testid='clip']",
                        "//span[@data-testid='clip']",
                        "//*[@data-icon='clip']",
                        "//button[@aria-label='Attach']",
                        "//*[@data-testid='attach']",
                        "//div[@id='main']//button[.//*[contains(@data-icon,'clip')]]",
                    ]
                    clicked = False
                    for xp in attach_selectors:
                        try:
                            btn = self.driver.find_element(By.XPATH, xp)
                            if btn.is_displayed():
                                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                                time.sleep(0.2)
                                ActionChains(self.driver).move_to_element(btn).click().perform()
                                print(f"[image] Clicked attach button: {xp}")
                                clicked = True
                                time.sleep(0.6)
                                break
                        except Exception:
                            continue
                    if not clicked:
                        return False

                    # Current WhatsApp Web has REMOVED data-testid attributes
                    # and the menu items carry no useful aria-label, so match
                    # on VISIBLE TEXT instead ("Photos & videos" / "Fotos y
                    # videos"). Log the menu contents so future DOM changes are
                    # diagnosable in one shot.
                    try:
                        menu_items = self.driver.execute_script("""
                            function vis(el) {
                                var r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 && el.offsetParent !== null;
                            }
                            return Array.from(document.querySelectorAll(
                                    'li, [role="menuitem"], [role="button"], button'))
                                .filter(vis)
                                .map(function(e){ return (e.innerText || '').trim().slice(0, 40); })
                                .filter(function(t){ return t.length > 0 && t.length < 40; })
                                .slice(0, 30);
                        """)
                        print(f"[image] Attach menu items visible: {menu_items}")
                    except Exception:
                        pass

                    # Click the "Photos & videos" entry by text, then return.
                    clicked_media = self.driver.execute_script("""
                        function vis(el) {
                            var r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && el.offsetParent !== null;
                        }
                        var re = /photos?\\s*(&|and)?\\s*videos?|fotos\\s*y\\s*v[ií]deos/i;
                        var nodes = Array.from(document.querySelectorAll(
                            'li, [role="menuitem"], [role="button"], button, span, div'));
                        for (var i = 0; i < nodes.length; i++) {
                            var n = nodes[i];
                            var txt = (n.innerText || '').trim();
                            if (!txt || txt.length > 40 || !re.test(txt)) continue;
                            if (!vis(n)) continue;
                            // Prefer the closest clickable ancestor
                            var target = n.closest('li, [role="menuitem"], [role="button"], button') || n;
                            target.click();
                            return txt;
                        }
                        return null;
                    """)
                    if clicked_media:
                        print(f"[image] Clicked media menu item by text: {clicked_media!r}")
                        time.sleep(0.8)
                        return True

                    # Legacy selectors kept as a last resort.
                    for xp in ("//*[@data-testid='mi-attach-media']",
                               "//*[@data-testid='attach-image']",
                               "//*[contains(@aria-label,'Photos') or contains(@aria-label,'Fotos')]"):
                        try:
                            item = self.driver.find_element(By.XPATH, xp)
                            if item.is_displayed():
                                ActionChains(self.driver).move_to_element(item).click().perform()
                                print(f"[image] Clicked Photos & Videos menu item: {xp}")
                                time.sleep(0.6)
                                return True
                        except Exception:
                            pass
                    print("[image] No 'Photos & videos' menu entry found")
                    return False

                def _try_input(candidate):
                    """Make the input visible, send the file, and detect preview state."""
                    file_input = candidate['el']
                    idx = candidate.get('idx', '?')
                    accept = candidate.get('accept', '')
                    try:
                        self.driver.execute_script("""
                            arguments[0].style.display='block';
                            arguments[0].style.opacity='1';
                            arguments[0].style.visibility='visible';
                            arguments[0].style.position='fixed';
                            arguments[0].style.top='0';
                            arguments[0].style.left='0';
                            arguments[0].style.zIndex='999999';
                        """, file_input)
                        time.sleep(0.2)
                        file_input.send_keys(abs_path)
                        # Modern WhatsApp Web ignores a bare send_keys() on a
                        # programmatically-shown input — it only observes the
                        # 'change' event. Fire change+input so the preview
                        # dialog actually opens.
                        try:
                            self.driver.execute_script("""
                                var ev = new Event('change', {bubbles: true});
                                arguments[0].dispatchEvent(ev);
                                arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
                            """, file_input)
                        except Exception:
                            pass
                        print(f"[image] File set on input #{idx} (accept={accept!r}): {abs_path}")

                        # Wait up to ~5s for a preview/viewer/sticker dialog to appear
                        state = None
                        for _ in range(15):
                            time.sleep(0.3)
                            state = _preview_state()
                            if state:
                                break

                        if state == 'media':
                            print(f"[image] Input #{idx} opened media preview successfully")
                            return 'media'

                        print(f"[image] Input #{idx} did not produce a recognizable preview (state={state})")
                        _dump_dom_state(f"after-input-{idx}")
                        try:
                            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                            time.sleep(0.3)
                        except Exception:
                            pass
                        return 'unknown'
                    except Exception as e_attach:
                        print(f"[image] Input #{idx} attach failed: {e_attach}")
                        return 'error'

                # ── Attach the image ─────────────────────────────────────────
                # The ONLY working path on current WhatsApp Web:
                #   click Attach → click "Photos & videos" (matched by visible
                #   TEXT, since data-testid attributes are gone) → set the file
                #   on the newly created media input
                #   (accept="image/*,video/mp4,…") → fire a 'change' event.
                #
                # Do NOT use the pre-existing accept="image/*" input: that one
                # is the sticker/profile-photo picker and silently swallows the
                # file without ever opening a preview.
                attached = False
                print("[image] Opening attach -> Photos & videos menu")
                if _open_attach_media_menu():
                    time.sleep(0.8)
                    candidates = _find_file_inputs()
                    print(f"[image] After menu open: found {len(candidates)} candidate file input(s)")
                    for idx, cand in enumerate(candidates):
                        cand['idx'] = idx
                        if _try_input(cand) == 'media':
                            attached = True
                            break

                # Fallback: a media input may already exist from a previous
                # menu interaction in this session.
                if not attached:
                    candidates = _find_file_inputs()
                    print(f"[image] Fallback: {len(candidates)} pre-existing candidate file input(s)")
                    for idx, cand in enumerate(candidates):
                        cand['idx'] = idx
                        if _try_input(cand) == 'media':
                            attached = True
                            break

                if not attached:
                    _dump_dom_state("attach-failed")
                    return {"error": (
                        "Could not attach image: the media preview editor never opened. "
                        "The Attach → 'Photos & videos' menu item may have been renamed "
                        "or the chat did not finish loading. See the [dom:attach-failed] "
                        "log line for the current aria-labels.")}

                # Wait for the media preview to fully render
                time.sleep(1)

                # ── Optional caption ─────────────────────────────────────────
                # In the current editor the caption box is the contenteditable
                # labelled exactly "Type a message" (the chat compose box is
                # "Type a message to <name>", so the exact match is safe).
                if caption:
                    caption_xpaths = [
                        "//*[@aria-label='Type a message'][@contenteditable='true']",
                        "//*[@aria-label='Type a message']//*[@contenteditable='true']",
                        "//*[@aria-label='Añade un comentario'][@contenteditable='true']",
                        "//div[@data-testid='media-caption-input']",
                        "//*[@aria-label='Add a caption']",
                    ]
                    for xp in caption_xpaths:
                        try:
                            cap_el = self.driver.find_element(By.XPATH, xp)
                            if cap_el.is_displayed():
                                self.driver.execute_script("arguments[0].focus();", cap_el)
                                time.sleep(0.2)
                                try:
                                    self.driver.execute_script(
                                        "document.execCommand('insertText', false, arguments[0]);",
                                        caption)
                                except Exception:
                                    cap_el.send_keys(caption)
                                print(f"[image] Caption typed ({len(caption)} chars): {caption}")
                                time.sleep(0.3)
                                break
                        except Exception:
                            pass

                # ── Send ─────────────────────────────────────────────────────
                # The editor's send control is aria-label="Send 1 selected".
                #
                # IMPORTANT: a JavaScript .click() is NOT enough here. In
                # headless Firefox, React's synthetic event system ignores
                # programmatic clicks on this button (exactly like the sidebar
                # chat rows, which needed ActionChains too) — the editor stays
                # open and nothing is ever delivered, while the code happily
                # reported "sent". We therefore use a REAL Selenium
                # coordinate-based click and then confirm the editor closed.
                def _editor_open():
                    return _preview_state() == 'media'

                sent = False

                def _confirm_editor_closed(timeout=12):
                    for _ in range(int(timeout / 0.5)):
                        time.sleep(0.5)
                        if not _editor_open():
                            return True
                    return False

                send_xpaths = [
                    "//*[starts-with(@aria-label,'Send') and contains(@aria-label,'selected')]",
                    "//*[starts-with(@aria-label,'Enviar')]",
                    "//div[@role='button'][starts-with(@aria-label,'Send')]",
                    "//button[@aria-label='Send']",
                    "//*[@data-icon='send']",
                ]
                for xp in send_xpaths:
                    if sent:
                        break
                    try:
                        for btn in self.driver.find_elements(By.XPATH, xp):
                            if not btn.is_displayed():
                                continue
                            label = btn.get_attribute('aria-label')
                            target = btn
                            # Click the button element itself when the label is
                            # on an inner span.
                            try:
                                clickable = btn.find_element(
                                    By.XPATH, "ancestor-or-self::*[self::button or @role='button'][1]")
                                target = clickable
                            except Exception:
                                pass
                            self.driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", target)
                            time.sleep(0.2)
                            ActionChains(self.driver).move_to_element(target).click().perform()
                            print(f"[image] Clicked send control (aria-label={label!r}) via ActionChains")
                            if _confirm_editor_closed():
                                sent = True
                                print("[image] Media editor closed — send accepted")
                                break
                            print("[image] Editor still open after click — trying next strategy")
                    except Exception as e_send:
                        print(f"[image] Send attempt on {xp} failed: {e_send}")

                # Fallback: Enter key while the editor has focus.
                if not sent:
                    try:
                        ActionChains(self.driver).send_keys(Keys.RETURN).perform()
                        print("[image] Pressed Enter in media editor")
                        if _confirm_editor_closed():
                            sent = True
                            print("[image] Media editor closed after Enter — send accepted")
                    except Exception:
                        pass

                if not sent:
                    _dump_dom_state("send-failed")
                    return {"error": (
                        "Image was attached and previewed, but the media editor "
                        "would not close after clicking Send — the image was NOT sent.")}

                time.sleep(2)

                if sent:
                    # ── Verify delivery: poll for an outgoing message bubble
                    # containing media to appear in the chat. Clicking "send"
                    # is not proof of delivery — the tool must not claim it is.
                    # Current WhatsApp Web selectors: .message-out and
                    # div.message-out still exist but are sometimes wrapped in
                    # [data-testid="msg-container"]; the outgoing tick icons
                    # use data-icon="msg-check"/"msg-dblcheck"/"msg-time".
                    # A new media row appearing in the conversation is proof the
                    # image is really in the chat. (The old check looked for
                    # div.message-out / data-testid tick icons — both removed
                    # from current WhatsApp Web, so it always said UNCONFIRMED
                    # even on successful sends.)
                    #
                    # NOTE: do NOT rely on the media-row COUNT increasing.
                    # WhatsApp virtualizes the message list — older rows are
                    # unmounted as new ones mount, so the count can stay flat
                    # after a perfectly successful send.  Instead inspect the
                    # LAST row: an outgoing image is proven by the row holding
                    # a picture *and* an outgoing status label ("Delivered",
                    # "Sent", "Read", "Pending" — note WhatsApp pads these
                    # with spaces, e.g. " Delivered ").
                    verified = False
                    try:
                        for _ in range(20):
                            res = self.driver.execute_script("""
                                var main = document.querySelector('#main');
                                if (!main) return null;
                                var rows = main.querySelectorAll('[role="row"]');
                                if (!rows.length) return null;
                                var last = rows[rows.length - 1];
                                var hasPic = !!last.querySelector(
                                    'img[src^="blob:"], img[src^="data:"], video')
                                    || !!Array.from(last.querySelectorAll('[aria-label]'))
                                        .find(function(e){
                                            return /open picture|forward media|abrir imagen/i
                                                .test(e.getAttribute('aria-label') || '');
                                        });
                                var status = Array.from(last.querySelectorAll('[aria-label]'))
                                    .map(function(e){ return (e.getAttribute('aria-label')||'').trim(); })
                                    .find(function(l){
                                        return /^(Sent|Delivered|Read|Pending|Enviado|Entregado|Le[ií]do|Pendiente)$/i.test(l);
                                    });
                                return {hasPic: hasPic, status: status || null};
                            """)
                            if res and res.get('hasPic') and res.get('status'):
                                verified = True
                                print(f"[image] Last chat row is an outgoing picture "
                                      f"with status {res['status']!r}")
                                break
                            time.sleep(1.5)
                        if not verified:
                            # Fallback: a brand-new media row appeared.
                            after = _media_row_count()
                            if after > media_rows_before:
                                verified = True
                                print(f"[image] Media rows after send: {after} "
                                      f"(was {media_rows_before}) — new media bubble present")
                    except Exception as e_ver:
                        print(f"[image] Delivery verification check failed: {e_ver}")
                    print(f"[image] Image delivery to {clean_num}: "
                          f"{'CONFIRMED in chat UI' if verified else 'UNCONFIRMED'}")
                    if not verified:
                        _dump_dom_state("delivery-unconfirmed")

                    contact_key = clean_num
                    if contact_key not in self.recently_sent:
                        self.recently_sent[contact_key] = []
                    self.recently_sent[contact_key].append({
                        "message": f"[image:{os.path.basename(abs_path)}] {caption}".strip(),
                        "timestamp": time.time(),
                    })
                    return {
                        "success": True,
                        "sent_file": abs_path,
                        "verified": verified,
                        "message": (
                            f"Image sent to {clean_num} and confirmed visible in the chat."
                            if verified else
                            f"Image send to {clean_num} was initiated but could NOT be "
                            "confirmed in the chat UI — it may not have been delivered. "
                            "Tell the user delivery is uncertain instead of claiming it arrived."
                        ),
                    }
                else:
                    return {"error": "Image previewed but send button not found"}

            except Exception as e:
                print(f"[image] send_image error: {e}")
                import traceback
                traceback.print_exc()
                return {"error": str(e)}

    def _fetch_fullres_image_from_viewer(self):
        """
        Open the media viewer for the latest incoming image in the currently
        open chat and fetch the FULL-RESOLUTION blob shown there. The in-chat
        <img> elements only carry small WebP previews; the viewer blob is the
        full-quality image. Returns a data-URI string or None.
        Caller must hold driver_lock.
        """
        opened = self.driver.execute_script("""
            var msgs = Array.from(document.querySelectorAll('div.message-in, .message-in'));
            for (var i = msgs.length - 1; i >= 0; i--) {
                var img = msgs[i].querySelector('img');
                if (!img) continue;
                var clickEl = img.closest('div[role="button"]') || img;
                clickEl.click();
                return true;
            }
            return false;
        """)
        if not opened:
            return None
        try:
            time.sleep(2.5)  # let the viewer render the full image
            return self.driver.execute_async_script("""
                var callback = arguments[arguments.length - 1];
                (function() {
                    var imgs = Array.from(document.querySelectorAll('img[src^="blob:"]'));
                    if (!imgs.length) { callback(null); return; }
                    // The media viewer shows the largest image currently in the DOM.
                    imgs.sort(function(a, b) {
                        return ((b.naturalWidth || 0) * (b.naturalHeight || 0))
                             - ((a.naturalWidth || 0) * (a.naturalHeight || 0));
                    });
                    var src = imgs[0].src;
                    fetch(src).then(function(r) { return r.blob(); }).then(function(blob) {
                        var rd = new FileReader();
                        rd.onloadend = function() { callback(rd.result); };
                        rd.onerror   = function() { callback(null); };
                        rd.readAsDataURL(blob);
                    }).catch(function() { callback(null); });
                })();
            """)
        finally:
            # Close the viewer so subsequent chat interaction works
            try:
                ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.5)
            except Exception:
                pass

    @staticmethod
    def _sniff_image_format(raw):
        """Return (format_name, extension) from magic bytes, or ('bin', '.bin')."""
        if raw[:3] == b'\xff\xd8\xff':
            return 'jpeg', '.jpg'
        if raw[:8] == b'\x89PNG\r\n\x1a\n':
            return 'png', '.png'
        if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            return 'webp', '.webp'
        if raw[:4] == b'GIF8':
            return 'gif', '.gif'
        if raw[:2] == b'BM':
            return 'bmp', '.bmp'
        return 'bin', '.bin'

    def _save_incoming_image(self, image_data, safe_id, full_res):
        """
        Decode a base64 data-URI, normalise the image to a real JPEG (via PIL
        when available — downstream vision tooling keys off the extension, and
        a WebP saved as .jpg breaks it), sniff the true format, and save it
        into _WHATSAPP_IMAGES_DIR.

        Returns {"success": True, "image_path": ..., "width": w, "height": h,
                 "format": fmt, "preview_only": bool} or {"error": ...}.
        """
        try:
            if "," in image_data:
                _, b64_data = image_data.split(",", 1)
            else:
                b64_data = image_data
            raw = base64.b64decode(b64_data)
        except Exception as e_dec:
            return {"error": f"Could not decode image data: {e_dec}"}

        fmt, ext = self._sniff_image_format(raw)
        width = height = None

        # Normalise to JPEG with PIL (flatten alpha onto white). This fixes
        # WebP/PNG-with-alpha payloads so the file always matches its .jpg
        # extension and loads in any vision pipeline.
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(raw))
            width, height = im.size
            if (im.format or '').lower() != 'jpeg':
                if im.mode in ('RGBA', 'LA', 'P'):
                    rgba = im.convert('RGBA')
                    bg = Image.new('RGB', rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[-1])
                    im = bg
                else:
                    im = im.convert('RGB')
                buf = io.BytesIO()
                im.save(buf, 'JPEG', quality=92)
                raw = buf.getvalue()
                fmt, ext = 'jpeg', '.jpg'
        except Exception as e_pil:
            # PIL unavailable or decode failed — keep original bytes + sniffed ext
            print(f"[images] PIL normalisation skipped: {e_pil}")

        # Flag low-quality previews (in-chat thumbnails are ~512px)
        preview_only = (not full_res) and bool(
            width and height and max(width, height) <= 640)

        os.makedirs(_WHATSAPP_IMAGES_DIR, exist_ok=True)
        timestamp = int(time.time())
        filename = f"incoming_{safe_id}_{timestamp}{ext}"
        dest_path = os.path.join(_WHATSAPP_IMAGES_DIR, filename)
        try:
            with open(dest_path, "wb") as fh:
                fh.write(raw)
        except Exception as e_wr:
            return {"error": f"Could not write image file: {e_wr}"}

        return {
            "success": True,
            "image_path": dest_path,
            "width": width,
            "height": height,
            "format": fmt,
            "preview_only": preview_only,
        }

    def _download_incoming_image_from_chat(self, phone_number_or_name):
        """
        Open the chat for phone_number_or_name, find the latest incoming image
        message, download it, and save to _WHATSAPP_IMAGES_DIR (the OpenClaw
        manager agent's workspace subdirectory).

        Assumes driver_lock is already held by the caller (get_new_messages).

        Returns:
          {"success": True, "image_path": "/abs/path.jpg", "caption": "...",
           "width": w, "height": h, "format": fmt, "preview_only": bool}
          {"error": "reason"}
        """
        try:
            p_str = str(phone_number_or_name).strip()
            clean_num = "".join(filter(str.isdigit, p_str))

            # Navigate to the specific chat
            if clean_num and len(clean_num) >= 7:
                self.driver.get(f"https://web.whatsapp.com/send?phone={clean_num}")
            else:
                try:
                    span = self.driver.find_element(
                        By.XPATH, f"//span[@title='{p_str}']")
                    ActionChains(self.driver).move_to_element(span).click().perform()
                except Exception:
                    return {"error": f"Cannot navigate to chat for '{p_str}'"}

            # Wait for the chat panel to load
            try:
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@id='main']"))
                )
            except TimeoutException:
                pass
            time.sleep(3)

            # Scroll to bottom so the latest messages are visible
            try:
                self.driver.execute_script("""
                    var panel =
                        document.querySelector('[data-testid="conversation-panel-messages"]') ||
                        document.querySelector('#main');
                    if (panel) panel.scrollTop = panel.scrollHeight;
                """)
                time.sleep(1)
            except Exception:
                pass

            # Extract caption text from the latest incoming image message
            caption = ""
            try:
                caption = self.driver.execute_script("""
                    var msgs = Array.from(document.querySelectorAll(
                        'div.message-in, .message-in'));
                    for (var i = msgs.length - 1; i >= 0; i--) {
                        if (!msgs[i].querySelector('img')) continue;
                        var cap = msgs[i].querySelector(
                            '[data-testid="caption"], .copyable-text span[dir], .image-caption');
                        return cap ? cap.textContent.trim() : '';
                    }
                    return '';
                """) or ""
            except Exception:
                pass

            # ── Download the image blob ───────────────────────────────────────
            # Set a generous async script timeout
            try:
                self.driver.set_script_timeout(25)
            except Exception:
                pass

            image_data = None
            full_res = False

            # Attempt 1: open the media viewer for the latest incoming image and
            # fetch the FULL-RESOLUTION blob shown there. (The in-chat <img> is
            # only a small WebP preview — saving it gives the agent a useless
            # 512px thumbnail.)
            try:
                image_data = self._fetch_fullres_image_from_viewer()
                full_res = bool(image_data)
                if full_res:
                    print("Full-resolution image fetched via media viewer")
            except Exception as e_viewer:
                print(f"Viewer full-res fetch failed: {e_viewer}")
                image_data = None
                full_res = False

            # Attempt 2: fetch() the blob URL from the latest incoming img element
            # (NOTE: this is usually only the small WebP in-chat preview.)
            if not image_data:
                try:
                    image_data = self.driver.execute_async_script("""
                        var callback = arguments[arguments.length - 1];
                        (function() {
                            var selectors = [
                                'div.message-in img[src^="blob:"]',
                                'div.message-in img[src^="data:"]',
                                '[data-testid="media-image"]',
                                '.message-in img',
                                'img[src^="blob:"]'
                            ];
                            var img = null;
                            for (var s = 0; s < selectors.length; s++) {
                                var found = document.querySelectorAll(selectors[s]);
                                if (found.length > 0) { img = found[found.length - 1]; break; }
                            }
                            if (!img) { callback(null); return; }
                            var src = img.src || img.getAttribute('src') || '';
                            if (!src) { callback(null); return; }
                            if (src.startsWith('data:')) { callback(src); return; }
                            if (src.startsWith('blob:')) {
                                fetch(src)
                                    .then(function(r) { return r.blob(); })
                                    .then(function(blob) {
                                        var rd = new FileReader();
                                        rd.onloadend = function() { callback(rd.result); };
                                        rd.onerror   = function() { callback(null); };
                                        rd.readAsDataURL(blob);
                                    })
                                    .catch(function() { callback(null); });
                                return;
                            }
                            callback(null);
                        })();
                    """)
                except Exception as e_fetch:
                    print(f"Blob fetch failed: {e_fetch}")

            # Attempt 3 (last resort): draw the img onto a canvas and export as JPEG
            if not image_data:
                try:
                    image_data = self.driver.execute_async_script("""
                        var callback = arguments[arguments.length - 1];
                        var imgs = document.querySelectorAll(
                            '.message-in img, [data-testid="media-image"]');
                        var img = imgs[imgs.length - 1];
                        if (!img) { callback(null); return; }
                        var c = document.createElement('canvas');
                        c.width  = img.naturalWidth  || img.width  || 800;
                        c.height = img.naturalHeight || img.height || 600;
                        try {
                            c.getContext('2d').drawImage(img, 0, 0);
                            callback(c.toDataURL('image/jpeg', 0.92));
                        } catch(e) { callback(null); }
                    """)
                except Exception as e_canvas:
                    print(f"Canvas fallback also failed: {e_canvas}")

            if not image_data:
                return {"error": "Could not extract image data from WhatsApp Web chat"}

            # ── Normalise format and save ─────────────────────────────────────
            safe_id = clean_num if clean_num else re.sub(r'[^a-zA-Z0-9]', '_', p_str)[:20]
            saved = self._save_incoming_image(image_data, safe_id, full_res)
            if "error" in saved:
                return saved
            saved["caption"] = caption
            print(f"Incoming WhatsApp image saved: {saved['image_path']} "
                  f"({saved.get('width')}x{saved.get('height')} {saved.get('format')}"
                  f"{', preview-only' if saved.get('preview_only') else ''})")
            return saved

        except Exception as e:
            print(f"_download_incoming_image_from_chat error: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}
        finally:
            try:
                self.driver.set_script_timeout(30)
            except Exception:
                pass

    def _process_image_messages(self, new_msgs):
        """
        For any messages in new_msgs whose sidebar preview text indicates an
        image / media message — OR whose chat row suggests media (covers
        media-with-caption, where the preview only shows the caption text) —
        open the chat, download the full image, save it to
        _WHATSAPP_IMAGES_DIR, and update the message dict in-place with:
          {"type": "image", "image_path": "/abs/path.jpg", "caption": "...",
           "width": w, "height": h, "preview_only": bool}

        Identical image content downloaded twice within 10 minutes for the
        same sender is suppressed (the downloader grabs the LATEST incoming
        image, which is sometimes one we already dispatched).

        Must be called while driver_lock is already held (e.g. from
        get_new_messages).  Non-fatal: failures are logged but don't abort the
        rest of the message list.
        """
        img_queue = [
            m for m in new_msgs
            if (any(p in m.get("message", "") for p in _IMAGE_PREVIEW_PATTERNS)
                or m.get("maybe_media"))
            and not m.get("type")  # skip if already typed
        ]
        if not img_queue:
            return

        print(f"[images] Downloading {len(img_queue)} incoming image message(s)…")
        for msg in list(img_queue):
            try:
                dl = self._download_incoming_image_from_chat(msg["from"])
                if dl.get("success"):
                    # Duplicate-content suppression
                    try:
                        import hashlib as _hl
                        with open(dl["image_path"], "rb") as _fh:
                            md5 = _hl.md5(_fh.read()).hexdigest()
                        prev = _RECENT_IMAGE_MD5.get(msg["from"])
                        if prev and prev[0] == md5 and time.time() - prev[1] < 600:
                            print(f"[images] Suppressed duplicate re-dispatch of identical "
                                  f"image for {msg['from']} (already sent {int(time.time()-prev[1])}s ago)")
                            if msg in new_msgs:
                                new_msgs.remove(msg)
                            continue
                        _RECENT_IMAGE_MD5[msg["from"]] = (md5, time.time())
                    except Exception as e_md5:
                        print(f"[images] md5 dedup check skipped: {e_md5}")

                    msg["type"] = "image"
                    msg["image_path"] = dl["image_path"]
                    msg["caption"] = dl.get("caption", "")
                    for _k in ("width", "height", "format", "preview_only"):
                        if dl.get(_k) is not None:
                            msg[_k] = dl[_k]
                    cap = msg["caption"]
                    msg["message"] = f"[image]{': ' + cap if cap else ''}"
                    print(f"[images] Saved for {msg['from']}: {dl['image_path']}")
                else:
                    print(f"[images] Download failed for {msg['from']}: {dl.get('error')}")
            except Exception as e_dl:
                print(f"[images] _process_image_messages error for {msg.get('from')}: {e_dl}")

    def start_monitoring(self, callback):
        """Start background monitoring thread"""
        self.monitoring = True
        def monitor():
            last_hb = 0
            while self.monitoring:
                try:
                    if time.time() - last_hb > 3600:
                        print(f"Monitor heartbeat ({time.strftime('%H:%M:%S')})")
                        last_hb = time.time()
                    
                    msgs = self.get_new_messages()
                    for msg in msgs:
                        callback(msg)
                except Exception as e:
                    print(f"Monitor error: {e}")
                
                time.sleep(10)  # Poll every 10 seconds
        
        self.monitor_thread = threading.Thread(target=monitor, daemon=True)
        self.monitor_thread.start()

    def stop_monitoring(self):
        """Stop background monitoring"""
        self.monitoring = False

    def driver_alive(self):
        """True if a live, responsive browser session exists."""
        if not self.driver:
            return False
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def ensure_driver(self, min_interval=60):
        """
        Recreate the browser if it never started or has died.  Rate-limited so
        that a page which polls every few seconds (e.g. /qr) cannot spawn a
        launch storm.  Returns True if a live driver is available afterwards.
        """
        if self.driver_alive():
            return True

        with self.driver_lock:
            if self.driver_alive():
                return True

            since = time.time() - self._last_recovery_attempt
            if since < min_interval:
                return False
            self._last_recovery_attempt = time.time()

            print("[driver] No live browser session — attempting recovery…")
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None

            try:
                self.start()
                self.last_error = None
                print("[driver] Recovery succeeded")
                return True
            except Exception as e:
                self.last_error = str(e)
                print(f"[driver] Recovery failed: {e}")
                return False

    def start(self):
        """Initialize the driver and load WhatsApp"""
        try:
            self._setup_driver()
        except Exception as e:
            self.last_error = str(e)
            raise
        self.last_error = None
        time.sleep(5)
        self.is_logged_in()


    def quit(self):
        """Clean shutdown"""
        self.stop_monitoring()
        with self.driver_lock:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
                self.driver = None
