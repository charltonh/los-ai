from flask import Flask, request, jsonify, render_template_string, redirect
import json
import os
import re
import subprocess
import sys
import requests
import threading
import atexit
import time
import tempfile
import base64
from datetime import datetime
from whatsapp_automation import (
    WhatsAppAutomation,
    _AGENT_WORKSPACE,
    _WHATSAPP_IMAGES_DIR,
    _kill_stale_drivers,
)


def _latest_incoming_image(max_age_sec=900, newer_than=None):
    """Return the absolute path of the newest incoming_* image younger than
    max_age_sec (default 15 min), or None. When newer_than (epoch) is given,
    the file must also be newer than that timestamp. Used to point the agent
    at a photo when the user references one in a text message."""
    try:
        candidates = [
            os.path.join(_WHATSAPP_IMAGES_DIR, f)
            for f in os.listdir(_WHATSAPP_IMAGES_DIR)
            if f.startswith('incoming_')
        ]
        if not candidates:
            return None
        newest = max(candidates, key=os.path.getmtime)
        if time.time() - os.path.getmtime(newest) > max_age_sec:
            return None
        if newer_than is not None and os.path.getmtime(newest) <= newer_than:
            return None
        return newest
    except Exception:
        pass
    return None


# Short per-sender history of recently dispatched TEXT messages, so an image
# that arrives shortly after a text instruction can carry the instruction as
# context (users often send the photo and the request as separate messages).
_recent_texts = {}  # {sender: [(epoch, text), ...]}


def _note_recent_text(sender, text):
    if not sender or not text:
        return
    _recent_texts.setdefault(sender, []).append((time.time(), text))
    _recent_texts[sender] = _recent_texts[sender][-5:]


def _recent_text_for(sender, max_age_sec=300):
    """Most recent dispatched text from this sender within max_age_sec."""
    try:
        for ts, text in reversed(_recent_texts.get(sender, [])):
            if time.time() - ts <= max_age_sec:
                return text
    except Exception:
        pass
    return None


def _sender_digits(from_number, config):
    """Best-effort digits for the sender: use them when the sender id already
    contains digits; otherwise fall back to the configured owner number."""
    digits = ''.join(ch for ch in str(from_number) if ch.isdigit())
    if len(digits) >= 7:
        return digits
    return (config or {}).get('owner_num', '') or ''


def _reply_context_line(from_number, config):
    """Tell the agent exactly who it's talking to and how to reach them, on
    every dispatch (independent of session memory)."""
    digits = _sender_digits(from_number, config)
    to_part = f'to="{digits}"' if digits else 'to="<their number, digits only>"'
    return (f'\n[Reply context: sender="{from_number}". Your plain-text reply is delivered '
            f'to them automatically — do NOT use whatsapp_send for that. To send THEM an '
            f'image/file, use whatsapp_send_image with {to_part}. whatsapp_send(_image) is '
            f'only for messaging OTHER people.]')

# The LOS user whose .config holds the 'whatsapp' section.  LOS_USER is set
# by the gateway from /los/sys/config.json; falling back to the system user
# preserves the previous standalone behaviour.
#
# Note that one WhatsApp session serves the whole install: a single phone
# number and browser profile.  Individual senders are kept apart further
# down the chain — each incoming message is dispatched to aicall with
# LOS_SESSION_SUFFIX set to the sender's number, so every contact gets its
# own conversation thread and memory.
USERNAME = (os.getenv('LOS_USER') or os.getenv('USER')
            or os.getenv('LOGNAME') or 'default')

app = Flask(__name__)

# Initialize WhatsApp automation
whatsapp = None

def handle_incoming_message(msg):
    """Handle incoming message from polling"""
    from_number = msg.get("from", "")
    message_body = msg.get("message", "")
    msg_type = msg.get("type", "text")
    image_path = msg.get("image_path", "")
    image_caption = msg.get("caption", "")

    if not from_number:
        return
    if not message_body and msg_type != "image":
        return

    print(f"Polled WhatsApp message from {from_number}: {message_body}")

    # Get config and aiconfig
    config = parse_whatsapp_config()
    if not config:
        print("Could not parse WhatsApp config")
        return

    aiconfig_path = config.get('aiconfig')
    if not aiconfig_path:
        print("No aiconfig specified in whatsapp config")
        return

    try:
        with open(aiconfig_path, 'r') as f:
            aiconfig = json.load(f)
    except Exception as e:
        print(f"Could not load aiconfig: {e}")
        return

    # Build the aicall prompt — richer context when an image is present
    if msg_type == "image" and image_path and os.path.exists(image_path):
        # Absolute path so the agent can find the file without guessing its CWD
        abs_image = os.path.abspath(image_path)
        cap_text = f'\nCaption: "{image_caption}"' if image_caption else ""
        dims = ""
        if msg.get("width") and msg.get("height"):
            dims = f" ({msg['width']}x{msg['height']}"
            if msg.get("preview_only"):
                dims += " — NOTE: preview quality only, the full-resolution image could not be retrieved"
            dims += ")"
        user_prompt = (
            f"Incoming WhatsApp image from {from_number}.{cap_text}\n"
            f"Image file (absolute path): {abs_image}{dims}\n"
            f"View this image with your vision tools before responding. If you cannot "
            f"actually load or see it, say so honestly and ask the user to resend — "
            f"do NOT guess or describe contents you cannot see."
        )
        # Photo and its instruction often arrive as separate messages — carry
        # the sender's recent text along so the agent knows what to DO with it.
        if not image_caption:
            prior_text = _recent_text_for(from_number, max_age_sec=300)
            if prior_text and prior_text != message_body:
                user_prompt += (
                    f"\n[Context: shortly before sending this image, the sender wrote: "
                    f"\"{prior_text[:300]}\"]"
                )
    else:
        user_prompt = f"Incoming WhatsApp message from {from_number}: {message_body}"
        # If the text references a picture, point the agent at the most recent
        # incoming image (photos and the text about them often arrive separately)
        if message_body and re.search(
                r'\b(photo|picture|image|pic|pics|foto|screenshot|selfie)\b',
                message_body, re.IGNORECASE):
            recent_img = _latest_incoming_image(max_age_sec=900)
            if not recent_img:
                # The photo may still be in transit/downloading (e.g. a captioned
                # photo whose media wasn't detected yet) — give it a few seconds.
                dispatch_start = time.time()
                for _ in range(10):
                    time.sleep(2)
                    recent_img = _latest_incoming_image(max_age_sec=300, newer_than=dispatch_start - 5)
                    if recent_img:
                        break
            if recent_img:
                user_prompt += (
                    f"\n[Context: this sender's most recent image is at {recent_img} "
                    f"(received within the last 15 minutes) — use it if relevant to their request.]"
                )
        # Remember this text so a shortly-following image can reference it
        if message_body:
            _note_recent_text(from_number, message_body)

    # Always tell the agent exactly who it's talking to and how to reach them
    user_prompt += _reply_context_line(from_number, config)
    print(f"Sending user prompt to aicall ({len(user_prompt)} chars): {user_prompt}")

    # Call aicall with the incoming message.  Pin the aicall session id to the
    # sender's phone number so the AI sees a persistent conversation per contact.
    try:
        from subprocess import run, PIPE

        env = os.environ.copy()
        env['LOS_SESSION_SUFFIX'] = from_number

        result = run([sys.executable, '/los/sys/aicall/aicall.py', config.get('aicall_label', 'whatsapp_incoming'), '-'],
                     input=user_prompt,
                     text=True,
                     capture_output=True,
                     check=False,
                     env=env)

        if result.returncode == 0:
            response_content = result.stdout.strip()
            if not response_content:
                response_content = "Sorry, there was no response generated. Please try again."
        else:
            response_content = f"Sorry, there was an error processing your message. stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}"
        print(f"Aicall response: {response_content}")

    except Exception as e:
        response_content = "Sorry, there was an error processing your message."
        print(f"Error calling aicall: {e}")

    # Send the response back via WhatsApp
    send_result = whatsapp.send_message(from_number, response_content)
    if 'error' in send_result:
        print(f"Error sending reply: {send_result['error']}")

# Placeholder for the MCP configuration
MCP_CONFIG = {
    "name": "whatsapp-messaging",
    "tools": {
        "whatsapp_send": {
            "description": "Send a WhatsApp text message to a specified number.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number (digits)"},
                    "message": {"type": "string", "description": "Text message to send"}
                },
                "required": ["to", "message"]
            }
        },
        "whatsapp_send_image": {
            "description": (
                "Send an image (or video) via WhatsApp to a recipient. "
                "Provide exactly ONE of: image_path (local file), image_url (HTTP URL — "
                "the server will download it), or image_base64 (base64-encoded image data). "
                "Use this tool to share food photos, screenshots, or any visual content."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number (digits)"},
                    "image_path": {"type": "string", "description": "Absolute local path to the image file"},
                    "image_url": {"type": "string", "description": "HTTP/HTTPS URL of an image to download and send"},
                    "image_base64": {"type": "string", "description": "Base64-encoded image data (with or without data-URI prefix)"},
                    "caption": {"type": "string", "description": "Optional text caption for the image"}
                },
                "required": ["to"]
            }
        }
    }
}

# Custom config parser (simplified for this service)
def parse_whatsapp_config():
    config_path = f"/los/{USERNAME}/.config"
    config = {}

    try:
        with open(config_path, 'r') as f:
            lines = f.readlines()

        in_whatsapp_section = False
        in_messenger_subsection = False

        for line in lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith('#'):
                continue

            current_indentation = len(line) - len(stripped_line)

            if stripped_line.endswith(':'):
                section_name = stripped_line[:-1]
                if section_name == 'whatsapp':
                    in_whatsapp_section = True
                    in_messenger_subsection = False
                elif section_name == 'messenger' and in_whatsapp_section:
                    in_messenger_subsection = True
                else:
                    in_whatsapp_section = False
                    in_messenger_subsection = False
            elif in_messenger_subsection:
                if '=' in stripped_line:
                    key, value = stripped_line.split('=', 1)
                    config[key.strip()] = value.strip().strip('"')

            elif in_whatsapp_section and not in_messenger_subsection:
                # Add session_id and token directly under whatsapp section
                if '=' in stripped_line:
                    key, value = stripped_line.split('=', 1)
                    # Strip spaces, then remove outer quotes if present
                    stripped_value = value.strip()
                    if stripped_value.startswith('"') and stripped_value.endswith('"'):
                        stripped_value = stripped_value[1:-1]
                    # Remove comments after #
                    clean_value = stripped_value.split('#')[0].strip().replace('"', '').strip()
                    config[key.strip()] = clean_value

    except FileNotFoundError:
        print(f"Error: Config file not found at {config_path}", file=sys.stderr)
        return None

    return config

# Use Super-Light-Web-WhatsApp-API-Server to send a WhatsApp message
def send_whatsapp_message(recipient_number, message_body, config):
    session_id = config.get("session_id")
    token = config.get("token")

    if not session_id or not token:
        return {"error": "Missing session_id or token in whatsapp section of .config"}

    url = f"http://localhost:3000/api/v1/messages?sessionId={session_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "recipient_type": "individual",
        "to": recipient_number,
        "type": "text",
        "text": {"body": message_body}
    }

    try:
        print(f"Sending POST to {url} with headers {headers} and data {data}")
        response = requests.post(url, headers=headers, json=data)
        print(f"Response status: {response.status_code}, text: {response.text}")
        if response.status_code == 200:
            response_json = response.json()
            if isinstance(response_json, list) and response_json:
                response_json = response_json[0]
            if response_json.get("status") == "success":
                return {"success": True, "response": response_json}
            else:
                return {"error": f"API returned error: {response_json.get('message', 'Unknown error')}"}
        else:
            return {"error": f"HTTP error {response.status_code}: {response.text}"}
    except Exception as e:
        print(f"Exception when sending: {str(e)}")
        return {"error": f"Error sending message: {str(e)}"}

# Endpoint to receive incoming WhatsApp messages
@app.route('/whatsapp/incoming', methods=['POST'])
def whatsapp_webhook():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json()

    if not data:
        return jsonify({"error": "No data received"}), 400

    # Log the complete incoming webhook data
    print(f"[{timestamp}] [WHATSAPP_MCP] Incoming webhook received: {json.dumps(data, default=str)}")

    # Parse Super-Light-Web-WhatsApp-API-Server format
    if data.get('event') == 'new-message':
        from_number = data.get('from', '').replace('@s.whatsapp.net', '').replace('@c.us', '')
        message_body = data.get('data', {}).get('message', {}).get('conversation', '')
    else:
        # Fallback to other formats if needed
        from_number = data.get('From', '').replace('whatsapp:', '')
        message_body = data.get('Body', '')

    if not from_number or not message_body:
        return jsonify({"error": "Invalid message data"}), 400

    print(f"Received WhatsApp message from {from_number}: {message_body}")

    # Get config and aiconfig
    config = parse_whatsapp_config()
    if not config:
        return jsonify({"error": "Could not parse WhatsApp config"}), 500

    aiconfig_path = config.get('aiconfig')
    if not aiconfig_path:
        return jsonify({"error": "No aiconfig specified in whatsapp config"}), 500

    try:
        with open(aiconfig_path, 'r') as f:
            aiconfig = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Could not load aiconfig: {e}"}), 500

    # Prepare prompt - just the incoming message
    user_prompt = f"Incoming WhatsApp message from {from_number}: {message_body}"
    print(f"Sending user prompt to aicall: {user_prompt}")

    # Call aicall with the incoming message. Pin the session id to the sender.
    try:
        # Import and use the existing aicall logic
        from subprocess import run, PIPE

        env = os.environ.copy()
        env['LOS_SESSION_SUFFIX'] = from_number

        result = run([sys.executable, '/los/sys/aicall/aicall.py', config.get('aicall_label', 'whatsapp_incoming'), '-'],
                     input=user_prompt,
                     text=True,
                     capture_output=True,
                     check=False,
                     env=env)

        if result.returncode == 0:
            response_content = result.stdout.strip()
            if not response_content:
                response_content = "Sorry, there was no response generated. Please try again."
        else:
            response_content = f"Sorry, there was an error processing your message. stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}"
        print(f"Aicall response: {response_content}")

    except Exception as e:
        response_content = "Sorry, there was an error processing your message."
        print(f"Error calling aicall: {e}", file=sys.stderr)

    # Send the response back via WhatsApp
    send_result = send_whatsapp_message(from_number, response_content, config)
    if 'error' in send_result:
        print(f"Error sending reply: {send_result['error']}", file=sys.stderr)
        return jsonify({"error": "Failed to send reply"}), 500

    return jsonify({"status": "Message processed and replied to"}), 200

@app.route('/qr', methods=['GET'])
@app.route('/qr/<action>', methods=['GET'])
def show_qr(action=None):
    """Display QR code for WhatsApp authentication or connection status"""

    # Handle actions
    if action == 'reconnect':
        global whatsapp
        import shutil
        try:
            print("Forcing reconnection - clearing session data...")
            # Close the old driver (if any).  quit() must never raise out of
            # this handler — a driver that failed to launch still has partial
            # state, and we must continue to rebuild the automation anyway.
            if whatsapp:
                whatsapp.is_authenticated = False
                old_driver = getattr(whatsapp, 'driver', None)
                quit_done = threading.Event()

                def _do_quit():
                    try:
                        whatsapp.stop_monitoring()
                        if old_driver:
                            old_driver.quit()
                    except Exception as e:
                        print(f"Error quitting old driver: {e}")
                    finally:
                        whatsapp.driver = None
                        quit_done.set()

                t = threading.Thread(target=_do_quit, daemon=True)
                t.start()
                # driver.quit() can hang on a wedged Firefox — cap the wait
                if not quit_done.wait(timeout=15):
                    print("Driver quit timed out after 15s — proceeding with forced cleanup")

            # Kill any orphaned geckodriver/chromedriver/firefox that is still
            # holding the profile or the driver binary (this is the direct fix
            # for 'Text file busy' on the next launch).
            try:
                _kill_stale_drivers(os.path.abspath("./whatsapp_session"))
            except Exception as e:
                print(f"Stale-driver cleanup warning: {e}")
            time.sleep(2)

            # Clear profile directory to ensure a fresh QR
            if os.path.exists("./whatsapp_session"):
                try:
                    shutil.rmtree("./whatsapp_session")
                    print("Cleared session data for fresh authentication")
                except Exception as e:
                    print(f"Error clearing session directory: {e}")
                    # Leftover files (e.g. still-locked sqlite) — try once more
                    try:
                        _kill_stale_drivers(os.path.abspath("./whatsapp_session"))
                        time.sleep(2)
                        shutil.rmtree("./whatsapp_session", ignore_errors=True)
                    except Exception:
                        pass

            # Always rebuild the automation, even if the old one never started
            # or the session directory did not exist.
            whatsapp = WhatsAppAutomation(session_path="./whatsapp_session")
            try:
                whatsapp.start()
                whatsapp.start_monitoring(handle_incoming_message)
                print("Reconnect complete — fresh session started")
            except Exception as start_err:
                # Keep `whatsapp` non-None so /qr can render diagnostics and
                # ensure_driver() can retry from the auto-refreshing page.
                print(f"Reconnect start failed: {start_err}")
                try:
                    whatsapp.start_monitoring(handle_incoming_message)
                except Exception:
                    pass
        except Exception as e:
            print(f"Error during reconnect: {e}")
        return redirect('/qr')


    # If the browser never started (or has died) try to bring it back before
    # rendering anything. ensure_driver() is rate-limited internally, so the
    # 4-second auto-refresh of this page cannot trigger a launch storm.
    if whatsapp and not whatsapp.driver_alive():
        whatsapp.ensure_driver()

    # Check authentication status
    try:
        is_auth = whatsapp and whatsapp.is_logged_in()
    except Exception as e:
        print(f"Error checking authentication: {e}")
        is_auth = False


    if is_auth:
        # Show connected status
        return render_template_string("""
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
                .status { color: green; font-size: 24px; margin: 20px; }
                .info { background: #f0f0f0; padding: 20px; border-radius: 10px; margin: 20px; }
                button { background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; }
                button:hover { background: #0056b3; }
                .reconnect { margin-top: 50px; color: #666; font-size: 12px; }
            </style>
        </head>
        <body>
        <h1>WhatsApp MCP Status</h1>
        <div class="status">✅ CONNECTED</div>
        <div class="info">
            <p>Your WhatsApp is linked and ready to send/receive messages.</p>
            <p><strong>Message polling:</strong> Active (every 10 seconds)</p>
            <p><strong>API endpoint:</strong> <code>POST /tools/whatsapp_send</code></p>
        </div>
        <div class="reconnect">
            <p>Need to link a different device?</p>
            <button onclick="if(confirm('This will log you out and clear the session. Continue?')) location.href='/qr/reconnect'">Reconnect Device</button>
        </div>
        </body>
        </html>
        """)

    # Not authenticated - show QR code or loading
    try:
        qr_data = whatsapp.get_qr_code_image()
    except Exception as e:
        print(f"Error getting QR code: {e}")
        qr_data = None

    if qr_data:
        return render_template_string("""
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; padding: 20px; }
                img { border: 15px solid white; box-shadow: 0 0 20px rgba(0,0,0,0.1); border-radius: 10px; background: white; }
                .waiting { color: orange; margin: 20px; font-weight: bold; }
                .steps { text-align: left; display: inline-block; margin: 20px; padding: 20px; background: #f9f9f9; border-radius: 10px; }
            </style>
        </head>
        <body>
        <h1>Link WhatsApp Device</h1>
        <div class="steps">
            <strong>To link your device:</strong>
            <ol>
                <li>Open WhatsApp on your phone</li>
                <li>Tap <b>Menu</b> or <b>Settings</b> and select <b>Linked Devices</b></li>
                <li>Tap on <b>Link a Device</b></li>
                <li>Point your phone to this screen to capture the code</li>
            </ol>
        </div>
        <br>
        <img src="data:image/png;base64,{{ qr_data }}" alt="QR Code" style="max-width: 300px;">
        <div class="waiting">⏳ Waiting for scan...</div>
        <script>
            setTimeout(function() {
                location.reload();
            }, 4000);
        </script>
        </body>
        </html>
        """, qr_data=qr_data)
    else:
        # Loading/QR not ready - get diagnostics
        diag = {}
        try:
            diag = whatsapp.get_debug_info()
        except Exception as e:
            diag = {"error": str(e)}

        return render_template_string("""
        <html>
        <head>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; padding: 50px; }
                .loading { color: #007bff; font-size: 18px; margin: 20px; }
                .debug-box { background: #f8f9fa; border: 1px solid #ddd; padding: 15px; border-radius: 5px; 
                           margin: 20px auto; max-width: 800px; text-align: left; font-family: monospace; font-size: 12px; }
                .screenshot { max-width: 100%; border: 1px solid #ccc; margin-top: 10px; }
                button { padding: 8px 16px; cursor: pointer; }
            </style>
        </head>
        <body>
        <h1>WhatsApp MCP</h1>
        <div class="loading">🔄 WhatsApp is initializing...</div>
        <p>This usually takes 10-20 seconds. The page will refresh automatically.</p>
        
        <div class="debug-box">
            <strong>Diagnostic Info:</strong>
            <ul>
                <li><b>URL:</b> {{ diag.url }}</li>
                <li><b>Title:</b> {{ diag.title }}</li>
                <li><b>Status:</b> {{ diag.status or 'Initializing browser' }}</li>
            </ul>
            {% if diag.screenshot %}
                <strong>Current Browser View:</strong><br>
                <img src="data:image/png;base64,{{ diag.screenshot }}" class="screenshot">
            {% endif %}
        </div>

        <p><button onclick="location.href='/qr/reconnect'">Force Reset</button> <button onclick="location.reload()">Refresh Now</button></p>
        
        <script>
            setTimeout(function() {
                location.reload();
            }, 5000);
        </script>
        </body>
        </html>
        """, diag=diag)

@app.route('/', methods=['GET'])
def get_config():
    return jsonify(MCP_CONFIG)

@app.route('/tools/whatsapp_send_image', methods=['POST'])
def use_whatsapp_send_image():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [WHATSAPP_MCP] Tool called: whatsapp_send_image")
    try:
        data = request.get_json()
        print(f"[{timestamp}] [WHATSAPP_MCP] Input for 'whatsapp_send_image': to={data.get('to')} "
              f"image_path={data.get('image_path')} image_url={data.get('image_url')} "
              f"has_base64={bool(data.get('image_base64'))}")
    except Exception as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400

    recipient = (data.get("to") or data.get("phone") or data.get("recipient") or
                 data.get("number") or data.get("phone_number"))
    caption   = data.get("caption", "")
    image_path_raw   = data.get("image_path", "")
    image_url_raw    = data.get("image_url", "")
    image_base64_raw = data.get("image_base64", "")

    if not recipient:
        return jsonify({"error": "Missing recipient (to/phone/recipient)"}), 400
    if not (image_path_raw or image_url_raw or image_base64_raw):
        return jsonify({"error": "Supply one of: image_path, image_url, or image_base64"}), 400

    tmp_file = None
    final_path = image_path_raw  # may be overridden below

    try:
        if image_url_raw:
            # Download the image from the URL into a temp file
            print(f"[{timestamp}] Downloading image from URL: {image_url_raw[:120]}")
            resp = requests.get(image_url_raw, timeout=30, stream=True)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            ext = ".jpg"
            if "png" in content_type:   ext = ".png"
            elif "gif" in content_type: ext = ".gif"
            elif "webp" in content_type: ext = ".webp"
            tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            for chunk in resp.iter_content(chunk_size=65536):
                tmp_file.write(chunk)
            tmp_file.close()
            final_path = tmp_file.name
            print(f"[{timestamp}] Image saved to temp: {final_path}")

        elif image_base64_raw:
            # Decode base64 data
            b64 = image_base64_raw
            if "," in b64:
                header, b64 = b64.split(",", 1)
                ext = ".jpg"
                if "png" in header:  ext = ".png"
                elif "gif" in header: ext = ".gif"
            else:
                ext = ".jpg"
            tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp_file.write(base64.b64decode(b64))
            tmp_file.close()
            final_path = tmp_file.name
            print(f"[{timestamp}] Base64 image decoded to temp: {final_path}")

        if not final_path or not os.path.exists(final_path):
            return jsonify({"error": f"Image file not found: {final_path}"}), 400

        result = whatsapp.send_image(recipient, final_path, caption)
        if result.get("success"):
            # Surface the delivery-verification detail so the caller (and the
            # agent) knows whether the image actually appeared in the chat.
            return jsonify({"success": True,
                            "result": result.get("message", f"Image sent to {recipient}")})
        else:
            return jsonify(result)

    except requests.exceptions.RequestException as re_err:
        return jsonify({"error": f"Failed to download image: {re_err}"}), 500
    except Exception as e:
        print(f"[{timestamp}] whatsapp_send_image error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up temp file (best-effort)
        if tmp_file and os.path.exists(tmp_file.name):
            try:
                os.unlink(tmp_file.name)
            except Exception:
                pass


@app.route('/tools/whatsapp_send', methods=['POST'])
def use_whatsapp_send():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [WHATSAPP_MCP] Tool called: whatsapp_send")
    try:
        data = request.get_json()
        print(f"[{timestamp}] [WHATSAPP_MCP] Input received for 'whatsapp_send': {json.dumps(data, default=str)}")
    except Exception as e:
        print(f"[{timestamp}] [WHATSAPP_MCP] Invalid JSON in request: {e}")
        return jsonify({"error": f"Invalid JSON in request: {e}"}), 400

    recipient_number = (data.get("to") or data.get("phone") or data.get("recipient") or
                        data.get("to_number") or data.get("number") or data.get("phone_number"))
    message = data.get("message")

    if not recipient_number or not message:
        return jsonify({"error": "Missing recipient (to/phone/recipient) or 'message'"}), 400

    # Use WhatsApp automation to send message
    result = whatsapp.send_message(recipient_number, message)
    if 'success' in result and result['success']:
        return jsonify({
            "success": True,
            "result": f"WhatsApp message sent to {recipient_number}"
        })
    else:
        return jsonify(result)

def cleanup():
    """Clean up WhatsApp automation on exit"""
    print("Cleaning up WhatsApp automation...")
    if whatsapp:
        try:
            whatsapp.quit()
        except Exception as e:
            print(f"Error during cleanup: {e}")


def initialize_whatsapp():
    """Initialize WhatsApp automation"""
    global whatsapp
    try:
        print("Initializing WhatsApp automation...")
        whatsapp = WhatsAppAutomation(session_path="./whatsapp_session")
        whatsapp.start()
        print("Starting message monitoring...")
        whatsapp.start_monitoring(handle_incoming_message)
        print("✅ WhatsApp automation enabled!")
        return True
    except Exception as e:
        print(f"⚠️  WhatsApp automation failed to start: {e}")
        print("💡 Server will run in API-only mode. For full functionality:")
        print("   - Install Chrome/Chromium: emerge www-client/chromium (recommended for session persistence)")
        print("   - Or Firefox: emerge www-client/firefox")
        print("   - Then restart the server to enable WhatsApp automation")
        return False

if __name__ == '__main__':
    print("WhatsApp MCP Server starting...")

    # Register cleanup function
    atexit.register(cleanup)

    # Initialize WhatsApp automation
    automation_enabled = initialize_whatsapp()

    # Note: In production, use a proper WSGI server like Gunicorn.
    # Host/port come from /los/sys/config.json via the gateway (los gateway);
    # the defaults keep standalone behaviour on 127.0.0.1:5101, which
    # distinguishes this from aicall_mcp.
    # use_reloader=False is important to prevent starting automation twice
    # We set debug=False as well to be safer against double-init
    _host = os.environ.get('LOS_WHATSAPP_HOST', '127.0.0.1')
    _port = int(os.environ.get('LOS_WHATSAPP_PORT', '5101'))
    print(f"WhatsApp MCP Server listening on {_host}:{_port}")
    app.run(host=_host, port=_port, debug=False, use_reloader=False)
