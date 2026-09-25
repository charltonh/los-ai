#!/usr/bin/env python3
import os
import ssl
import json
import sys
import shutil # Added for directory operations
import re    # Added for validation
import fnmatch # Add this import for file pattern matching
import calendar as cal_module
import threading
import queue
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import quote

# Add the parent directory to the path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session
from auth import (
    authenticate_user, is_valid_username,
    get_user_data_paths, # Removed ensure_user_files_exist as calendar path is dynamic
)

app = Flask(__name__)
# The session secret comes from /los/sys/config.json via the gateway
# (services.ssl_server.secret_key, generated at install time).  When the app
# is started by hand outside the gateway we fall back to a random key, which
# is safe but invalidates existing sessions on every restart.
app.secret_key = os.environ.get('LOS_WEB_SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_SECURE'] = True  # Use secure cookies for HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Prevent JavaScript access to session cookie
app.config['PERMANENT_SESSION_LIFETIME'] = 315576000 # Session timeout in seconds (10 years)

# --- Security Headers ---
@app.after_request
def set_security_headers(response):
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


# --- Constants ---
LOS_BASE_PATH = "/los"
SKELETON_DIR = os.path.join(os.path.dirname(__file__), '..', 'skeleton') # Path to /los/sys/skeleton
ALLOWED_ENTITY_NAME_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')

# Directory holding the `los` package, i.e. /los/sys.  LOS_SYS is honoured so
# the lookup keeps working when the tree is installed elsewhere.
LOS_SYS_DIR = os.environ.get(
    'LOS_SYS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_los_version():
    """Return the installed LOS version (e.g. "1.0.2"), or "unknown".

    Read straight from the `los` package so the version shown in the dashboard
    can never drift from the code that is actually running.
    """
    try:
        if LOS_SYS_DIR not in sys.path:
            sys.path.insert(0, LOS_SYS_DIR)
        from los import __version__
        return __version__
    except Exception:
        return 'unknown'

# --- Decorators ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            # For API requests, return 401 Unauthorized
            if request.endpoint and request.endpoint.startswith('api_'):
                 return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- Helper Functions ---
def get_user_root_path():
    """Gets the absolute path to the current user's root directory in /los."""
    if 'username' not in session:
        return None
    # Construct path like /los/username
    user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, session['username']))
    # Basic check to ensure it's within LOS_BASE_PATH
    if not user_root.startswith(os.path.abspath(LOS_BASE_PATH) + os.sep):
        return None # Should not happen if LOS_BASE_PATH and username are sane
    return user_root

def read_safe(file_path, default=''):
    """Safely reads a file, returning default if error or not found."""
    try:
        if os.path.exists(file_path) and os.path.isfile(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        return default
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return default

def write_safe(file_path, content):
    """Safely writes content to a file."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Error writing file {file_path}: {e}")
        return False

def append_safe(file_path, line):
    """Safely appends a line to a file, adding newline if needed."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        # Ensure file exists before appending
        if not os.path.exists(file_path):
             with open(file_path, 'w', encoding='utf-8') as f:
                 f.write(line + '\n')
        else:
            # Open in append+read ('a+') mode to allow reading the last char
            with open(file_path, 'a+', encoding='utf-8') as f:
                # Add newline if file doesn't end with one
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(f.tell() - 1, os.SEEK_SET)
                    if f.read(1) != '\n':
                        f.write('\n')
                f.write(line + '\n')
        return True
    except Exception as e:
        print(f"Error appending to file {file_path}: {e}")
        return False

def is_safe_path(base_path, target_path):
    """Checks if target_path is safely within base_path."""
    abs_base = os.path.abspath(base_path)
    abs_target = os.path.abspath(target_path)
    return abs_target.startswith(abs_base + os.sep) or abs_target == abs_base


def scan_agenda_tree(root_dir, current_path=''):
    """
    Recursively scans directories within the user's agenda path.
    Returns a list of dictionaries: {'name': ..., 'path': ..., 'children': [...]}.
    """
    tree = []
    full_current_dir = os.path.join(root_dir, current_path)

    if not os.path.isdir(full_current_dir):
        return []

    try:
        # Read the parent's entities file to know valid sub-entities
        parent_entities_file = os.path.join(full_current_dir, 'entities')
        entities_content = read_safe(parent_entities_file)
        valid_sub_entities = set()
        for line in entities_content.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split(maxsplit=1) # Split only once
                if parts:
                    valid_sub_entities.add(parts[0]) # Add the name part

        for item in os.listdir(full_current_dir):
            full_item_path = os.path.join(full_current_dir, item)
            if os.path.isdir(full_item_path):
                # --- Filtering Logic ---
                # 1. Exclude specific names (like venvs, hidden dirs)
                if item == 'ssl_venv' or item.startswith('.'):
                    continue
                # 2. Check if the directory name is listed in the parent's entities file
                if item not in valid_sub_entities:
                    continue

                relative_item_path = os.path.join(current_path, item)
                node = {
                    'name': item,
                    'path': relative_item_path,
                    'children': scan_agenda_tree(root_dir, relative_item_path)
                }
                tree.append(node)
    except OSError as e:
        print(f"Error scanning directory {full_current_dir}: {e}")
        # Return empty list or partial tree depending on desired behavior
        return []

    # Sort alphabetically by name
    tree.sort(key=lambda x: x['name'])
    return tree


# --- Original File Operations (Legacy - might refactor later) ---
def read_data(file_path):
    """Reads data assuming one JSON object per line."""
    items = []
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line: # Avoid empty lines
                        # Keep items as JSON strings for consistency with how calendar_add appends
                        items.append(line)
        return items
    except Exception as e:
        print(f"Error reading legacy file {file_path}: {e}")
        return []

def write_data(file_path, items):
    """Writes data with one JSON object per line."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True) # Ensure directory exists
        with open(file_path, 'w', encoding='utf-8') as f:
            # items should already be a list of JSON strings
            for item_json_str in items:
                 if isinstance(item_json_str, str): # Ensure it's a string
                    f.write(item_json_str + '\n')
        return True
    except Exception as e:
        print(f"Error writing to legacy file {file_path}: {e}")
        return False

# --- New Helper Functions ---

def get_entity_data_file_path(entity_rel_path, data_subdir_name, filename):
    """Gets the absolute path to a specific data file within a given entity path."""
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return None # Invalid base entity path

    # Construct the path to the data file (e.g., /los/user/entity/data/todo/todo)
    data_file_path = os.path.abspath(os.path.join(entity_abs_path, 'data', data_subdir_name, filename))

    # Security check: Ensure the final data file path is still within the entity's path
    # This prevents manipulation like '../../other_user/data/...'
    data_dir_path = os.path.dirname(data_file_path)
    if not is_safe_path(entity_abs_path, data_dir_path):
         print(f"Security Alert: Attempt to access data file outside entity data directory. User: {session.get('username')}, Entity Path: {entity_abs_path}, Target File: {data_file_path}")
         return None

    return data_file_path


def get_entity_absolute_path(entity_rel_path):
    """Gets the validated absolute path for a relative entity path."""
    user_root = get_user_root_path()
    if not user_root:
        return None # User not logged in or root path error

    # Handle empty path (root of user's agenda)
    if not entity_rel_path:
        return user_root

    # Prevent path traversal and ensure it's relative
    if '..' in entity_rel_path or entity_rel_path.startswith('/'):
        print(f"Warning: Invalid relative path requested: {entity_rel_path}")
        return None

    entity_abs_path = os.path.abspath(os.path.join(user_root, entity_rel_path))

    # Final safety check
    if not is_safe_path(user_root, entity_abs_path):
        print(f"Security Alert: Attempt to access path outside user root via entity path. User: {session.get('username')}, Path: {entity_abs_path}")
        return None

    return entity_abs_path

def find_data_dirs_recursively(start_path, data_subdir_name):
    """Finds all directories named data_subdir_name recursively.

    Uses a seen-set so that each matching directory is returned at most
    once even though the root-level check and os.walk would otherwise
    both find the same path.
    """
    found_dirs = []
    seen = set()   # Track abs-paths to avoid duplicates
    if not os.path.isdir(start_path):
        return found_dirs

    # Walk all subdirectories (os.walk starts at start_path itself, so this
    # also covers the root-level data dir without a separate special check).
    for dirpath, dirnames, _ in os.walk(start_path):
        # Important: Filter dirnames to avoid traversing into unwanted dirs
        # like .git, ssl_venv, hidden dirs, etc.
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]

        if data_subdir_name in dirnames:
            data_dir_path = os.path.join(dirpath, data_subdir_name)
            # Double-check it is actually a directory
            if os.path.isdir(data_dir_path):
                abs_data_dir = os.path.abspath(data_dir_path)
                if abs_data_dir not in seen:
                    found_dirs.append(data_dir_path)
                    seen.add(abs_data_dir)

    return found_dirs


def read_json_lines_from_files(file_paths):
    """Reads JSON objects (one per line) from a list of files."""
    all_items = []
    for file_path in file_paths:
        try:
            if os.path.exists(file_path) and os.path.isfile(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                item = json.loads(line)
                                all_items.append(item)
                            except json.JSONDecodeError as json_e:
                                print(f"Warning: Skipping malformed JSON line in {file_path}: {json_e} - Line: '{line}'")
        except Exception as e:
            print(f"Error reading file {file_path}: {e}")
    return all_items


# Define the path at the top level for clarity
TODO_FILE_PATH = 'data/todo/todo' # Corrected path

# --- Helper function to parse todo lines ---
def parse_todo_line(line, index):
    """Parses a line from todo.txt, attempting JSON then priority|task format."""
    line = line.strip()
    default_priority = 1.0 # Default priority is now 1.0
    
    # Attempt 1: Parse as JSON (legacy format)
    try:
        data = json.loads(line)
        if isinstance(data, dict):
            task = data.get('text', data.get('task', '')) # Handle 'text' or 'task' key
            priority = float(data.get('priority', default_priority)) # Get priority if exists
            # Ensure ID from JSON is preserved if needed, but API uses index now.
            # We primarily need 'task' and 'priority'.
            return {'id': index, 'priority': max(0.0, min(9.9, priority)), 'task': task}
    except json.JSONDecodeError:
        pass # Not JSON, proceed to next format
    except (ValueError, TypeError):
         print(f"Warning: Error parsing priority from potential JSON: '{line}'. Using default.")
         pass # Error getting priority from JSON, proceed

    # Attempt 2: Parse as priority|task
    try:
        if '|' in line:
            parts = line.split('|', 1)
            p_val = float(parts[0].strip())
            priority = max(0.0, min(9.9, p_val)) # Clamp priority 0.0-9.9
            task = parts[1].strip()
            return {'id': index, 'priority': priority, 'task': task}
    except (ValueError, IndexError):
        # Keep default priority if parsing fails
        print(f"Warning: Could not parse priority|task format from line: '{line}'. Treating as simple task.")
        pass # Fall through to default

    # Default: Treat entire line as task with default priority
    return {'id': index, 'priority': default_priority, 'task': line}

# --- Authentication Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if authenticate_user(username, password):
            session.permanent = True # Use persistent session based on lifetime
            session['username'] = username
            # ensure_user_files_exist(username) # This might be legacy, check usage
            print(f"User '{username}' logged in.")
            return redirect(url_for('index'))
        else:
            error = "Invalid username or password."
            print(f"Failed login attempt for username: '{username}'")
    return render_template('auth/login.html', error=error)

@app.route('/register')
def register():
    return render_template('auth/disabled.html',
                           message="User registration through the web interface is disabled. Please use the command line tool: /los/sys/bin/los-user-manage")

@app.route('/logout')
def logout():
    username = session.pop('username', None)
    if username:
        print(f"User '{username}' logged out.")
    return redirect(url_for('login'))

# --- Main Application Routes ---
@app.route('/')
@login_required
def index():
    # Pass the username as the base agenda title, plus the running LOS version
    # for the LOS Dashboard system-info list.
    return render_template('index.html', agenda_title=session['username'],
                           los_version=get_los_version())

@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

@app.route('/icons/<path:filename>')
def serve_icon(filename):
    """Serve icon files from ssl_server/icons/ directory."""
    icons_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icons')
    return send_from_directory(icons_dir, filename)

# --- File System API Endpoints ---

@app.route('/api/agenda/files', methods=['GET'])
@login_required
def api_get_agenda_files():
    """Get files and non-entity directories for a given entity path."""
    entity_rel_path = request.args.get('path', '')
    user_root = get_user_root_path()
    
    if not user_root:
        return jsonify({"error": "Could not determine user root path"}), 500
    
    # Construct the absolute path
    if entity_rel_path:
        entity_abs_path = os.path.abspath(os.path.join(user_root, entity_rel_path))
    else:
        entity_abs_path = user_root
    
    # Security check
    if not is_safe_path(user_root, entity_abs_path):
        return jsonify({"error": "Invalid path"}), 400
    
    if not os.path.isdir(entity_abs_path):
        return jsonify({"error": "Path is not a directory"}), 400
    
    result = {
        "files": [],
        "directories": []
    }
    
    try:
        for item in os.listdir(entity_abs_path):
            # Skip hidden files/directories
            if item.startswith('.'):
                continue
            
            item_path = os.path.join(entity_abs_path, item)
            if os.path.isfile(item_path):
                result["files"].append({
                    "name": item,
                    "path": os.path.join(entity_rel_path, item) if entity_rel_path else item
                })
            elif os.path.isdir(item_path):
                # Check if this directory is an entity (listed in entities file)
                parent_entities_file = os.path.join(entity_abs_path, 'entities')
                is_entity = False
                
                if os.path.isfile(parent_entities_file):
                    with open(parent_entities_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                entity_name = line.split()[0] if line.split() else ''
                                if entity_name == item:
                                    is_entity = True
                                    break
                
                if not is_entity:
                    result["directories"].append({
                        "name": item,
                        "path": os.path.join(entity_rel_path, item) if entity_rel_path else item,
                        "expanded": False,
                        "files": []
                    })
    except OSError as e:
        print(f"Error reading directory {entity_abs_path}: {e}")
        return jsonify({"error": "Failed to read directory"}), 500
    
    # Sort alphabetically
    result["files"].sort(key=lambda x: x["name"])
    result["directories"].sort(key=lambda x: x["name"])
    
    return jsonify(result)


@app.route('/api/agenda/directory/contents', methods=['GET'])
@login_required
def api_get_directory_contents():
    """Get contents of a non-entity directory."""
    dir_rel_path = request.args.get('path', '')
    user_root = get_user_root_path()
    
    if not user_root:
        return jsonify({"error": "Could not determine user root path"}), 500
    
    # Construct the absolute path
    dir_abs_path = os.path.abspath(os.path.join(user_root, dir_rel_path))
    
    # Security check
    if not is_safe_path(user_root, dir_abs_path):
        return jsonify({"error": "Invalid path"}), 400
    
    if not os.path.isdir(dir_abs_path):
        return jsonify({"error": "Path is not a directory"}), 400
    
    contents = []
    
    try:
        for item in os.listdir(dir_abs_path):
            # Skip hidden files/directories
            if item.startswith('.'):
                continue
            
            item_path = os.path.join(dir_abs_path, item)
            if os.path.isfile(item_path):
                contents.append({
                    "name": item,
                    "path": os.path.join(dir_rel_path, item),
                    "type": "file"
                })
            elif os.path.isdir(item_path):
                contents.append({
                    "name": item,
                    "path": os.path.join(dir_rel_path, item),
                    "type": "directory"
                })
    except OSError as e:
        print(f"Error reading directory {dir_abs_path}: {e}")
        return jsonify({"error": "Failed to read directory"}), 500
    
    # Sort alphabetically
    contents.sort(key=lambda x: x["name"])
    
    return jsonify(contents)


@app.route('/api/file/read', methods=['GET'])
@login_required
def api_read_file():
    """Read the contents of a file."""
    file_rel_path = request.args.get('path', '')
    user_root = get_user_root_path()
    
    if not user_root:
        return jsonify({"error": "Could not determine user root path"}), 500
    
    # Construct the absolute path
    file_abs_path = os.path.abspath(os.path.join(user_root, file_rel_path))
    
    # Security check
    if not is_safe_path(user_root, file_abs_path):
        return jsonify({"error": "Invalid path"}), 400
    
    if not os.path.isfile(file_abs_path):
        return jsonify({"error": "File not found"}), 404
    
    try:
        with open(file_abs_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({
            "content": content,
            "path": file_rel_path,
            "name": os.path.basename(file_rel_path)
        })
    except Exception as e:
        print(f"Error reading file {file_abs_path}: {e}")
        return jsonify({"error": "Failed to read file"}), 500


@app.route('/api/file/write', methods=['POST'])
@login_required
def api_write_file():
    """Write content to a file."""
    data = request.json
    if not data:
        return jsonify({"error": "Invalid request data"}), 400
    
    file_rel_path = data.get('path', '')
    content = data.get('content', '')
    user_root = get_user_root_path()
    
    if not user_root:
        return jsonify({"error": "Could not determine user root path"}), 500
    
    # Construct the absolute path
    file_abs_path = os.path.abspath(os.path.join(user_root, file_rel_path))
    
    # Security check
    if not is_safe_path(user_root, file_abs_path):
        return jsonify({"error": "Invalid path"}), 400
    
    # Ensure the file is within the user's directory
    try:
        os.makedirs(os.path.dirname(file_abs_path), exist_ok=True)
        with open(file_abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"success": True, "path": file_rel_path})
    except Exception as e:
        print(f"Error writing file {file_abs_path}: {e}")
        return jsonify({"error": "Failed to write file"}), 500


# --- Agenda API Endpoints ---

@app.route('/api/agenda/tree', methods=['GET'])
@login_required
def api_get_agenda_tree():
    user_root = get_user_root_path()
    if not user_root:
        return jsonify({"error": "Could not determine user root path"}), 500
    if not os.path.isdir(user_root):
         # If user dir doesn't exist yet (e.g. first login), create it? Or return empty?
         # For now, return empty as the structure doesn't exist.
         print(f"User root directory not found: {user_root}")
         return jsonify([]) # Return empty list, frontend expects a list

    # Scan for children *within* the user's root
    children_data = scan_agenda_tree(user_root)

    # Create the root node representing the user themselves
    root_node = {
        'name': session['username'], # The user's root directory name
        'path': '',                 # Path relative to the user's root is empty
        'children': children_data
    }

    # Return a list containing only the root node
    return jsonify([root_node])


@app.route('/api/agenda/subentities', methods=['GET'])
@login_required
def api_get_subentities():
    """
    Return the immediate sub-entities of a given entity path, ordered by weight
    descending (higher weight = more important).

    Reads the entity's `entities` file (one `name weight` line per sub-entity)
    and filters out entries whose directory does not actually exist.
    """
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    entities_file = os.path.join(entity_abs_path, 'entities')
    subs = []
    if os.path.isfile(entities_file):
        try:
            with open(entities_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split()
                    if not parts:
                        continue
                    name = parts[0]
                    weight = 100
                    if len(parts) > 1:
                        try:
                            weight = int(parts[1])
                        except (ValueError, TypeError):
                            pass
                    sub_dir = os.path.join(entity_abs_path, name)
                    if os.path.isdir(sub_dir):
                        subs.append({"name": name, "weight": weight})
        except OSError as e:
            print(f"Error reading entities file {entities_file}: {e}")

    # Higher weight = more important → list first
    subs.sort(key=lambda x: x['weight'], reverse=True)
    return jsonify(subs)


@app.route('/api/entity/context', methods=['GET'])
@login_required
def api_get_entity_context():
    """
    Returns the entity's identity, omni, and goals file contents
    (the core context used when AI mode == 'Entity').
    """
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    return jsonify({
        "path": entity_rel_path,
        "identity": read_safe(os.path.join(entity_abs_path, 'identity')),
        "omni":     read_safe(os.path.join(entity_abs_path, 'omni')),
        "goals":    read_safe(os.path.join(entity_abs_path, 'goals')),
    })


@app.route('/api/agenda/skeleton', methods=['GET'])
@login_required
def api_get_skeleton_data():

    if not os.path.isdir(SKELETON_DIR):
         return jsonify({"error": "Skeleton directory not found on server"}), 500

    skeleton_data = {
        'identity': read_safe(os.path.join(SKELETON_DIR, 'identity')),
        'goals': read_safe(os.path.join(SKELETON_DIR, 'goals')),
        'omni': read_safe(os.path.join(SKELETON_DIR, 'omni'))
    }
    return jsonify(skeleton_data)


@app.route('/api/agenda/create', methods=['POST'])
@login_required
def api_create_subentity():
    user_root = get_user_root_path()
    if not user_root:
        return jsonify({"error": "Could not determine user root path"}), 500

    data = request.json
    if not data:
        return jsonify({"error": "Invalid request data"}), 400

    parent_rel_path = data.get('parent_path', '')
    entity_name = data.get('name')
    identity_content = data.get('identity', '')
    goals_content = data.get('goals', '')
    omni_content = data.get('omni', '')
    weight = data.get('weight', 100)

    # --- Validation ---
    if not entity_name:
        return jsonify({"error": "Sub-entity name is required"}), 400
    if not ALLOWED_ENTITY_NAME_REGEX.match(entity_name):
        return jsonify({"error": "Invalid characters in sub-entity name. Use letters, numbers, underscore, hyphen."}), 400
    if '..' in parent_rel_path or parent_rel_path.startswith('/'):
         return jsonify({"error": "Invalid parent path"}), 400
    try:
        weight = int(weight)
        if weight < 0: raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid weight value"}), 400

    # --- Path Construction ---
    # Ensure parent_rel_path is treated as relative within the user_root
    parent_abs_path = os.path.abspath(os.path.join(user_root, parent_rel_path))
    new_entity_abs_path = os.path.abspath(os.path.join(parent_abs_path, entity_name))
    parent_entities_file = os.path.join(parent_abs_path, 'entities')

    # --- Security and Existence Checks ---
    if not is_safe_path(user_root, parent_abs_path):
        print(f"Security Alert: Attempt to access path outside user root. User: {session['username']}, Path: {parent_abs_path}")
        return jsonify({"error": "Invalid parent path specified"}), 400
    if not os.path.isdir(parent_abs_path):
         # This could happen if the parent path sent from frontend is wrong
         return jsonify({"error": "Parent directory does not exist"}), 404
    if os.path.exists(new_entity_abs_path):
        return jsonify({"error": f"Sub-entity '{entity_name}' already exists at this location"}), 409
    if not os.path.isdir(SKELETON_DIR):
         print(f"Server Configuration Error: Skeleton directory not found at {SKELETON_DIR}")
         return jsonify({"error": "Server configuration error [Skeleton]"}), 500

    # --- Create Entity ---
    try:
        # 1. Copy skeleton
        shutil.copytree(SKELETON_DIR, new_entity_abs_path)

        # 2. Overwrite files with provided content
        write_safe(os.path.join(new_entity_abs_path, 'identity'), identity_content)
        write_safe(os.path.join(new_entity_abs_path, 'goals'), goals_content)
        write_safe(os.path.join(new_entity_abs_path, 'omni'), omni_content)
        # Ensure the copied 'entities' file is initially empty in the new entity
        write_safe(os.path.join(new_entity_abs_path, 'entities'), '')

        # 3. Add to parent's entities file
        entity_line = f"{entity_name} {weight}"
        if not append_safe(parent_entities_file, entity_line):
             # Attempt cleanup? This is tricky. Maybe log the failure.
             print(f"CRITICAL: Failed to update parent entities file '{parent_entities_file}' after creating '{new_entity_abs_path}'. Manual correction might be needed.")
             # Don't necessarily fail the whole request, as the dir is created.
             pass # Logged the error

        print(f"User '{session['username']}' created sub-entity: {new_entity_abs_path}")
        return jsonify({"success": True, "message": f"Sub-entity '{entity_name}' created."}), 201

    except OSError as e:
        print(f"OSError creating sub-entity {new_entity_abs_path}: {e}")
        # Attempt to clean up partially created directory if copy failed midway?
        if os.path.exists(new_entity_abs_path):
            try:
                shutil.rmtree(new_entity_abs_path)
            except Exception as cleanup_e:
                 print(f"Error during cleanup of {new_entity_abs_path}: {cleanup_e}")
        return jsonify({"error": f"Failed to create directory structure: {e}"}), 500
    except Exception as e:
        print(f"Unexpected error creating sub-entity {new_entity_abs_path}: {e}")
        # Attempt cleanup
        if os.path.exists(new_entity_abs_path):
             try:
                 shutil.rmtree(new_entity_abs_path)
             except Exception as cleanup_e:
                 print(f"Error during cleanup of {new_entity_abs_path}: {cleanup_e}")
        return jsonify({"error": "An unexpected error occurred during creation."}), 500


# --- Item Move API (drag-and-drop from calendar/todo → agenda entity) ---

def _find_todo_file_with_id(entity_abs, item_id):
    """Scan todo files under entity_abs; return path of the file that contains item_id."""
    item_id_str = str(item_id)
    todo_dirs = find_data_dirs_recursively(entity_abs, 'todo')
    for todo_dir in todo_dirs:
        for fname in ('todo', 'done'):
            fpath = os.path.join(todo_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped:
                                try:
                                    obj = json.loads(stripped)
                                    if str(obj.get('id')) == item_id_str:
                                        return fpath
                                except json.JSONDecodeError:
                                    pass
                except OSError:
                    pass
    return None


def _move_todo_item(item_id, source_abs, target_abs, source_file,
                    user_root, target_entity_path):
    """Move a todo JSON line from source entity to target entity."""
    # No-op: source and target are the same entity
    if os.path.abspath(source_abs) == os.path.abspath(target_abs):
        print(f"_move_todo_item: source == target ({source_abs}), skipping move")
        return jsonify({"success": True, "skipped": True}), 200

    # Resolve source file
    abs_src = None
    if source_file:
        candidate = os.path.abspath(source_file)
        if is_safe_path(user_root, candidate) and os.path.isfile(candidate):
            abs_src = candidate
    if not abs_src:
        abs_src = _find_todo_file_with_id(source_abs, item_id)
    if not abs_src:
        return jsonify({"error": f"Todo '{item_id}' not found in source entity"}), 404

    # Read and extract the matching line
    found_obj = None
    lines_to_keep = []
    try:
        with open(abs_src, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    try:
                        obj = json.loads(stripped)
                        if isinstance(obj, dict) and str(obj.get('id')) == str(item_id):
                            found_obj = obj
                            continue   # remove from source
                    except json.JSONDecodeError:
                        pass
                lines_to_keep.append(line)
    except Exception as e:
        return jsonify({"error": f"Could not read source todo file: {e}"}), 500

    if not found_obj:
        return jsonify({"error": f"Todo '{item_id}' not found in {abs_src}"}), 404

    # Update the stored entity_path
    found_obj['entity_path'] = target_entity_path

    # Write to target
    target_todo_file = os.path.join(target_abs, 'data', 'todo', 'todo')
    os.makedirs(os.path.dirname(target_todo_file), exist_ok=True)
    if not append_safe(target_todo_file, json.dumps(found_obj)):
        return jsonify({"error": "Failed to write to target todo file"}), 500

    # Remove from source
    write_safe(abs_src, ''.join(lines_to_keep))

    # Move associated project directory (notes, AI history, attachments, etc.)
    src_proj = os.path.join(source_abs, 'data', 'project', str(item_id))
    dst_proj = os.path.join(target_abs, 'data', 'project', str(item_id))
    if os.path.isdir(src_proj):
        if os.path.exists(dst_proj):
            return jsonify({
                "error": (
                    f"Project directory already exists at destination: "
                    f"data/project/{item_id} — please resolve the conflict manually before moving."
                )
            }), 409
        try:
            os.makedirs(os.path.join(target_abs, 'data', 'project'), exist_ok=True)
            shutil.move(src_proj, dst_proj)
            print(f"Moved project dir {src_proj} → {dst_proj}")
        except Exception as e:
            print(f"Warning: could not move project dir {src_proj}: {e}")

    print(f"Moved todo '{item_id}' from {source_abs} → {target_abs}")
    return jsonify({"success": True}), 200


def _move_calendar_item(item_id, source_abs, target_abs,
                        user_root, target_entity_path):
    """Move a calendar event JSON line from source entity to target entity."""
    # No-op: source and target are the same entity
    if os.path.abspath(source_abs) == os.path.abspath(target_abs):
        print(f"_move_calendar_item: source == target ({source_abs}), skipping move")
        return jsonify({"success": True, "skipped": True}), 200

    item_id_str       = str(item_id)
    calendar_data_dir = os.path.join(source_abs, 'data', 'calendar')

    if not os.path.isdir(calendar_data_dir):
        return jsonify({"error": "No calendar data in source entity"}), 404

    found_obj     = None
    src_file_path = None
    lines_to_keep = []

    try:
        for year_dir in os.listdir(calendar_data_dir):
            year_path = os.path.join(calendar_data_dir, year_dir)
            if not os.path.isdir(year_path) or not year_dir.isdigit():
                continue
            for day_file in os.listdir(year_path):
                fpath = os.path.join(year_path, day_file)
                if not os.path.isfile(fpath):
                    continue
                file_keep  = []
                found_here = False
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        for line in f:
                            stripped = line.strip()
                            if stripped:
                                try:
                                    obj = json.loads(stripped)
                                    if isinstance(obj, dict) and str(obj.get('id')) == item_id_str:
                                        found_obj     = obj
                                        src_file_path = fpath
                                        found_here    = True
                                        continue
                                except json.JSONDecodeError:
                                    pass
                            file_keep.append(line)
                except OSError:
                    continue
                if found_here:
                    lines_to_keep = file_keep
                    break
            if found_obj:
                break
    except OSError as e:
        return jsonify({"error": f"Error scanning calendar dir: {e}"}), 500

    if not found_obj:
        return jsonify({"error": f"Calendar event '{item_id}' not found in source entity"}), 404

    # Update entity_rel_path in extendedProps
    if not isinstance(found_obj.get('extendedProps'), dict):
        found_obj['extendedProps'] = {}
    found_obj['extendedProps']['entity_rel_path'] = target_entity_path

    # Determine year/mmdd from the event's start date
    start_str = found_obj.get('start', '')
    try:
        if len(str(start_str)) == 10:
            start_dt = datetime.strptime(str(start_str), '%Y-%m-%d')
        else:
            start_dt = datetime.fromisoformat(str(start_str).replace('Z', '+00:00'))
            if start_dt.tzinfo:
                start_dt = start_dt.astimezone()
    except (ValueError, TypeError):
        start_dt = datetime.now()

    year_str      = start_dt.strftime('%Y')
    month_day_str = start_dt.strftime('%m%d')

    target_cal_file = os.path.join(target_abs, 'data', 'calendar', year_str, month_day_str)
    os.makedirs(os.path.dirname(target_cal_file), exist_ok=True)

    if not append_safe(target_cal_file, json.dumps(found_obj)):
        return jsonify({"error": "Failed to write to target calendar file"}), 500

    # Remove from source
    write_safe(src_file_path, ''.join(lines_to_keep))

    # Move associated project directory (notes, AI history, attachments, etc.)
    src_proj = os.path.join(source_abs, 'data', 'project', item_id_str)
    dst_proj = os.path.join(target_abs, 'data', 'project', item_id_str)
    if os.path.isdir(src_proj):
        if os.path.exists(dst_proj):
            return jsonify({
                "error": (
                    f"Project directory already exists at destination: "
                    f"data/project/{item_id_str} — please resolve the conflict manually before moving."
                )
            }), 409
        try:
            os.makedirs(os.path.join(target_abs, 'data', 'project'), exist_ok=True)
            shutil.move(src_proj, dst_proj)
            print(f"Moved project dir {src_proj} → {dst_proj}")
        except Exception as e:
            print(f"Warning: could not move project dir {src_proj}: {e}")

    print(f"Moved calendar event '{item_id}' from {source_abs} → {target_abs}")
    return jsonify({"success": True}), 200


def _move_cron_item(item_id, source_abs, target_abs, source_file,
                    user_root, target_entity_path):
    """Move a cron entry JSON line from source entity to target entity."""
    # No-op: source and target are the same entity
    if os.path.abspath(source_abs) == os.path.abspath(target_abs):
        print(f"_move_cron_item: source == target ({source_abs}), skipping move")
        return jsonify({"success": True, "skipped": True}), 200

    item_id_str = str(item_id)
    found_obj   = None
    abs_src     = None
    lines_to_keep = []

    # If source_file is specified, search only that file
    if source_file:
        candidate = os.path.abspath(source_file)
        if is_safe_path(user_root, candidate) and os.path.isfile(candidate):
            abs_src = candidate

    if abs_src:
        # Read the specified file
        try:
            with open(abs_src, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        try:
                            obj = json.loads(stripped)
                            if isinstance(obj, dict) and str(obj.get('id')) == item_id_str:
                                found_obj = obj
                                continue
                        except json.JSONDecodeError:
                            pass
                    lines_to_keep.append(line)
        except Exception as e:
            return jsonify({"error": f"Could not read source cron file: {e}"}), 500
    else:
        # Scan all cron dirs/files within the source entity
        cron_dirs = find_data_dirs_recursively(source_abs, 'cron')
        for cron_dir in cron_dirs:
            if found_obj:
                break
            if not os.path.isdir(cron_dir):
                continue
            try:
                for fname in sorted(os.listdir(cron_dir)):
                    if fname.startswith('.') or fname == 'log':
                        continue
                    fpath = os.path.join(cron_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    file_keep  = []
                    found_here = False
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            for line in f:
                                stripped = line.strip()
                                if stripped:
                                    try:
                                        obj = json.loads(stripped)
                                        if isinstance(obj, dict) and str(obj.get('id')) == item_id_str:
                                            found_obj  = obj
                                            abs_src    = fpath
                                            found_here = True
                                            continue
                                    except json.JSONDecodeError:
                                        pass
                                file_keep.append(line)
                    except OSError:
                        continue
                    if found_here:
                        lines_to_keep = file_keep
                        break
            except OSError:
                continue

    if not found_obj:
        return jsonify({"error": f"Cron entry '{item_id}' not found in source entity"}), 404

    # Use the same filename in the target entity (e.g. 'cron', 'cycles', 'birthdays')
    src_filename = os.path.basename(abs_src) if abs_src else 'cron'
    target_cron_file = os.path.join(target_abs, 'data', 'cron', src_filename)
    os.makedirs(os.path.dirname(target_cron_file), exist_ok=True)

    if not append_safe(target_cron_file, json.dumps(found_obj)):
        return jsonify({"error": "Failed to write to target cron file"}), 500

    # Remove from source
    write_safe(abs_src, ''.join(lines_to_keep))

    # Move associated project directory (notes, AI history, attachments, etc.)
    src_proj = os.path.join(source_abs, 'data', 'project', item_id_str)
    dst_proj = os.path.join(target_abs, 'data', 'project', item_id_str)
    if os.path.isdir(src_proj):
        if os.path.exists(dst_proj):
            return jsonify({
                "error": (
                    f"Project directory already exists at destination: "
                    f"data/project/{item_id_str} — please resolve the conflict manually before moving."
                )
            }), 409
        try:
            os.makedirs(os.path.join(target_abs, 'data', 'project'), exist_ok=True)
            shutil.move(src_proj, dst_proj)
            print(f"Moved project dir {src_proj} → {dst_proj}")
        except Exception as e:
            print(f"Warning: could not move project dir {src_proj}: {e}")

    print(f"Moved cron entry '{item_id}' from {source_abs} → {target_abs}")
    return jsonify({"success": True}), 200


@app.route('/api/item/move', methods=['POST'])
@login_required
def api_move_item():
    """Move a calendar event, todo item, or cron entry from one entity to another.

    Body JSON:
      type               – 'calendar', 'todo', or 'cron'
      id                 – item ID
      source_entity_path – relative entity path ('' = user root)
      target_entity_path – relative entity path of destination
      source_file        – (optional) absolute path to the todo source file
    """
    data = request.json
    if not data:
        return jsonify({"error": "Invalid request data"}), 400

    item_type          = data.get('type')
    item_id            = data.get('id')
    source_entity_path = data.get('source_entity_path', '')
    target_entity_path = data.get('target_entity_path', '')
    source_file        = data.get('source_file')

    if item_type not in ('calendar', 'todo', 'cron'):
        return jsonify({"error": "Invalid item type"}), 400
    if not item_id:
        return jsonify({"error": "Item ID is required"}), 400

    user_root  = get_user_root_path()
    source_abs = get_entity_absolute_path(source_entity_path)
    target_abs = get_entity_absolute_path(target_entity_path)

    if not source_abs:
        return jsonify({"error": "Invalid source entity path"}), 400
    if not target_abs:
        return jsonify({"error": "Invalid target entity path"}), 400
    if not os.path.isdir(target_abs):
        return jsonify({"error": "Target entity does not exist"}), 404

    try:
        if item_type == 'todo':
            return _move_todo_item(item_id, source_abs, target_abs,
                                   source_file, user_root, target_entity_path)
        elif item_type == 'cron':
            return _move_cron_item(item_id, source_abs, target_abs,
                                   source_file, user_root, target_entity_path)
        else:
            return _move_calendar_item(item_id, source_abs, target_abs,
                                       user_root, target_entity_path)
    except Exception as e:
        print(f"Unexpected error in api_move_item: {e}")
        return jsonify({"error": str(e)}), 500


# --- Legacy API Endpoints (Todo, Calendar, Crontab) ---
# These might need refactoring to use the new path structure if they
# are meant to operate within specific agendas in the future.
# For now, they likely operate on files directly under /los/<username>/data/

# User data paths helper (Modified: Calendar path is now dynamic)
def get_user_files():
    """Gets paths for non-calendar user data files (e.g., todo, crontab)."""
    if 'username' not in session:
        return None
    # Get base paths, excluding calendar which is handled separately
    paths = get_user_data_paths(session['username'])
    paths.pop('calendar', None) # Remove calendar path if it exists

    # Ensure other files exist (e.g., todo, crontab)
    for file_path in paths.values():
        if file_path and not os.path.exists(file_path):
             try:
                 directory = os.path.dirname(file_path)
                 os.makedirs(directory, exist_ok=True)
                 with open(file_path, 'w', encoding='utf-8') as f:
                     pass # Create empty file
             except Exception as e:
                 print(f"Error ensuring user file exists {file_path}: {e}")
                 # Decide if this should be fatal or just logged

    return paths

# --- Calendar Data Helpers (New Structure) ---

def get_calendar_base_path(username):
    """Returns the base directory path for the user's calendar data."""
    user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, username))
    # Basic check to ensure it's within LOS_BASE_PATH
    if not user_root.startswith(os.path.abspath(LOS_BASE_PATH) + os.sep):
        print(f"Warning: Potential path issue for user '{username}'")
        return None # Or raise an error
    return os.path.join(user_root, 'data', 'calendar')

def get_calendar_file_path(username, start_dt):
    """Determines the correct calendar file path based on the start datetime in local system timezone."""
    if not isinstance(start_dt, datetime):
        raise ValueError("start_dt must be a datetime object")

    base_path = get_calendar_base_path(username)
    if not base_path:
        raise ValueError("Could not determine calendar base path")

    # Ensure the datetime is timezone-aware (assume UTC if naive)
    # Note: FullCalendar typically sends ISO strings which include timezone info or 'Z'
    if start_dt.tzinfo is None or start_dt.tzinfo.utcoffset(start_dt) is None:
         # print(f"Warning: Naive datetime encountered for calendar event: {start_dt}. Assuming UTC.")
         start_dt = start_dt.replace(tzinfo=timezone.utc)

    # Convert to local system timezone for file path determination
    start_dt_local = start_dt.astimezone()

    # Format path components based on local time
    year_str = start_dt_local.strftime('%Y')
    month_day_str = start_dt_local.strftime('%m%d')

    return os.path.join(base_path, year_str, month_day_str)

def delete_event_by_id(entity_abs_path, event_id_to_delete):
    """
    Scans calendar files within a specific entity's data directory
    and removes the event with the matching ID.
    """
    if not entity_abs_path or not os.path.isdir(entity_abs_path):
        print(f"Error: Invalid entity path provided to delete_event_by_id: {entity_abs_path}")
        return False

    calendar_data_dir = os.path.join(entity_abs_path, 'data', 'calendar')
    if not os.path.isdir(calendar_data_dir):
        # It's okay if the calendar data dir doesn't exist for this entity yet
        return False # No calendar data to scan

    found_and_deleted = False
    event_id_str = str(event_id_to_delete) # Ensure comparison is string-based

    try:
        # Scan only within the specific entity's calendar data dir
        for year_dir in os.listdir(calendar_data_dir):
            year_path = os.path.join(calendar_data_dir, year_dir)
            if not os.path.isdir(year_path) or not year_dir.isdigit():
                continue # Skip non-year directories

            for day_file in os.listdir(year_path):
                file_path = os.path.join(year_path, day_file)
                if not os.path.isfile(file_path):
                    continue # Skip non-files

                try:
                    # Read all lines, filter out the event, rewrite if changed
                    lines_to_keep = []
                    file_changed = False
                    original_lines = []
                    if os.path.exists(file_path):
                         with open(file_path, 'r', encoding='utf-8') as f_read:
                            original_lines = f_read.readlines()

                    for line in original_lines:
                        line_stripped = line.strip()
                        if not line_stripped:
                            lines_to_keep.append(line) # Keep empty lines if any
                            continue
                        try:
                            event_data = json.loads(line_stripped)
                            if str(event_data.get('id')) == event_id_str:
                                file_changed = True
                                found_and_deleted = True # Mark as found
                                # print(f"Found event {event_id_str} in {file_path}, removing.")
                            else:
                                lines_to_keep.append(line)
                        except json.JSONDecodeError:
                            lines_to_keep.append(line) # Keep malformed lines

                    # Rewrite the file only if the event was found and removed
                    if file_changed:
                        # print(f"Rewriting {file_path} without event {event_id_str}")
                        with open(file_path, 'w', encoding='utf-8') as f_write:
                            f_write.writelines(lines_to_keep)
                        # If we found it, we can potentially stop scanning,
                        # assuming IDs are unique across all files within this entity.
                        return True # Exit early once found and deleted

                except Exception as e:
                    print(f"Error processing calendar file {file_path}: {e}")
                    # Continue scanning other files in this entity

    except OSError as e:
        print(f"Error scanning calendar directory {calendar_data_dir}: {e}")

    return found_and_deleted # Return whether it was found anywhere in this entity


# --- Helper: Date rule computation functions for cron entries ---

def compute_easter(year):
    """Compute Easter Sunday using the Anonymous Gregorian algorithm (computus)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day)


def compute_nth_weekday(year, month, nth, weekday):
    """Compute the nth occurrence of a weekday in a given month.
    weekday: 0=Monday, 1=Tuesday, ..., 6=Sunday (Python convention)
    nth: 1-based (1st, 2nd, 3rd, 4th, 5th)
    Returns a datetime or None if invalid."""
    try:
        first_day = datetime(year, month, 1)
        first_weekday = first_day.weekday()  # 0=Monday
        days_ahead = weekday - first_weekday
        if days_ahead < 0:
            days_ahead += 7
        first_occurrence = first_day + timedelta(days=days_ahead)
        target = first_occurrence + timedelta(weeks=nth - 1)
        # Verify it's still in the same month
        if target.month != month:
            return None
        return target
    except (ValueError, OverflowError):
        return None


def compute_last_weekday(year, month, weekday):
    """Compute the last occurrence of a weekday in a given month.
    weekday: 0=Monday, 1=Tuesday, ..., 6=Sunday
    Returns a datetime or None if invalid."""
    try:
        last_day_num = cal_module.monthrange(year, month)[1]
        last_day = datetime(year, month, last_day_num)
        while last_day.weekday() != weekday:
            last_day -= timedelta(days=1)
        return last_day
    except (ValueError, OverflowError):
        return None


def resolve_cron_date(entry, year):
    """Resolve the actual date for a cron entry for a given year.
    Handles fixed dates, nth_weekday rules, last_weekday rules, and easter rules.
    Returns a datetime or None."""
    rule = entry.get('rule')

    if rule == 'easter':
        offset = entry.get('offset', 0)
        try:
            easter = compute_easter(year)
            return easter + timedelta(days=offset)
        except (ValueError, OverflowError):
            return None

    elif rule == 'nth_weekday':
        month = entry.get('month')
        nth = entry.get('nth')
        weekday = entry.get('weekday')
        if month is None or nth is None or weekday is None:
            return None
        return compute_nth_weekday(year, month, nth, weekday)

    elif rule == 'last_weekday':
        month = entry.get('month')
        weekday = entry.get('weekday')
        if month is None or weekday is None:
            return None
        return compute_last_weekday(year, month, weekday)

    else:
        # Fixed date (no rule or rule=null)
        month = entry.get('month')
        day = entry.get('day')
        if month is None or day is None:
            return None
        try:
            return datetime(year, month, day)
        except ValueError:
            return None


# --- Helper: Generate virtual calendar events from cron entries ---
def generate_cron_calendar_events(entity_abs_path, start_range=None, end_range=None,
                                  existing_events=None):
    """Generate virtual calendar events from data/cron/ entries for display on the calendar.
    Returns a list of FullCalendar-compatible event dicts.

    `existing_events` (optional): a list of physical calendar events already
    loaded for this view. Any virtual occurrence that coincides (same cron_id
    + same date) with a physical persisted event is skipped so the user's
    manually-edited copy (e.g. status=done) takes precedence and we don't
    double-render the same instance.
    """
    from datetime import date as date_type

    now = datetime.now()
    current_year = now.year

    # Default range: show events for a wide window (current year +/- 1)
    if not start_range:
        start_range = datetime(current_year - 1, 1, 1)
    if not end_range:
        end_range = datetime(current_year + 2, 1, 1)

    # Build a set of (cron_id, YYYY-MM-DD) tuples for physical events that
    # were originally generated from a cron entry. We use these to suppress
    # the matching virtual occurrences.
    persisted_keys = set()
    if existing_events:
        for ev in existing_events:
            if not isinstance(ev, dict):
                continue
            ep = ev.get('extendedProps') or {}
            cid = ep.get('cron_id')
            start_str = ev.get('start')
            if not cid or not start_str:
                continue
            # Take only the date portion (YYYY-MM-DD) for matching, regardless
            # of timezone or all-day/timed format.
            persisted_keys.add((cid, str(start_str)[:10]))


    cron_data_dirs = find_data_dirs_recursively(entity_abs_path, 'cron')
    virtual_events = []

    for cron_dir in cron_data_dirs:
        entries = read_all_cron_entries_from_dir(cron_dir)
        for entry in entries:
            if not entry.get('enabled', True):
                continue

            entry_type = entry.get('type', '')
            title = entry.get('title', 'Untitled')
            description = entry.get('description', '')
            action = entry.get('action', 'display')
            entry_id = entry.get('id', '')
            source_filename = entry.get('source_filename', '')

            # Color coding based on source file / type
            color = '#6c757d'  # default gray
            if 'birthday' in source_filename:
                color = '#e91e63'  # pink
            elif 'holiday' in source_filename:
                if '_us' in source_filename:
                    color = '#1565c0'  # blue
                elif '_mx' in source_filename:
                    color = '#2e7d32'  # green
                else:
                    color = '#7b1fa2'  # purple
            elif entry_type == 'cycle':
                color = '#ff6f00'  # amber
            elif entry_type == 'cron':
                color = '#00838f'  # teal
            elif entry_type == 'date':
                color = '#5e35b1'  # deep purple

            if action == 'execute':
                color = '#d32f2f'  # red for executable entries

            if entry_type == 'date':
                rule = entry.get('rule')
                year = entry.get('year')

                # For non-rule entries, require month and day
                if not rule and (entry.get('month') is None or entry.get('day') is None):
                    continue
                # For rule entries (nth_weekday, last_weekday need month; easter doesn't)
                if rule in ('nth_weekday', 'last_weekday') and entry.get('month') is None:
                    continue

                # Generate events for each year in range
                years_to_check = range(start_range.year, end_range.year + 1)
                if year is not None:
                    years_to_check = [year]

                for y in years_to_check:
                    event_date = resolve_cron_date(entry, y)
                    if event_date is None:
                        continue
                    if start_range <= event_date <= end_range:
                        # Skip if a physical persisted copy exists for this
                        # (cron_id, date) — the user's edited version wins.
                        date_key = event_date.strftime('%Y-%m-%d')
                        if (entry_id, date_key) in persisted_keys:
                            continue

                        # Compute age/anniversary from origin_year if present
                        event_description = description
                        origin_year = entry.get('origin_year')
                        if origin_year is not None:
                            try:
                                years_elapsed = y - int(origin_year)
                                if years_elapsed > 0:
                                    if 'birthday' in source_filename.lower() or 'bday' in entry_id.lower():
                                        event_description = f"Turns {years_elapsed}" + (f" — {description}" if description else "")
                                    else:
                                        ordinal = lambda n: f"{n}{'th' if 11<=n%100<=13 else {1:'st',2:'nd',3:'rd'}.get(n%10,'th')}"
                                        event_description = f"{ordinal(years_elapsed)} anniversary" + (f" — {description}" if description else "")
                                elif years_elapsed == 0 and description:
                                    event_description = description
                            except (ValueError, TypeError):
                                pass

                        # Each occurrence gets a unique instance ID so that marking
                        # one "done" doesn't affect other occurrences of the same entry.
                        # Format: {entry_id}_{YYYYMMDD}  (no dashes, single underscore)
                        instance_id = f"{entry_id}_{date_key.replace('-', '')}"
                        virtual_events.append({
                            'id': instance_id,
                            'title': title,
                            'start': date_key,
                            'allDay': True,
                            'backgroundColor': color,
                            'borderColor': color,
                            'editable': False,
                            'extendedProps': {
                                'description': event_description,
                                'source': 'cron',
                                'cron_id': entry_id,
                                'cron_type': entry_type,
                                'cron_action': action,
                                'source_filename': source_filename
                            }
                        })


            elif entry_type == 'cycle':
                interval = entry.get('interval')
                anchor = entry.get('anchor')
                if not interval or not anchor:
                    continue

                try:
                    anchor_dt = datetime.fromisoformat(anchor)
                except (ValueError, TypeError):
                    continue

                days = interval.get('days', 0)
                hours = interval.get('hours', 0)
                minutes = interval.get('minutes', 0)
                delta = timedelta(days=days, hours=hours, minutes=minutes)

                if delta.total_seconds() <= 0:
                    continue

                is_all_day = (hours == 0 and minutes == 0)

                # The anchor IS the first occurrence. Iterate forward from the
                # anchor by `delta` to generate all occurrences within the
                # visible range. We intentionally do NOT shift the starting
                # point by `last_run` here — that field is used by the cron
                # daemon to decide whether to fire, but for calendar display
                # we always want the anchor (and all past/future occurrences
                # that fall in range) to remain visible.
                #
                # If the anchor falls before `start_range`, fast-forward in
                # whole `delta` steps so we don't iterate uselessly through
                # potentially years of past instances.
                current_dt = anchor_dt
                if current_dt < start_range:
                    skip_steps = int(
                        (start_range - current_dt).total_seconds() // delta.total_seconds()
                    )
                    if skip_steps > 0:
                        current_dt = current_dt + delta * skip_steps

                count = 0
                # Always emit the anchor as the first instance when it falls
                # within the visible range (handled naturally by the loop
                # below since we start `current_dt` at `anchor_dt`).
                while current_dt <= end_range and count < 200:
                    if current_dt >= start_range:
                        date_key = current_dt.strftime('%Y-%m-%d')
                        # Skip if a physical persisted copy of this exact
                        # occurrence already exists (e.g. user marked it
                        # "done" in the editor); the persisted version wins.
                        if (entry_id, date_key) not in persisted_keys:
                            is_anchor = (current_dt == anchor_dt)
                            # Each occurrence gets a unique instance ID so that
                            # marking one "done" doesn't interfere with other
                            # occurrences of the same cycle entry.
                            # Format: {entry_id}_{YYYYMMDD} or {entry_id}_{YYYYMMDD}_{HHMM}
                            if is_all_day:
                                instance_id = f"{entry_id}_{date_key.replace('-', '')}"
                                start_value = date_key
                            else:
                                instance_id = f"{entry_id}_{current_dt.strftime('%Y%m%d_%H%M')}"
                                start_value = current_dt.isoformat()
                            event_data = {
                                'id': instance_id,
                                'title': f'🔄 {title}',
                                'start': start_value,
                                'allDay': is_all_day,
                                'backgroundColor': color,
                                'borderColor': color,
                                'editable': False,
                                'extendedProps': {
                                    'description': description,
                                    'source': 'cron',
                                    'cron_id': entry_id,
                                    'cron_type': entry_type,
                                    'cron_action': action,
                                    'source_filename': source_filename,
                                    'is_anchor': is_anchor
                                }
                            }
                            virtual_events.append(event_data)
                    current_dt += delta
                    count += 1

    return virtual_events


# Calendar API endpoints
@app.route('/api/calendar', methods=['GET'])
@login_required
def get_calendar():
    """Gets calendar events recursively from the specified entity path.
    Also includes virtual events generated from data/cron/ entries."""
    username = session.get('username')
    entity_rel_path = request.args.get('path', '') # Get path from query param
    include_cron = request.args.get('include_cron', 'true').lower() != 'false'

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
         # Handle root path error or invalid path
         return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    print(f"Fetching calendar data for user '{username}', path: '{entity_rel_path}' -> '{entity_abs_path}'")

    calendar_data_dirs = find_data_dirs_recursively(entity_abs_path, 'calendar')

    all_event_files = []
    for data_dir in calendar_data_dirs:
        # Calendar files are nested under YYYY/MMDD
        try:
            if not os.path.isdir(data_dir): continue # Skip if somehow not a dir
            for year_dir in os.listdir(data_dir):
                year_path = os.path.join(data_dir, year_dir)
                if os.path.isdir(year_path) and year_dir.isdigit():
                    for day_file in os.listdir(year_path):
                        day_file_path = os.path.join(year_path, day_file)
                        if os.path.isfile(day_file_path):
                            all_event_files.append(day_file_path)
        except OSError as e:
            print(f"Error scanning calendar data directory {data_dir}: {e}")

    # Read events and stamp each with the sub-entity relative path it came from,
    # so the client knows where to save / display the event's origin.
    # Each day-file path is: <entity>/data/calendar/<year>/<MMDD>  → 4 levels up = entity dir
    _user_root = os.path.abspath(f'/los/{username}')
    all_events = []
    for _fpath in all_event_files:
        _entity_dir = os.path.normpath(os.path.join(_fpath, '..', '..', '..', '..'))
        _entity_rel = os.path.relpath(_entity_dir, _user_root)
        if _entity_rel == '.':
            _entity_rel = ''
        try:
            with open(_fpath, 'r', encoding='utf-8') as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _event = json.loads(_line)
                        if isinstance(_event, dict):
                            if not isinstance(_event.get('extendedProps'), dict):
                                _event['extendedProps'] = {}
                            _event['extendedProps']['entity_rel_path'] = _entity_rel
                            all_events.append(_event)
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    # Add virtual events from cron data, passing the physical events so any
    # virtual occurrence that has already been persisted (e.g. user marked
    # a specific instance "done" via the editor) is suppressed in favour of
    # the persisted version.
    if include_cron:
        try:
            cron_events = generate_cron_calendar_events(
                entity_abs_path, existing_events=all_events
            )
            all_events.extend(cron_events)
        except Exception as e:
            print(f"Error generating cron calendar events: {e}")

    return jsonify(all_events)


@app.route('/api/calendar', methods=['POST'])
@login_required
def add_or_update_calendar():
    """Adds or updates a calendar event in the specified entity path."""
    username = session.get('username')
    entity_rel_path = request.args.get('path', '') # Get path from query param

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
         return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    event_data = request.json
    if not event_data or not isinstance(event_data, dict) or 'id' not in event_data or 'start' not in event_data:
        return jsonify({"error": "Invalid event data provided (requires at least 'id' and 'start')"}), 400

    # --- Validate Optional Fields ---
    if 'extendedProps' in event_data and isinstance(event_data['extendedProps'], dict):
        delegatable_score = event_data['extendedProps'].get('delegatable_score')
        if delegatable_score is not None:
            try:
                d_score = float(delegatable_score)
                # Clamp 0.0-1.0 and update in the original dict
                event_data['extendedProps']['delegatable_score'] = max(0.0, min(1.0, d_score))
            except (ValueError, TypeError):
                print(f"Warning: Invalid delegatable_score received for event {event_data.get('id')}: {delegatable_score}. Setting to null.")
                event_data['extendedProps']['delegatable_score'] = None
        
        # Handle status for calendar events
        # Match allowed statuses with the UI dropdown
        allowed_calendar_statuses = ['new', 'in_progress', 'blocked', 'delegated', 'done', 'archived', 'tentative', 'confirmed', 'cancelled', 'completed']
        current_status = event_data['extendedProps'].get('status')
        if current_status is not None and current_status not in allowed_calendar_statuses:
            print(f"Warning: Invalid status '{current_status}' for calendar event {event_data.get('id')}. Setting to 'tentative'.")
            event_data['extendedProps']['status'] = 'tentative'
        elif current_status is None:
             event_data['extendedProps']['status'] = 'tentative' # Default if not provided

    # --- Determine Target File Path within the specific entity and Convert Timestamps to Local Timezone ---
    try:
        start_iso = event_data.get('start')

        # Detect all-day events: FullCalendar sends a plain "YYYY-MM-DD" date
        # string (no time component) for all-day events, including virtual cron
        # occurrences.  Applying timezone conversion to a bare date would
        # incorrectly roll it back to the previous day for UTC− timezones.
        is_all_day_event = (
            event_data.get('allDay', False)
            or (isinstance(start_iso, str) and len(start_iso) == 10 and 'T' not in start_iso)
        )

        if is_all_day_event:
            # Parse as a local date — no timezone conversion needed.
            start_dt_local = datetime.strptime(start_iso[:10], '%Y-%m-%d')
            # Preserve the plain date string (no timezone suffix) in stored data
            event_data['start'] = start_iso[:10]
            # For all-day events the end is also a plain date or absent; keep as-is
            if 'end' in event_data and event_data.get('end'):
                event_data['end'] = str(event_data['end'])[:10]
        else:
            start_dt = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))

            # Ensure the datetime is timezone-aware (assume UTC if naive)
            if start_dt.tzinfo is None or start_dt.tzinfo.utcoffset(start_dt) is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)

            # Convert to local system timezone for file path determination and storage
            start_dt_local = start_dt.astimezone()

            # Update the event data to store timestamps in local timezone
            event_data['start'] = start_dt_local.isoformat()

            # Handle end timestamp if present
            if 'end' in event_data and event_data.get('end'):
                end_iso = event_data['end']
                end_dt = datetime.fromisoformat(end_iso.replace('Z', '+00:00'))
                if end_dt.tzinfo is None or end_dt.tzinfo.utcoffset(end_dt) is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                end_dt_local = end_dt.astimezone()
                event_data['end'] = end_dt_local.isoformat()

        # Format path components based on local date
        year_str = start_dt_local.strftime('%Y')
        month_day_str = start_dt_local.strftime('%m%d')
        calendar_data_dir = os.path.join(entity_abs_path, 'data', 'calendar')
        target_file_path = os.path.join(calendar_data_dir, year_str, month_day_str)

        # Security check (redundant if get_entity_absolute_path is trusted, but safe)
        if not is_safe_path(entity_abs_path, os.path.dirname(target_file_path)):
             print(f"Security Alert: Calendar file path escape attempt. User: {username}, Entity: {entity_abs_path}, Target: {target_file_path}")
             raise ValueError("Invalid target file path generated")

    except (ValueError, TypeError) as e:
        print(f"Error parsing event start time or getting file path: {e}")
        return jsonify({"error": "Invalid start date format or unable to determine file path"}), 400

    # --- First, delete the event from anywhere in the entity (handles date changes) ---
    event_id_to_update = str(event_data['id'])
    deleted_old = delete_event_by_id(entity_abs_path, event_id_to_update)
    if deleted_old:
        print(f"Removed existing event {event_id_to_update} from previous location in entity '{entity_rel_path}' before relocating.")

    # --- Read Existing Data & Add to Target (since old was removed, just add) ---
    # Serialize the potentially modified event_data
    event_json_str = json.dumps(event_data)
    lines_to_write = []

    try:
        # Ensure directory exists before reading/writing
        os.makedirs(os.path.dirname(target_file_path), exist_ok=True)

        # Read existing lines from the target file (should not contain this event now)
        if os.path.exists(target_file_path):
            with open(target_file_path, 'r', encoding='utf-8') as f_read:
                lines_to_write = f_read.readlines()  # Keep all existing lines

        # Add the event as a new line
        lines_to_write.append(event_json_str + '\n')
        print(f"Event ID {event_id_to_update} added/updated in {target_file_path} for entity '{entity_rel_path}'")

        # --- Write Updated Data Back to File ---
        with open(target_file_path, 'w', encoding='utf-8') as f_write:
            f_write.writelines(lines_to_write)

        print(f"Saved event {event_id_to_update} to {target_file_path}")
        return jsonify({"success": True, "event": event_data}), 200 # OK status for update/add

    except Exception as e:
        print(f"Error adding/updating calendar event in {target_file_path}: {e}")
        return jsonify({"error": "Failed to save calendar event"}), 500


@app.route('/api/calendar/<event_id>', methods=['DELETE'])
@login_required
def delete_calendar(event_id):
    """Deletes a calendar event by its ID from the specified entity path."""
    username = session.get('username') # Still needed for logging/context
    entity_rel_path = request.args.get('path', '') # Get path from query param

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
         return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    # Call the modified helper function to scan and delete within the entity path
    deleted = delete_event_by_id(entity_abs_path, event_id)

    if deleted:
        print(f"Deleted calendar event ID {event_id} within path '{entity_rel_path}' for user '{username}'")
        return jsonify({"success": True}), 200
    else:
        print(f"Calendar event ID {event_id} not found within path '{entity_rel_path}' for user '{username}'")
        return jsonify({"error": "Event not found within the specified path"}), 404


# --- Todo API endpoints ---
@app.route('/api/todos', methods=['GET'])
@login_required
def get_todos():
    """Gets todo items (in JSON format) recursively from the specified entity path."""
    username = session.get('username')
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
         return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    print(f"Fetching todo data for user '{username}', path: '{entity_rel_path}' -> '{entity_abs_path}'")

    todo_data_dirs = find_data_dirs_recursively(entity_abs_path, 'todo')

    all_todo_files = []
    for data_dir in todo_data_dirs:
        todo_file = os.path.join(data_dir, 'todo') # Specific filename
        if os.path.isfile(todo_file):
            all_todo_files.append(todo_file)

    print(f"DEBUG: get_todos found potential todo files: {all_todo_files}")

    user_root = get_user_root_path()
    all_todos = []
    for todo_file in all_todo_files:
        # Compute the entity path (relative to user root) that owns this todo file.
        # File layout is: <entity>/data/todo/todo  →  3 levels up from the todo file = entity dir.
        try:
            entity_dir = os.path.normpath(os.path.join(todo_file, '..', '..', '..'))
            entity_rel_for_file = os.path.relpath(entity_dir, user_root) if user_root else ''
            if entity_rel_for_file == '.':
                entity_rel_for_file = ''
        except Exception as _e:
            entity_rel_for_file = ''

        try:
            with open(todo_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1): # Add line number for logging
                    line = line.strip()
                    if line:
                        try:
                            # Attempt to parse as JSON
                            parsed_todo = json.loads(line)
                            # Basic validation: must be a dict and have an 'id'
                            if isinstance(parsed_todo, dict) and 'id' in parsed_todo:
                                parsed_todo['source_file'] = todo_file # Add source file path
                                # Stamp each todo with the relative entity path it belongs to,
                                # so the client can edit/save/notes in the correct entity even
                                # when todos are aggregated from sub-entities.
                                parsed_todo['entity_path'] = entity_rel_for_file
                                all_todos.append(parsed_todo)
                            else:
                                print(f"Warning: Skipping invalid JSON object (not dict or missing 'id') in {todo_file}:{line_num}")
                        except json.JSONDecodeError:
                            # Log and skip lines that are not valid JSON (old format or corrupted)
                            print(f"Warning: Skipping non-JSON line in {todo_file}:{line_num}")
                        except Exception as parse_e:
                            # Catch other potential errors during processing
                            print(f"Error processing line in {todo_file}:{line_num}: {parse_e}")
        except Exception as e:
            print(f"Error reading todo file {todo_file}: {e}")


    print(f"DEBUG: get_todos parsed {len(all_todos)} valid JSON todo items from files.")

    # Sort by priority (ascending, 0.0 is highest)
    # Use a default high value for items missing priority (shouldn't happen with validation)
    all_todos.sort(key=lambda item: item.get('priority', 10.0))

    print(f"DEBUG: get_todos returning sorted: {all_todos}") # Log the sorted data
    return jsonify(all_todos)


@app.route('/api/todos', methods=['POST'])
@login_required
def todo_add():
    """Adds a new todo item in JSON format to the specified entity path."""
    data = request.json
    print(f"Received data in todo_add: {data}")

    if not data or 'text' not in data or not data['text'].strip():
        # Changed 'task' to 'text' for consistency with new structure
        return jsonify({"error": "'text' content is required"}), 400

    # Get the relative path for the entity from the request
    entity_rel_path = data.get('path', '') # Expecting path in the JSON body

    # --- Extract data and set defaults ---
    text = data['text'].strip()
    priority = 1.0 # Default priority
    status = data.get('status', 'new').strip() or 'new' # Default status
    assigned_to = data.get('assigned_to')
    deadline = data.get('deadline') # Expecting ISO format string or null
    delegatable_score = data.get('delegatable_score') # Expecting float or null

    # Validate and clamp priority
    if 'priority' in data:
        try:
            p_val = float(data['priority'])
            priority = max(0.0, min(9.99, p_val)) # Clamp 0.0-9.99
        except (ValueError, TypeError):
             print(f"Warning: Invalid priority value received: {data['priority']}. Using default 1.0.")

    # Validate delegatable_score (if provided)
    if delegatable_score is not None:
        try:
            d_score = float(delegatable_score)
            delegatable_score = max(0.0, min(1.0, d_score)) # Clamp 0.0-1.0
        except (ValueError, TypeError):
            print(f"Warning: Invalid delegatable_score received: {delegatable_score}. Setting to null.")
            delegatable_score = None

    # --- Generate new fields ---
    now = datetime.now(timezone.utc)
    # Generate a more unique ID using timestamp and random bytes
    # Format: todo_YYYYMMDD_HHMMSS_timestamp
    timestamp_str = now.strftime('%Y%m%d_%H%M%S')
    new_id = f"todo_{timestamp_str}_{int(now.timestamp())}"
    created_at = now.isoformat()

    # --- Construct the new Todo object ---
    new_todo_obj = {
        "id": new_id,
        "text": text,
        "priority": priority,
        "status": status,
        "created_at": created_at,
        "completed_at": None, # Always None on creation
        "assigned_to": assigned_to,
        "deadline": deadline,
        "delegatable_score": delegatable_score
    }

    # --- Save to file ---
    target_todo_file = get_entity_data_file_path(entity_rel_path, 'todo', 'todo')

    if not target_todo_file:
         print(f"ERROR: todo_add could not determine target file path for entity '{entity_rel_path}'")
         return jsonify({"error": "Invalid entity path specified for saving todo"}), 400

    # Convert object to JSON string for saving
    line_to_add = json.dumps(new_todo_obj)

    # --- Prepend newline if file exists and is not empty ---
    prefix = ""
    try:
        # Check if file exists and has size > 0
        if os.path.exists(target_todo_file) and os.path.getsize(target_todo_file) > 0:
            prefix = "\n" # Add the extra newline separator
    except OSError as e:
        print(f"Warning: Could not check size of {target_todo_file}: {e}")
        # Proceed without prefix if size check fails

    # Combine prefix and line
    final_line_to_add = prefix + line_to_add

    print(f"DEBUG: todo_add attempting to append final line to file: {target_todo_file}")
    success = append_safe(target_todo_file, final_line_to_add) # Pass the combined line
    # Note: append_safe will still add its own trailing newline

    print(f"DEBUG: todo_add append_safe result for {target_todo_file}: {success}")

    if success:
        # Return the newly created object
        return jsonify(new_todo_obj), 201
    else:
        # append_safe already prints error
        print(f"ERROR: todo_add failed to append to {target_todo_file}")
        return jsonify({"error": "Failed to save todo"}), 500


# New route for deleting by specific file and task content
# --- Todo Delete (New ID-based) ---
@app.route('/api/todos/<todo_id>', methods=['DELETE'])
@login_required
def delete_todo(todo_id):
    """Deletes a todo item by its unique ID from the specified source file."""
    source_file_path = request.args.get('source_file', None)
    username = session.get('username') # For validation/logging

    if not source_file_path:
        return jsonify({"error": "Missing 'source_file' query parameter"}), 400
    if not todo_id:
         return jsonify({"error": "Missing 'todo_id' in URL path"}), 400 # Should be caught by Flask routing

    # --- Security Validation ---
    user_root = get_user_root_path()
    if not user_root:
         return jsonify({"error": "Authentication error or invalid user session"}), 401

    abs_source_file_path = os.path.abspath(source_file_path)
    if not is_safe_path(user_root, abs_source_file_path):
         print(f"SECURITY ALERT: Attempt to delete todo from unsafe path. User: {username}, Path: {abs_source_file_path}")
         return jsonify({"error": "Invalid file path specified"}), 403 # Forbidden

    if not os.path.isfile(abs_source_file_path):
         print(f"ERROR: delete_todo target file not found: {abs_source_file_path}")
         return jsonify({"error": "Specified todo file not found"}), 404

    # --- Read, Filter, and Rewrite ---
    try:
        lines_to_keep = []
        found_and_deleted = False
        deleted_line_content = "" # For logging

        with open(abs_source_file_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines() # Read all lines first

        for i, line in enumerate(all_lines):
            line_stripped = line.strip()
            if not line_stripped:
                lines_to_keep.append(line) # Preserve empty lines if needed
                continue

            try:
                # Attempt to parse as JSON
                parsed_todo = json.loads(line_stripped)
                if isinstance(parsed_todo, dict) and str(parsed_todo.get('id')) == str(todo_id):
                    # Found the item to delete, DO NOT add it to lines_to_keep
                    found_and_deleted = True
                    deleted_line_content = line_stripped # For logging
                    print(f"DEBUG: delete_todo found matching ID '{todo_id}' at line {i+1} in {abs_source_file_path}. Marking for deletion.")
                else:
                    # Not the item we're looking for, keep it
                    lines_to_keep.append(line)
            except json.JSONDecodeError:
                # Keep lines that are not valid JSON (old format or corrupted)
                lines_to_keep.append(line)
            except Exception as parse_e:
                 # Keep line if other parsing error occurs, but log it
                 print(f"Error parsing line {i+1} in {abs_source_file_path} during delete: {parse_e}. Keeping line.")
                 lines_to_keep.append(line)

        # --- Check if found and proceed with writing ---
        if not found_and_deleted:
            print(f"ERROR: delete_todo did not find todo with ID '{todo_id}' in {abs_source_file_path}")
            return jsonify({"error": "Todo item with specified ID not found in the file"}), 404

        # Rewrite the file with the filtered lines
        content_to_write = "".join(lines_to_keep)
        if write_safe(abs_source_file_path, content_to_write):
            print(f"DEBUG: delete_todo successfully deleted todo ID '{todo_id}' ('{deleted_line_content}') from {abs_source_file_path}.")
            return jsonify({"success": True, "message": "Todo deleted."}), 200
        else:
            print(f"ERROR: delete_todo failed to rewrite file {abs_source_file_path} after deleting todo ID '{todo_id}'")
            # This is a critical error state, the file might be empty or corrupted
            return jsonify({"error": "Failed to update todo file after deletion"}), 500

    except Exception as e:
        print(f"Error processing delete_todo request for ID '{todo_id}', file '{abs_source_file_path}': {e}")
        return jsonify({"error": "An unexpected error occurred during deletion."}), 500
@app.route('/api/todos/<todo_id>', methods=['PUT'])
@login_required
def update_todo(todo_id):
    """Updates an existing todo item in the specified entity path."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    # The frontend now sends the full updated object, including the entity path
    # where the todo resides. We need this to find the correct file.
    entity_rel_path = data.get('entity_path')
    print(f"DEBUG update_todo: id={todo_id} entity_path={entity_rel_path!r}")
    if entity_rel_path is None: # Allow empty string for root, but not missing key
        print(f"DEBUG update_todo: 400 - entity_path key missing from body. Keys present: {list(data.keys())}")
        return jsonify({"error": "Missing 'entity_path' in request body"}), 400

    # Validate entity_path format (basic check)
    if '..' in entity_rel_path or entity_rel_path.startswith('/'):
        print(f"DEBUG update_todo: 400 - invalid entity_path format: {entity_rel_path!r}")
        return jsonify({"error": "Invalid entity path format"}), 400

    todo_file_path = get_entity_data_file_path(entity_rel_path, 'todo', 'todo')
    if not todo_file_path:
        print(f"DEBUG update_todo: 400 - could not construct todo file path for entity_path={entity_rel_path!r}")
        return jsonify({"error": "Invalid entity path or could not construct todo file path"}), 400
    if not os.path.exists(os.path.dirname(todo_file_path)):
         # If the directory structure doesn't exist, we can't update a file within it
         print(f"Warning: Attempted to update todo in non-existent directory: {os.path.dirname(todo_file_path)}")
         return jsonify({"error": "Entity data directory not found"}), 404


    # --- Read existing todos ---
    try:
        # read_data reads JSON strings per line
        existing_todos_json = read_data(todo_file_path)
        existing_todos = []
        for index, json_str in enumerate(existing_todos_json):
            try:
                todo = json.loads(json_str)
                # Ensure essential fields exist, especially 'id'
                if 'id' not in todo:
                    print(f"Warning: Todo item at index {index} in {todo_file_path} is missing an ID. Skipping.")
                    continue
                existing_todos.append(todo)
            except json.JSONDecodeError:
                print(f"Warning: Skipping malformed JSON line in {todo_file_path}: {json_str}")
                continue
    except Exception as e:
        print(f"Error reading todo file {todo_file_path} for update: {e}")
        return jsonify({"error": "Failed to read todo data"}), 500


    # --- Find and update the specific todo ---
    found_index = -1
    for i, todo in enumerate(existing_todos):
         # Compare IDs as strings, as incoming todo_id is string from URL
        if str(todo.get('id')) == str(todo_id):
            found_index = i
            break

    if found_index == -1:
        print(f"Error: update_todo did not find todo with ID '{todo_id}' in {todo_file_path}")
        return jsonify({"error": "Todo not found"}), 404

    # --- Validate and Update ---
    original_todo = existing_todos[found_index]
    # The incoming 'data' is the full updated object from the frontend
    updated_todo = data

    # Basic Validation (add more as needed)
    allowed_statuses = ['new', 'in_progress', 'blocked', 'delegated', 'done', 'archived', 'confirmed', 'tentative', 'cancelled']
    if 'status' in updated_todo and updated_todo['status'] is not None and updated_todo['status'] not in allowed_statuses:
        return jsonify({"error": f"Invalid status value: {updated_todo['status']}"}), 400

    # If status is null/None in the update, preserve the original status
    if updated_todo.get('status') is None:
        updated_todo['status'] = original_todo.get('status')

    # Ensure critical fields are not lost (like id, timestamp)
    # Frontend should send these, but we enforce keeping the original ID and timestamp
    updated_todo['id'] = original_todo['id'] # Keep original ID
    updated_todo['timestamp'] = original_todo.get('timestamp') # Keep original creation timestamp
    # Ensure lastModified is updated server-side for consistency
    updated_todo['lastModified'] = datetime.now(timezone.utc).isoformat()

    # Update the list in memory
    existing_todos[found_index] = updated_todo

    # --- Write back the updated list ---
    try:
        # Convert back to list of JSON strings for write_data
        updated_todos_json = [json.dumps(todo) for todo in existing_todos]
        if not write_data(todo_file_path, updated_todos_json):
             raise IOError("write_data function returned false")
    except Exception as e:
        print(f"Error writing updated todo file {todo_file_path}: {e}")
        # Consider rolling back the in-memory change? Difficult state.
        return jsonify({"error": "Failed to save updated todo data"}), 500

    print(f"User '{session['username']}' updated todo ID {todo_id} in entity '{entity_rel_path}'")
    # Return the fully updated todo object (as saved) as confirmation
    return jsonify(updated_todo), 200

@app.route('/api/todos/<todo_id>/status', methods=['PUT'])
@login_required
def update_todo_status(todo_id):
    """Updates the status of a todo item and moves it to a 'done' file if completed."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    new_status = data.get('status')
    source_file_path_from_request = data.get('source_file') # Expecting full path from client
    username = session.get('username')

    if not new_status:
        return jsonify({"error": "Missing 'status' in request body"}), 400
    if not source_file_path_from_request:
        return jsonify({"error": "Missing 'source_file' in request body"}), 400

    # --- Security Validation for source_file_path ---
    user_root = get_user_root_path()
    if not user_root:
        return jsonify({"error": "Authentication error or invalid user session"}), 401

    abs_source_file_path = os.path.abspath(source_file_path_from_request)
    if not is_safe_path(user_root, abs_source_file_path):
        print(f"SECURITY ALERT: Attempt to update todo status from unsafe path. User: {username}, Path: {abs_source_file_path}")
        return jsonify({"error": "Invalid file path specified"}), 403

    if not os.path.isfile(abs_source_file_path):
        print(f"ERROR: update_todo_status target file not found: {abs_source_file_path}")
        return jsonify({"error": "Specified todo file not found"}), 404

    # --- Read, Find, Update, and Move ---
    try:
        lines_to_keep_in_original = []
        item_to_move = None
        found = False

        with open(abs_source_file_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()

        for line in all_lines:
            line_stripped = line.strip()
            if not line_stripped:
                lines_to_keep_in_original.append(line) # Preserve empty/formatting lines
                continue
            try:
                todo = json.loads(line_stripped)
                if isinstance(todo, dict) and str(todo.get('id')) == str(todo_id):
                    found = True
                    if new_status == 'completed':
                        todo['status'] = 'completed'
                        todo['completed_at'] = datetime.now(timezone.utc).isoformat()
                        todo['lastModified'] = todo['completed_at'] # Also update lastModified
                        item_to_move = todo
                        # This item will NOT be added to lines_to_keep_in_original
                    else:
                        # For other status updates (not 'completed'), update in place
                        todo['status'] = new_status
                        todo['lastModified'] = datetime.now(timezone.utc).isoformat()
                        lines_to_keep_in_original.append(json.dumps(todo) + '\n')
                        # If we were to support non-completed status updates here,
                        # we'd return the updated item. For now, focusing on 'completed'.
                else:
                    lines_to_keep_in_original.append(line)
            except json.JSONDecodeError:
                lines_to_keep_in_original.append(line) # Keep malformed lines

        if not found:
            return jsonify({"error": "Todo item not found"}), 404

        # --- Handle 'completed' status: Move to done.txt and rewrite original ---
        if new_status == 'completed' and item_to_move:
            # Determine done_file_path (e.g., .../data/todo/done)
            source_dir = os.path.dirname(abs_source_file_path)
            done_file_path = os.path.join(source_dir, 'done') # Name of the done file

            # Security check for done_file_path (should be within user_root)
            if not is_safe_path(user_root, done_file_path):
                 print(f"SECURITY ALERT: Attempt to write done file to unsafe path. User: {username}, Path: {done_file_path}")
                 return jsonify({"error": "Invalid destination path for completed item"}), 500

            # Append the completed item to done.txt
            # Ensure done.txt directory exists (append_safe handles this)
            if not append_safe(done_file_path, json.dumps(item_to_move)):
                print(f"ERROR: Failed to append completed todo ID '{todo_id}' to {done_file_path}")
                return jsonify({"error": "Failed to move item to completed file"}), 500
            print(f"Moved todo ID '{todo_id}' to {done_file_path}")

            # Rewrite the original todo file without the completed item
            content_for_original_file = "".join(lines_to_keep_in_original)
            if not write_safe(abs_source_file_path, content_for_original_file):
                print(f"CRITICAL ERROR: Failed to rewrite original todo file {abs_source_file_path} after moving item ID '{todo_id}'. Data may be inconsistent.")
                # This is a problematic state. The item is in done.txt but might still be in original.
                return jsonify({"error": "Failed to update original todo file. Please check data consistency."}), 500
            
            print(f"Successfully updated status for todo ID '{todo_id}' to '{new_status}' and moved to done file.")
            return jsonify({"success": True, "message": "Todo marked as completed and moved."}), 200
        
        elif found: # Item was found but status was not 'completed' (future use)
            # This part is for if we extend to update status to something other than 'completed'
            # For now, the frontend only calls this for 'completed'
            # Rewrite the original file with the updated (but not completed) item
            content_for_original_file = "".join(lines_to_keep_in_original)
            if not write_safe(abs_source_file_path, content_for_original_file):
                print(f"ERROR: Failed to rewrite {abs_source_file_path} for non-completed status update of todo ID '{todo_id}'.")
                return jsonify({"error": "Failed to update todo file for status change."}), 500
            
            # Find the updated item to return it (the one that was modified in lines_to_keep_in_original)
            updated_item_for_response = None
            for line_str in lines_to_keep_in_original:
                stripped_line = line_str.strip()
                if stripped_line:
                    try:
                        potential_item = json.loads(stripped_line)
                        if isinstance(potential_item, dict) and str(potential_item.get('id')) == str(todo_id):
                            updated_item_for_response = potential_item
                            break
                    except json.JSONDecodeError:
                        continue
            
            print(f"Successfully updated status for todo ID '{todo_id}' to '{new_status}'.")
            return jsonify({"success": True, "message": f"Todo status updated to {new_status}.", "todo": updated_item_for_response}), 200

        # Should not be reached if logic is correct
        return jsonify({"error": "An unexpected state was reached during status update."}), 500

    except Exception as e:
        print(f"Error updating todo status for ID '{todo_id}', file '{abs_source_file_path}': {e}")
        return jsonify({"error": "An unexpected error occurred during status update."}), 500


# --- End of Todo Delete ---


# Removed update_todo function and route as it's complex with line-based storage
# and wasn't part of the requested update.


# --- Crontab API endpoints (Enhanced: reads all files in data/cron/) ---

def read_all_cron_entries_from_dir(cron_data_dir):
    """Read all cron entries from ALL files in a data/cron/ directory.
    Returns list of dicts with source_file added to each entry."""
    entries = []
    if not os.path.isdir(cron_data_dir):
        return entries

    try:
        for filename in sorted(os.listdir(cron_data_dir)):
            if filename.startswith('.') or filename == 'log':
                continue
            filepath = os.path.join(cron_data_dir, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line_stripped = line.strip()
                        if not line_stripped or line_stripped.startswith('#'):
                            continue
                        try:
                            entry = json.loads(line_stripped)
                            if isinstance(entry, dict) and 'id' in entry:
                                entry['source_file'] = filepath
                                entry['source_filename'] = filename
                                entries.append(entry)
                        except json.JSONDecodeError:
                            pass  # skip non-JSON lines (comments, etc.)
            except OSError as e:
                print(f"Error reading cron file {filepath}: {e}")
    except OSError as e:
        print(f"Error listing cron directory {cron_data_dir}: {e}")
    return entries


def find_and_modify_cron_entry(entity_abs_path, entry_id, operation='delete', new_data=None):
    """Find a cron entry by ID across all files in all data/cron/ dirs recursively.
    operation: 'delete' or 'update'
    Returns (success_bool, found_file_path_or_None)"""
    cron_data_dirs = find_data_dirs_recursively(entity_abs_path, 'cron')

    for cron_dir in cron_data_dirs:
        if not os.path.isdir(cron_dir):
            continue
        try:
            for filename in os.listdir(cron_dir):
                if filename.startswith('.') or filename == 'log':
                    continue
                filepath = os.path.join(cron_dir, filename)
                if not os.path.isfile(filepath):
                    continue

                # Read all lines from this file
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                except OSError:
                    continue

                new_lines = []
                found = False
                for line in lines:
                    stripped = line.strip()
                    if not stripped or stripped.startswith('#'):
                        new_lines.append(line)
                        continue
                    try:
                        obj = json.loads(stripped)
                        if isinstance(obj, dict) and str(obj.get('id')) == str(entry_id):
                            found = True
                            if operation == 'update' and new_data:
                                new_data['id'] = obj['id']  # Preserve original ID
                                new_lines.append(json.dumps(new_data) + '\n')
                            # For 'delete', we simply skip adding it
                        else:
                            new_lines.append(line)
                    except json.JSONDecodeError:
                        new_lines.append(line)

                if found:
                    # Write back the modified file
                    try:
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.writelines(new_lines)
                        return True, filepath
                    except OSError as e:
                        print(f"Error writing cron file {filepath}: {e}")
                        return False, filepath
        except OSError:
            continue

    return False, None


@app.route('/api/crontab', methods=['GET'])
@login_required
def get_crontab():
    """Gets crontab entries from ALL files in data/cron/ recursively from the specified entity path."""
    username = session.get('username')
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
         return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    print(f"Fetching crontab data for user '{username}', path: '{entity_rel_path}' -> '{entity_abs_path}'")

    cron_data_dirs = find_data_dirs_recursively(entity_abs_path, 'cron')

    all_entries = []
    for cron_dir in cron_data_dirs:
        entries = read_all_cron_entries_from_dir(cron_dir)
        all_entries.extend(entries)

    return jsonify(all_entries)


@app.route('/api/crontab/files', methods=['GET'])
@login_required
def get_crontab_files():
    """Lists files in the entity's data/cron/ directory."""
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
         return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    cron_dir = os.path.join(entity_abs_path, 'data', 'cron')
    files = []

    if os.path.isdir(cron_dir):
        try:
            for filename in sorted(os.listdir(cron_dir)):
                if filename.startswith('.') or filename == 'log':
                    continue
                filepath = os.path.join(cron_dir, filename)
                if os.path.isfile(filepath):
                    # Count entries in this file
                    entry_count = 0
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith('#'):
                                    try:
                                        json.loads(line)
                                        entry_count += 1
                                    except json.JSONDecodeError:
                                        pass
                    except OSError:
                        pass
                    files.append({
                        "name": filename,
                        "path": filepath,
                        "count": entry_count
                    })
        except OSError as e:
            print(f"Error listing cron directory {cron_dir}: {e}")

    return jsonify(files)


@app.route('/api/crontab', methods=['POST'])
@login_required
def add_crontab():
    """Adds a new crontab entry to the specified entity path and file."""
    entity_rel_path = request.args.get('path', '')
    target_file = request.args.get('file', 'cron')  # Default to 'cron' file

    # Validate target filename (prevent path traversal)
    if '/' in target_file or '\\' in target_file or '..' in target_file or target_file.startswith('.'):
        return jsonify({"error": "Invalid target filename"}), 400

    cron_file_path = get_entity_data_file_path(entity_rel_path, 'cron', target_file)

    if not cron_file_path:
        return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    entry = request.json
    if not entry or not isinstance(entry, dict):
         return jsonify({"error": "Invalid crontab entry data provided"}), 400

    # Validate required fields based on type
    entry_type = entry.get('type')
    if entry_type not in ('date', 'cron', 'cycle'):
        return jsonify({"error": "Entry must have a valid 'type': date, cron, or cycle"}), 400

    if not entry.get('title'):
        return jsonify({"error": "Entry must have a 'title'"}), 400

    # Ensure the target directory exists
    os.makedirs(os.path.dirname(cron_file_path), exist_ok=True)

    # Add ID and timestamp if missing
    if 'id' not in entry or not entry['id']:
        now = datetime.now()
        entry['id'] = f"cron_{now.strftime('%Y%m%d_%H%M%S')}_{int(now.timestamp())}"
    if 'created_at' not in entry:
         entry['created_at'] = datetime.now().isoformat()

    # Set defaults for missing fields
    entry.setdefault('enabled', True)
    entry.setdefault('action', 'display')
    entry.setdefault('command', None)
    entry.setdefault('description', '')
    entry.setdefault('month', None)
    entry.setdefault('day', None)
    entry.setdefault('year', None)
    entry.setdefault('expression', None)
    entry.setdefault('schedule', None)
    entry.setdefault('interval', None)
    entry.setdefault('anchor', None)
    entry.setdefault('last_run', None)

    # Remove source_file/source_filename if client sent them (internal fields)
    entry.pop('source_file', None)
    entry.pop('source_filename', None)

    # Append the new entry (as a JSON string line)
    if append_safe(cron_file_path, json.dumps(entry)):
        print(f"Added crontab entry {entry.get('id')} to {cron_file_path}")
        entry['source_file'] = cron_file_path
        entry['source_filename'] = target_file
        return jsonify({"success": True, "entry": entry}), 201
    print(f"Error adding crontab entry to {cron_file_path}")
    return jsonify({"error": "Failed to save crontab entry"}), 500


@app.route('/api/crontab/<entry_id>', methods=['DELETE'])
@login_required
def delete_crontab(entry_id):
    """Deletes a crontab entry by its ID, searching all files in data/cron/ recursively."""
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    success, filepath = find_and_modify_cron_entry(entity_abs_path, entry_id, operation='delete')

    if success:
        print(f"Deleted crontab entry {entry_id} from {filepath}")
        return jsonify({"success": True})
    elif filepath:
        print(f"Error deleting crontab entry {entry_id} from {filepath}")
        return jsonify({"error": "Failed to delete crontab entry (write error)"}), 500
    else:
        print(f"Crontab entry {entry_id} not found")
        return jsonify({"error": "Crontab entry not found"}), 404


@app.route('/api/crontab/<entry_id>', methods=['PUT'])
@login_required
def update_crontab(entry_id):
    """Updates a crontab entry by its ID, searching all files in data/cron/ recursively."""
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    updated_entry_data = request.json
    if not updated_entry_data or not isinstance(updated_entry_data, dict):
        return jsonify({"error": "Invalid crontab data provided"}), 400

    # Remove internal fields before saving
    updated_entry_data.pop('source_file', None)
    updated_entry_data.pop('source_filename', None)

    success, filepath = find_and_modify_cron_entry(
        entity_abs_path, entry_id, operation='update', new_data=updated_entry_data
    )

    if success:
        print(f"Updated crontab entry {entry_id} in {filepath}")
        return jsonify({"success": True, "entry": updated_entry_data})
    elif filepath:
        print(f"Error updating crontab entry {entry_id} in {filepath}")
        return jsonify({"error": "Failed to update crontab entry (write error)"}), 500
    else:
        print(f"Crontab entry {entry_id} not found for update")
        return jsonify({"error": "Crontab entry not found"}), 404


@app.route('/api/crontab/toggle/<entry_id>', methods=['PUT'])
@login_required
def toggle_crontab(entry_id):
    """Toggles the enabled state of a crontab entry."""
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid or inaccessible entity path specified"}), 400

    data = request.json
    if not data or 'enabled' not in data:
        return jsonify({"error": "Missing 'enabled' field"}), 400

    # Find the entry across all files
    cron_data_dirs = find_data_dirs_recursively(entity_abs_path, 'cron')
    for cron_dir in cron_data_dirs:
        if not os.path.isdir(cron_dir):
            continue
        try:
            for filename in os.listdir(cron_dir):
                if filename.startswith('.') or filename == 'log':
                    continue
                filepath = os.path.join(cron_dir, filename)
                if not os.path.isfile(filepath):
                    continue
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                except OSError:
                    continue

                new_lines = []
                found = False
                for line in lines:
                    stripped = line.strip()
                    if not stripped or stripped.startswith('#'):
                        new_lines.append(line)
                        continue
                    try:
                        obj = json.loads(stripped)
                        if isinstance(obj, dict) and str(obj.get('id')) == str(entry_id):
                            found = True
                            obj['enabled'] = bool(data['enabled'])
                            new_lines.append(json.dumps(obj) + '\n')
                        else:
                            new_lines.append(line)
                    except json.JSONDecodeError:
                        new_lines.append(line)

                if found:
                    try:
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.writelines(new_lines)
                        return jsonify({"success": True, "enabled": data['enabled']})
                    except OSError as e:
                        return jsonify({"error": f"Failed to write: {e}"}), 500
        except OSError:
            continue

    return jsonify({"error": "Crontab entry not found"}), 404


# --- User Profile (Example - Not fully implemented) ---
@app.route('/api/profile', methods=['GET'])
@login_required
def get_profile():
    # Example: Return basic user info
    return jsonify({
        "username": session.get('username'),
        "user_root": get_user_root_path(),
        # Add other relevant info if needed
    })

# --- Notes API ---

@app.route('/api/notes/<task_id>', methods=['GET'])
@login_required
def get_notes(task_id):
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    notes_file = os.path.join(entity_abs_path, 'data', 'project', task_id, 'notes')
    if os.path.exists(notes_file):
        content = read_safe(notes_file)
        return jsonify({"notes": content})
    else:
        return jsonify({"notes": ""})

@app.route('/api/notes/<task_id>', methods=['PUT'])
@login_required
def save_notes(task_id):
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    data = request.json
    notes_content = data.get('notes', '')

    notes_file = os.path.join(entity_abs_path, 'data', 'project', task_id, 'notes')
    if write_safe(notes_file, notes_content):
        return jsonify({"success": True})
    else:
        return jsonify({"error": "Failed to save notes"}), 500

# --- AI Configuration API ---

def parse_ai_configs_from_config(username):
    """
    Parse the user's .config file to extract all AI configuration labels
    and their associated settings (aiconfig path, etc).
    Returns a list of dicts: [{'label': ..., 'aiconfig': ..., ...}, ...]
    """
    config_path = os.path.join(LOS_BASE_PATH, username, '.config')
    configs = []

    if not os.path.isfile(config_path):
        return configs

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error reading .config for AI configs: {e}")
        return configs

    # Parse the indentation-based config to find all labels with 'aiconfig' keys
    current_label = None
    current_section = {}
    label_stack = []  # Track nested labels with (label_name, indent_level)

    for line in lines:
        stripped = line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith('#'):
            continue

        current_indent = len(line) - len(line.lstrip())

        # Check for label definition (ends with :)
        if stripped.endswith(':'):
            # Save previous section if it had an aiconfig
            if current_label and 'aiconfig' in current_section:
                current_section['label'] = current_label
                configs.append(dict(current_section))

            label_name = stripped[:-1]

            # Handle nesting: pop labels from stack that are at same or deeper indent
            while label_stack and label_stack[-1][1] >= current_indent:
                label_stack.pop()

            # Build full label path for nested labels
            if label_stack:
                full_label = label_stack[-1][0] + ':' + label_name
            else:
                full_label = label_name

            label_stack.append((full_label, current_indent))
            current_label = full_label
            current_section = {'label': current_label}

        elif current_label and '=' in stripped:
            key, value = stripped.split('=', 1)
            key = key.strip()
            value = value.strip()
            # Remove quotes
            if value.startswith('"') and '"' in value[1:]:
                value = value[1:value.index('"', 1)]
            # Remove inline comments
            if '#' in value:
                value = value[:value.index('#')].strip()
            value = value.strip('"').strip()
            current_section[key] = value

    # Don't forget the last section
    if current_label and 'aiconfig' in current_section:
        current_section['label'] = current_label
        configs.append(dict(current_section))

    return configs


def find_entity_ai_label(username, entity_name):
    """
    Given an entity name (e.g. 'health'), find the matching AI config label
    from the user's .config file.
    Returns the label string or None.
    """
    configs = parse_ai_configs_from_config(username)
    entity_lower = entity_name.lower()

    # First try exact match on label or last part of label
    for cfg in configs:
        label = cfg.get('label', '')
        # Check if the entity name matches the label or the last segment of a nested label
        label_parts = label.split(':')
        if label_parts[-1].lower() == entity_lower:
            return label

    return None


# --- Agent identity / label resolution ---------------------------------------
#
# Notifications and history entries carry a 'label' naming the agent that
# produced them.  Agent names are never hardcoded: the label is derived from
# the user's own declarations, in this order:
#   1. an explicit ?label= parameter on the callback URL (minted by LOS from the
#      dispatch's .config label, so it is that label name verbatim);
#   2. the job manifest written by the dispatcher;
#   3. another message belonging to the same job;
#   4. a .config label name recognised in the job id (e.g. 'claw_invest-cron');
#   5. '<provider>_<agent>' from an agent declared in ai/aiconfig.* (e.g. a
#      hermes profile named 'sage' -> 'hermes_sage') — works with or without a
#      .config entry;
#   6. the entity's configured label, then the single configured agent, if any;
#   7. UNKNOWN_AGENT_LABEL otherwise — never a framework name.

UNKNOWN_AGENT_LABEL = 'unknown source'

# Tokens that must never be treated as a real agent label.
GENERIC_AGENT_LABELS = {
    'openclaw', 'openclaw/webhook', 'agent', 'agent_callback', 'webhook',
    'unknown', 'unknown source',
}


def _read_aiconfig_agent(username, aiconfig_path):
    """Read (provider, agent_id, type) from an aiconfig file.

    Relative paths resolve against the user's root (same convention aicall
    uses).  Missing or unreadable files yield empty values.
    """
    if not aiconfig_path:
        return '', '', ''
    path = aiconfig_path
    if not os.path.isabs(path):
        path = os.path.join(LOS_BASE_PATH, username, path)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return '', '', ''
    if not isinstance(data, dict):
        return '', '', ''
    return (str(data.get('provider') or ''),
            str(data.get('agent') or ''),
            str(data.get('type') or ''))


def _aiconfig_profile_name(aiconfig_path):
    """Profile/agent name implied by an aiconfig filename (aiconfig.sage -> sage)."""
    base = os.path.basename(aiconfig_path or '')
    if base.lower().startswith('aiconfig.'):
        return base[len('aiconfig.'):]
    return base


def _user_agent_identity(username):
    """Collect the agent identities this user has declared.

    Sources (all user-owned):
      * <user>/.config sections that carry an aiconfig, when either the section
        or that aiconfig declares type=agent (legacy 'claw_' label names are
        accepted too);
      * every <user>/ai/aiconfig.* declaring type=agent that .config does not
        reference, for agents the user never listed in .config.

    Returns a dict with:
      labels        — {lowercased label} of every known agent
      tokens        — {token: {label}} union of the two maps below
      config_tokens — tokens derived from .config label names
      derived_tokens— tokens derived from ai/aiconfig.* provider + agent names
    """
    labels = set()
    config_tokens = {}
    derived_tokens = {}
    referenced = set()

    def add(token_map, token, label):
        token = str(token or '').strip().lower()
        if token:
            token_map.setdefault(token, set()).add(label)

    for cfg in parse_ai_configs_from_config(username):
        label = str(cfg.get('label') or '')
        aiconfig = str(cfg.get('aiconfig') or '')
        if not label:
            continue
        provider, agent_id, ai_type = _read_aiconfig_agent(username, aiconfig)
        if aiconfig:
            path = aiconfig if os.path.isabs(aiconfig) \
                else os.path.join(LOS_BASE_PATH, username, aiconfig)
            referenced.add(os.path.abspath(path))
        if not (cfg.get('type') == 'agent' or ai_type == 'agent' or 'claw_' in label):
            continue
        labels.add(label.lower())
        for token in (label, label.split(':')[-1], agent_id,
                      _aiconfig_profile_name(aiconfig)):
            add(config_tokens, token, label)

    ai_dir = os.path.join(LOS_BASE_PATH, username, 'ai')
    try:
        entries = sorted(os.listdir(ai_dir))
    except OSError:
        entries = []
    for name in entries:
        if not name.startswith('aiconfig.'):
            continue
        path = os.path.join(ai_dir, name)
        if os.path.abspath(path) in referenced:
            continue
        provider, agent_id, ai_type = _read_aiconfig_agent(username, path)
        if ai_type != 'agent' or not agent_id:
            continue
        label = f"{provider}_{agent_id}" if provider else agent_id
        labels.add(label.lower())
        for token in (agent_id, _aiconfig_profile_name(path), label):
            add(derived_tokens, token, label)

    tokens = {t: set(v) for t, v in config_tokens.items()}
    for token, labels_for_token in derived_tokens.items():
        tokens.setdefault(token, set()).update(labels_for_token)

    return {
        'labels': labels,
        'tokens': tokens,
        'config_tokens': config_tokens,
        'derived_tokens': derived_tokens,
    }


def _is_usable_agent_label(label, known_labels=()):
    """True when label names a real agent (not a generic/unknown marker)."""
    if not label or not isinstance(label, str):
        return False
    low = label.lower()
    if low in GENERIC_AGENT_LABELS:
        return False
    known = {str(l).lower() for l in (known_labels or ())}
    if low in known or label.split(':')[-1].lower() in known:
        return True
    # Legacy OpenClaw-style label names, and explicit agent/ai/assistant prefixes
    if 'claw_' in label:
        return True
    return label.startswith(('agent:', 'ai:', 'assistant:'))


def _infer_agent_label_from_job_id(identity, job_id):
    """Best-effort agent label taken from the job id the caller chose.

    Callers that schedule their own work (cron jobs, standby runs) invent job
    ids such as 'sage-study-am-20260923' or 'athena-review'; the id therefore
    often contains the agent/profile name.  Tokens are matched against the
    user's own declared identities, and only a token matching exactly ONE known
    agent is accepted — otherwise nothing is inferred.

    Returns (label, source) or (None, '').
    """
    if not job_id:
        return None, ''
    tokens = [t for t in re.split(r'[^A-Za-z0-9]+', str(job_id)) if t]
    # .config label names take precedence over synthesised provider_agent names
    for token_map, source in ((identity['config_tokens'], '.config label'),
                              (identity['derived_tokens'], 'aiconfig agent')):
        hits = set()
        for token in tokens:
            hits |= token_map.get(token.lower(), set())
        if len(hits) == 1:
            return hits.pop(), source
    return None, ''


def _find_entity_agent_label(username, entity_name, known_labels=()):
    """Best .config agent label for an entity name.

    Compares the entity name with the label's word tokens, so 'invest' matches
    the label 'claw_invest', 'health' matches 'claw_health' and a nested label
    like 'health:doctor:claw_health' matches too.  Ambiguous matches are
    ignored rather than guessed.
    """
    exact = find_entity_ai_label(username, entity_name)
    if _is_usable_agent_label(exact, known_labels):
        return exact
    entity_lower = entity_name.lower()
    candidates = []
    for cfg in parse_ai_configs_from_config(username):
        label = str(cfg.get('label') or '')
        if not _is_usable_agent_label(label, known_labels):
            continue
        label_tokens = {t.lower() for t in re.split(r'[^A-Za-z0-9]+', label) if t}
        if entity_lower in label_tokens:
            candidates.append(label)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _build_agent_webhook_url(self_base, project_id, username, entity_param,
                             project_token, job_id, label):
    """Build the per-project callback URL handed to a dispatched agent.

    The label parameter is what lets the receiver attribute a callback to the
    agent that owns the URL — it keeps working even when the agent rewrites
    job= or copies the URL into its own scheduler.  The label is URL-encoded so
    nested .config labels (e.g. 'manager:claw_manager') survive intact.
    """
    return (
        f"{self_base}/api/agent/webhook/{project_id}"
        f"?user={username}&path={entity_param}&token={project_token}"
        f"&job={job_id}&label={quote(str(label))}"
    )


def _resolve_agent_label(username, entity_path, proj_dir, job_id, label_hint=''):
    """Resolve which agent produced a callback/message.

    Follows the documented ladder (see the comment above UNKNOWN_AGENT_LABEL).
    Returns (label, source); label is None when nothing could be determined, in
    which case the caller should use UNKNOWN_AGENT_LABEL.
    """
    identity = _user_agent_identity(username)
    known = identity['labels']

    # 1. Explicit ?label= on the callback URL.  LOS mints this from the .config
    #    label of the dispatch, so it names the agent exactly.  Only accepted
    #    when it really is one of this user's own agent labels.
    if label_hint:
        hint = str(label_hint).strip()
        if hint.lower() in known or hint.split(':')[-1].lower() in known:
            return hint, 'url label'

    # 2. Job manifest written when the job was dispatched.
    if proj_dir and job_id:
        job_file = os.path.join(proj_dir, 'jobs', f"{job_id}.json")
        if os.path.exists(job_file):
            try:
                with open(job_file, 'r') as f:
                    manifest_label = (json.load(f) or {}).get('label')
                if _is_usable_agent_label(manifest_label, known):
                    return manifest_label, 'job manifest'
            except Exception as e:
                print(f"[webhook] Could not read job manifest for label: {e}")

    # 3. Another message of the SAME job already names the agent.  Never look at
    #    unrelated turns: that is what used to stamp a stale label on a
    #    different agent's callback.
    if proj_dir and job_id:
        history_file = os.path.join(proj_dir, 'history.json')
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                if isinstance(history, list):
                    for entry in reversed(history):
                        if not isinstance(entry, dict):
                            continue
                        if entry.get('job_id') != job_id:
                            continue
                        candidate = entry.get('label')
                        if _is_usable_agent_label(candidate, known):
                            return candidate, 'same-job history'
            except Exception as e:
                print(f"[webhook] Could not read history for label: {e}")

    # 4./5. Recognise the agent from the job id the caller chose.
    inferred, source = _infer_agent_label_from_job_id(identity, job_id)
    if inferred:
        return inferred, source

    # 6a. The entity's configured label.
    if entity_path:
        entity_name = entity_path.rstrip('/').split('/')[-1]
        entity_label = _find_entity_agent_label(username, entity_name, known)
        if entity_label:
            return entity_label, 'entity config'

    # 6b. If exactly one agent is configured for this user, it must be that one.
    agent_configs = [c for c in parse_ai_configs_from_config(username)
                     if c.get('type') == 'agent'
                     or _is_usable_agent_label(c.get('label'), known)]
    if len(agent_configs) == 1 and agent_configs[0].get('label'):
        return agent_configs[0]['label'], 'only configured agent'

    # 7. Nothing is known.
    return None, ''


@app.route('/api/ai/configs', methods=['GET'])
@login_required
def api_get_ai_configs():
    """Return all AI configuration labels from the user's .config file."""
    username = session.get('username')
    configs = parse_ai_configs_from_config(username)

    # Return sanitized configs (strip sensitive fields like api keys, tokens)
    sanitized = []
    sensitive_keys = {'token', 'auth_token', 'account_sid', 'api_key', 'origin_num'}
    for cfg in configs:
        clean = {}
        for k, v in cfg.items():
            if k.lower() not in sensitive_keys:
                clean[k] = v
        sanitized.append(clean)

    return jsonify(sanitized)


@app.route('/api/ai/entity-config', methods=['GET'])
@login_required
def api_get_entity_ai_config():
    """Given an entity path, find the AI config label that applies."""
    username = session.get('username')
    entity_path = request.args.get('path', '')

    # Extract the entity name from the path (last segment)
    if entity_path:
        entity_name = entity_path.rstrip('/').split('/')[-1]
    else:
        entity_name = username  # Root = user's own name

    label = find_entity_ai_label(username, entity_name)

    if label:
        return jsonify({"label": label, "entity": entity_name})
    else:
        return jsonify({"label": None, "entity": entity_name})


@app.route('/api/ai/call', methods=['POST'])
@login_required
def api_ai_call():
    """Execute aicall with the given label and prompt, return the output."""
    import subprocess

    data = request.json
    if not data:
        return jsonify({"error": "Invalid request data"}), 400

    label = data.get('label', '')
    prompt = data.get('prompt', '')

    if not label:
        return jsonify({"error": "AI configuration label is required"}), 400
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    username = session.get('username')

    # Validate the label exists in the user's config
    configs = parse_ai_configs_from_config(username)
    valid_labels = [c.get('label', '') for c in configs]
    if label not in valid_labels:
        return jsonify({"error": f"Invalid AI configuration label: {label}"}), 400

    # Execute aicall
    aicall_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'aicall', 'aicall.py')

    # Read timeout from the label's aiconfig file instead of hardcoding
    subprocess_timeout = 300  # fallback default
    matched_cfg = next((c for c in configs if c.get('label') == label), None)
    if matched_cfg and matched_cfg.get('aiconfig'):
        try:
            aiconfig_path = matched_cfg['aiconfig'].strip('"').strip()
            if not os.path.isabs(aiconfig_path):
                aiconfig_path = os.path.join(LOS_BASE_PATH, username, aiconfig_path)
            with open(aiconfig_path, 'r') as f:
                aiconfig_data = json.load(f)
            # Use the maximum timeout across all configured LLMs, plus a buffer
            llm_timeouts = [llm.get('timeout', 120) for llm in aiconfig_data.get('llms', [])]
            if llm_timeouts:
                subprocess_timeout = max(llm_timeouts) + 30  # buffer for overhead
            elif aiconfig_data.get('timeout'):
                # Agent configs (no 'llms' array) can set top-level 'timeout'
                subprocess_timeout = int(aiconfig_data['timeout']) + 30
        except Exception as e:
            print(f"Warning: Could not read timeout from aiconfig, using default 120s: {e}")

    try:
        result = subprocess.run(
            ['python3', aicall_path, label],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=subprocess_timeout,
            cwd=os.path.join(LOS_BASE_PATH, username),
            env={**os.environ, 'USER': username, 'LOGNAME': username}
        )

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        return jsonify({
            "output": output,
            "stderr": stderr if stderr else None,
            "returncode": result.returncode
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": f"AI call timed out ({subprocess_timeout}s limit)"}), 504
    except Exception as e:
        print(f"Error executing aicall: {e}")
        return jsonify({"error": f"Failed to execute AI call: {str(e)}"}), 500


    # --- Project History API ---

def get_self_base_url():
    """This server's own base URL, used for agent callbacks and webhooks.

    The port comes from /los/sys/config.json via the gateway; 14001 is the
    default for a standalone run.
    """
    return "https://localhost:%s" % os.environ.get('LOS_WEB_PORT', '14001')

def get_agent_callback_url():
    """Returns the base URL for the agent callback."""
    return f"{get_self_base_url()}/api/agent/callback"

def get_project_dir(entity_abs_path, task_id):
    """Returns the absolute path to the project directory for a task."""
    if not entity_abs_path or not task_id:
        return None
    # Sanitize task_id: only allow alphanumeric, underscore, hyphen
    if not re.match(r'^[a-zA-Z0-9_\-]+$', str(task_id)):
        return None
    return os.path.join(entity_abs_path, 'data', 'project', str(task_id))


@app.route('/api/project/<task_id>/history', methods=['GET'])
@login_required
def api_get_project_history(task_id):
    """Get the conversation/AI history for a task from its project directory."""
    entity_rel_path = request.args.get('path', '')
    username = session.get('username')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    # Security check
    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Invalid path"}), 403

    history_file = os.path.join(proj_dir, 'history.json')

    if not os.path.exists(history_file):
        return jsonify([])

    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            history = json.load(f)
        return jsonify(history)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading history for task {task_id}: {e}")
        return jsonify([])


@app.route('/api/project/<task_id>/history', methods=['POST'])
@login_required
def api_append_project_history(task_id):
    """Append one or more messages to the task's history.json."""
    entity_rel_path = request.args.get('path', '')
    username = session.get('username')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    # Security check
    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Invalid path"}), 403

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Accept either a single message dict or a list of messages
    if isinstance(data, dict):
        new_messages = [data]
    elif isinstance(data, list):
        new_messages = data
    else:
        return jsonify({"error": "Invalid data format"}), 400

    history_file = os.path.join(proj_dir, 'history.json')
    os.makedirs(proj_dir, exist_ok=True)

    # Load existing history
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            history = []

    # Append new messages, adding IDs and timestamps if missing.
    # Guard: skip any message whose role+content duplicates the most-recent
    # history entry within a 10-second window (prevents double-saves that
    # occur when both the browser streamDoneCallback and a server-side path
    # write the same response in quick succession).
    now = datetime.now(timezone.utc).isoformat()
    for msg in new_messages:
        if not isinstance(msg, dict):
            continue
        # Deduplication check
        if history:
            last = history[-1]
            if (last.get('role') == msg.get('role') and
                    last.get('content') == msg.get('content') and
                    msg.get('content')):
                try:
                    last_ts = datetime.fromisoformat(last.get('timestamp', ''))
                    now_dt  = datetime.now(timezone.utc)
                    # If the last message has a timezone-aware timestamp, compare directly;
                    # otherwise assume UTC.
                    if last_ts.tzinfo is None:
                        from datetime import timezone as _tz
                        last_ts = last_ts.replace(tzinfo=_tz.utc)
                    age_s = (now_dt - last_ts).total_seconds()
                    if age_s < 10:
                        # Duplicate within 10 s — skip silently
                        continue
                except (ValueError, TypeError):
                    pass
        if 'id' not in msg:
            msg['id'] = f"msg_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}"
        if 'timestamp' not in msg:
            msg['timestamp'] = now
        history.append(msg)

    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        # Fan out each newly appended message to live project SSE subscribers
        for msg in new_messages:
            if isinstance(msg, dict):
                _push_to_project_subscribers(task_id, {'type': 'message', 'data': msg})
        return jsonify({"success": True, "count": len(history)})
    except OSError as e:
        print(f"Error writing history for task {task_id}: {e}")
        return jsonify({"error": "Failed to save history"}), 500


@app.route('/api/agent/callback', methods=['POST'])
@login_required
def api_agent_callback():
    """Handle callback from an agent to append to project history."""
    data = request.json
    project_id = data.get('project_id')
    message = data.get('message')

    if not project_id or not message:
        return jsonify({"error": "Missing project_id or message"}), 400

    # Locate the project directory
    # We need entity_path to locate it properly. If not passed, we have to search
    # or assume it's in a known location (for now, assume entity_path is passed)
    entity_path = data.get('entity_path', '')
    entity_abs_path = get_entity_absolute_path(entity_path)
    
    if not entity_abs_path:
        # Fallback: scan projects if entity_path not reliable
        entity_abs_path = os.path.join(get_user_root_path(), 'data')
    
    proj_dir = get_project_dir(entity_abs_path, project_id)
    if not proj_dir or not os.path.exists(proj_dir):
        # Scan if not found
        proj_dir = None
        for root, dirs, _ in os.walk(os.path.join(get_user_root_path(), 'data')):
            if f"project/{project_id}" in root:
                proj_dir = root
                break
        
    if not proj_dir:
        return jsonify({"error": "Project directory not found"}), 404

    history_file = os.path.join(proj_dir, 'history.json')
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except:
            history = []

    msg = {
        'role': 'assistant',
        'label': 'agent_callback',
        'content': message,
        'id': f"msg_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}",
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    history.append(msg)

    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        return jsonify({"success": True, "count": len(history)})
    except OSError as e:
        print(f"Error writing history for task {task_id}: {e}")
        return jsonify({"error": "Failed to save history"}), 500


@app.route('/api/project/<task_id>/agent-pending', methods=['GET'])
@login_required
def api_check_agent_pending(task_id):
    """Check for and consume a pending agent response file."""
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir or not os.path.exists(proj_dir):
        return jsonify({"pending": False})

    pending_file = os.path.join(proj_dir, '.agent_pending.json')
    if os.path.exists(pending_file):
        try:
            with open(pending_file, 'r') as f:
                data = json.load(f)
            os.remove(pending_file)
            return jsonify({"pending": True, "message": data.get('message')})
        except Exception as e:
            print(f"Error reading pending agent file: {e}")
            return jsonify({"error": "Failed to read pending file"}), 500
    
    return jsonify({"pending": False})


@app.route('/api/project/<task_id>/history', methods=['DELETE'])
@login_required
def api_clear_project_history(task_id):
    """Clear the history for a task (reset to empty)."""
    entity_rel_path = request.args.get('path', '')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Invalid path"}), 403

    history_file = os.path.join(proj_dir, 'history.json')
    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump([], f)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": f"Failed to clear history: {e}"}), 500


@app.route('/api/project/<task_id>/history/entry', methods=['POST'])
@login_required
def api_delete_project_history_entry(task_id):
    """Delete one or more consecutive messages from the task's history.json by index."""
    entity_rel_path = request.args.get('path', '')
    username = session.get('username')

    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Invalid path"}), 403

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    start_idx = data.get('start_idx')
    count = data.get('count', 1)

    if start_idx is None or not isinstance(start_idx, int):
        return jsonify({"error": "start_idx is required and must be an integer"}), 400
    if not isinstance(count, int) or count < 1:
        return jsonify({"error": "count must be a positive integer"}), 400

    history_file = os.path.join(proj_dir, 'history.json')
    if not os.path.exists(history_file):
        return jsonify({"error": "History file not found"}), 404

    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            history = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": f"Failed to read history: {e}"}), 500

    if not isinstance(history, list):
        return jsonify({"error": "Invalid history format"}), 500

    if start_idx < 0 or start_idx >= len(history):
        return jsonify({"error": "start_idx out of range"}), 400
    if start_idx + count > len(history):
        count = len(history) - start_idx

    del history[start_idx:start_idx + count]

    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except OSError as e:
        return jsonify({"error": f"Failed to write history: {e}"}), 500

    return jsonify({"success": True, "deleted": count})


# --- Project token, agent report, per-project SSE, and job endpoints ---

@app.route('/api/project/<task_id>/token', methods=['GET'])
@login_required
def api_get_project_token(task_id):
    """Return (or create) the per-project agent auth token.

    Browsers call this when they want to hand the token to an outgoing agent.
    The agent then uses it as:  Authorization: Bearer <token>
    when POSTing to /api/project/<task_id>/report.
    """
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Forbidden"}), 403

    os.makedirs(proj_dir, exist_ok=True)
    token = ensure_project_token(proj_dir)

    username = session.get('username')
    # Build the full report URL the agent should POST to
    report_url = (
        f"https://{request.host}/api/project/{task_id}/report"
        f"?user={username}&path={entity_rel_path}"
    )
    return jsonify({
        "token":      token,
        "task_id":    task_id,
        "report_url": report_url,
    })


@app.route('/api/project/<task_id>/report', methods=['POST'])
def api_project_report(task_id):
    """Token-authenticated report endpoint — no web session required.

    Agents (local subprocesses or remote services) POST here to append messages
    to a project's history and optionally update a job's status.

    URL query params:
      user=<username>           (required)
      path=<entity_rel_path>    (required, may be empty for root)

    Request header:
      Authorization: Bearer <64-char project token>

    Request body (JSON):
      {
        "content":  "...",          # required — the message text
        "job_id":   "job_...",      # optional — ties message to a specific job
        "role":     "assistant",    # optional, default "assistant"
        "label":    "openclaw/main",# optional, shown in canvas
        "status":   "running"       # optional — "running"|"done"|"error"
      }
    """
    # --- Validate task_id ---
    if not re.match(r'^[a-zA-Z0-9_\-]+$', str(task_id)):
        return jsonify({"error": "Invalid task ID"}), 400

    # --- Authenticate via Bearer token ---
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({"error": "Authorization: Bearer <token> header required"}), 401
    provided_token = auth_header[len('Bearer '):].strip()
    if not provided_token:
        return jsonify({"error": "Empty token"}), 401

    # --- Locate project via user + entity_path (no filesystem scanning) ---
    username    = request.args.get('user', '').strip()
    entity_path = request.args.get('path', '').strip()

    if not username or not re.match(r'^[a-zA-Z0-9_\-]+$', username):
        return jsonify({"error": "Missing or invalid 'user' query parameter"}), 400

    user_dir = os.path.abspath(os.path.join(LOS_BASE_PATH, username))
    los_abs  = os.path.abspath(LOS_BASE_PATH)
    if not user_dir.startswith(los_abs + os.sep):
        return jsonify({"error": "Forbidden"}), 403

    if entity_path and ('..' in entity_path or entity_path.startswith('/')):
        return jsonify({"error": "Invalid entity path"}), 400

    entity_abs = (
        os.path.normpath(os.path.join(user_dir, entity_path))
        if entity_path else user_dir
    )
    if not entity_abs.startswith(user_dir):
        return jsonify({"error": "Forbidden"}), 403

    proj_dir = os.path.join(entity_abs, 'data', 'project', str(task_id))

    # --- Validate token ---
    token_file = os.path.join(proj_dir, '.token')
    if not os.path.exists(token_file):
        return jsonify({"error": "Project not found or token not initialised"}), 403
    try:
        with open(token_file, 'r') as f:
            stored_token = f.read().strip()
    except OSError:
        return jsonify({"error": "Token read error"}), 500

    if stored_token != provided_token:
        return jsonify({"error": "Invalid token"}), 403

    # --- Parse body ---
    data    = request.json or {}
    content = data.get('content') or data.get('message', '')
    if not content:
        return jsonify({"error": "Missing 'content' field"}), 400

    role   = data.get('role', 'assistant')
    label  = data.get('label', 'agent')
    job_id = data.get('job_id')
    status = data.get('status')   # running | done | error

    # --- Append to history ---
    history_file = os.path.join(proj_dir, 'history.json')
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []

    now = datetime.now(timezone.utc).isoformat()
    msg = {
        'id':        f"msg_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}",
        'role':      role,
        'label':     label,
        'content':   content,
        'timestamp': now,
    }
    if job_id:
        msg['job_id'] = job_id

    history.append(msg)

    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error writing history via report endpoint for {task_id}: {e}")
        return jsonify({"error": "Failed to save history"}), 500

    # --- Update job manifest ---
    if job_id:
        update_job_status(proj_dir, job_id,
                          status=status or 'running',
                          last_message_at=now)

    # --- Fan-out to live project SSE subscribers ---
    _push_to_project_subscribers(task_id, {'type': 'message', 'data': msg})

    print(f"[report] task={task_id} user={username} job={job_id} status={status} len={len(content)}")
    return jsonify({"success": True, "message_id": msg['id'], "history_count": len(history)})


@app.route('/api/project/<task_id>/events', methods=['GET'])
@login_required
def api_project_events(task_id):
    """Per-project SSE stream.

    Browsers subscribe here when they focus a task in the canvas.  The stream
    remains open indefinitely (keepalives every 25 s) and pushes 'message'
    events for every new report that arrives — whether from a local aicall
    subprocess or a remote agent POSTing to /api/project/<task_id>/report.

    Unlike the old per-stream SSE, this channel does NOT close when the
    subprocess exits, so late callbacks are delivered to the browser.
    """
    from flask import Response, stream_with_context

    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Forbidden"}), 403

    sub_queue = queue.Queue()

    with _project_subs_lock:
        if task_id not in _project_subscribers:
            _project_subscribers[task_id] = []
        _project_subscribers[task_id].append(sub_queue)

    def generate():
        try:
            while True:
                try:
                    event = sub_queue.get(timeout=25)
                    event_type  = event.get('type', 'message')
                    payload     = json.dumps(event.get('data', event), ensure_ascii=False)
                    safe_payload = payload.replace('\n', '\\n')
                    yield f"event: {event_type}\ndata: {safe_payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _project_subs_lock:
                subs = _project_subscribers.get(task_id, [])
                if sub_queue in subs:
                    subs.remove(sub_queue)
                if not subs and task_id in _project_subscribers:
                    del _project_subscribers[task_id]

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'close',
        }
    )


@app.route('/api/project/<task_id>/jobs', methods=['GET'])
@login_required
def api_project_jobs(task_id):
    """List all job manifests for a project (sorted by started_at desc)."""
    entity_rel_path = request.args.get('path', '')
    entity_abs_path = get_entity_absolute_path(entity_rel_path)
    if not entity_abs_path:
        return jsonify({"error": "Invalid entity path"}), 400

    proj_dir = get_project_dir(entity_abs_path, task_id)
    if not proj_dir:
        return jsonify({"error": "Invalid task ID"}), 400

    user_root = get_user_root_path()
    if not is_safe_path(user_root, proj_dir):
        return jsonify({"error": "Forbidden"}), 403

    jobs_dir = os.path.join(proj_dir, 'jobs')
    jobs = []
    if os.path.isdir(jobs_dir):
        for fn in sorted(os.listdir(jobs_dir), reverse=True):
            if fn.endswith('.json') and not fn.startswith('.'):
                try:
                    with open(os.path.join(jobs_dir, fn), 'r') as f:
                        jobs.append(json.load(f))
                except Exception:
                    pass
    return jsonify(jobs)


# --- Notification helpers ---

def _get_notifications_path(username):
    """Return path to the user's notifications.json file."""
    return os.path.join(LOS_BASE_PATH, username, 'data', 'notifications.json')


def _load_notifications(username):
    """Load notification list for a user (newest first)."""
    path = _get_notifications_path(username)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save_notifications(username, notifications):
    """Persist notification list, capping at NOTIFICATIONS_MAX."""
    path = _get_notifications_path(username)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(notifications[:NOTIFICATIONS_MAX], f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving notifications for {username}: {e}")


def _push_user_notification(username, event_data):
    """Fan-out a notification event to all live SSE queues for a user."""
    with _notif_subs_lock:
        queues = list(_notif_subscribers.get(username, []))
    for q in queues:
        try:
            q.put_nowait(event_data)
        except Exception:
            pass


def _add_notification(username, notif):
    """Persist a notification and push it live to subscribed browsers."""
    notifications = _load_notifications(username)
    notifications.insert(0, notif)   # newest first
    _save_notifications(username, notifications)
    _push_user_notification(username, {'type': 'new', 'data': notif})
    unread = sum(1 for n in notifications if not n.get('read', False))
    _push_user_notification(username, {'type': 'count', 'data': {'unread': unread}})


def _extract_openclaw_text(payload):
    """Extract plain text from OpenClaw's webhook delivery payload.

    Handles the nested result.payloads, top-level payloads, and plain text
    formats that different OpenClaw versions may send.
    """
    if not isinstance(payload, dict):
        return str(payload) if payload else ''
    # nested result.payloads  (modern gateway format)
    result = payload.get('result', {})
    if isinstance(result, dict):
        payloads = result.get('payloads', [])
        if payloads:
            texts = [p.get('text', '') for p in payloads if p.get('text')]
            if texts:
                return '\n'.join(texts)
    # top-level payloads  (direct gateway format)
    payloads = payload.get('payloads', [])
    if payloads:
        texts = [p.get('text', '') for p in payloads if p.get('text')]
        if texts:
            return '\n'.join(texts)
    # plain text fields  (webhook shim format)
    for key in ('text', 'message', 'content', 'body', 'response'):
        val = payload.get(key)
        if val and isinstance(val, str):
            return val
    return ''


# --- Agent webhook receiver (openclaw --deliver --reply-channel webhook) ---

@app.route('/api/agent/webhook/<task_id>', methods=['POST'])
def api_agent_webhook(task_id):
    """Webhook receiver for openclaw async callbacks.

    OpenClaw POSTs here when a deferred agent job completes.  Authentication
    is via token in the URL query string — no web session required.

    URL params:
      user=<username>        (required)
      path=<entity_path>     (required, may be empty for root)
      token=<project_token>  (required)
      job=<job_id>           (optional)
      label=<agent_label>    (optional; the .config label of the dispatching
                              agent — LOS includes it in the URLs it hands out,
                              and it is only honoured when it really is one of
                              this user's own agent labels)
    """
    if not re.match(r'^[a-zA-Z0-9_\-]+$', str(task_id)):
        return jsonify({"error": "Invalid task ID"}), 400

    username    = request.args.get('user', '').strip()
    entity_path = request.args.get('path', '').strip()
    token       = request.args.get('token', '').strip()
    job_id      = request.args.get('job', '').strip()

    if not username or not re.match(r'^[a-zA-Z0-9_\-]+$', username):
        return jsonify({"error": "Missing or invalid user"}), 400
    if not token:
        return jsonify({"error": "Missing token"}), 401

    # Security checks
    user_dir = os.path.abspath(os.path.join(LOS_BASE_PATH, username))
    los_abs  = os.path.abspath(LOS_BASE_PATH)
    if not user_dir.startswith(los_abs + os.sep):
        return jsonify({"error": "Forbidden"}), 403

    if entity_path and ('..' in entity_path or entity_path.startswith('/')):
        return jsonify({"error": "Invalid entity path"}), 400

    entity_abs = (
        os.path.normpath(os.path.join(user_dir, entity_path))
        if entity_path else user_dir
    )
    if not entity_abs.startswith(user_dir):
        return jsonify({"error": "Forbidden"}), 403

    proj_dir = os.path.join(entity_abs, 'data', 'project', str(task_id))

    # Validate token
    token_file = os.path.join(proj_dir, '.token')
    if not os.path.exists(token_file):
        return jsonify({"error": "Project not found or token not initialised"}), 403
    try:
        with open(token_file, 'r') as f:
            stored_token = f.read().strip()
    except OSError:
        return jsonify({"error": "Token read error"}), 500

    if stored_token != token:
        return jsonify({"error": "Invalid token"}), 403

    # Extract message text from openclaw's webhook payload
    payload = request.json or {}
    content = _extract_openclaw_text(payload)
    if not content:
        content = json.dumps(payload)[:2000]  # fallback: store raw JSON

    # Resolve which agent produced this callback — see _resolve_agent_label for
    # the ladder.  Nothing here hardcodes an agent, provider or profile name.
    label_hint = request.args.get('label', '').strip()
    notif_label, label_source = _resolve_agent_label(
        username, entity_path, proj_dir, job_id, label_hint)
    if not notif_label:
        notif_label = UNKNOWN_AGENT_LABEL
        label_source = 'unresolved'

    # Append to project history
    history_file = os.path.join(proj_dir, 'history.json')
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []

    now = datetime.now(timezone.utc).isoformat()
    msg = {
        'id':        f"msg_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}",
        'role':      'assistant',
        # Label the entry with the resolved agent so the canvas block shows the
        # real agent (hermes_default, claw_health, …) instead of a generic tag.
        'label':     notif_label,
        'content':   content,
        'timestamp': now,
        'source':    'webhook',
    }
    if job_id:
        msg['job_id'] = job_id

    history.append(msg)
    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[webhook] Error writing history for {task_id}: {e}")
        return jsonify({"error": "Failed to save history"}), 500

    # Update job manifest
    if job_id:
        update_job_status(proj_dir, job_id, status='done',
                          finished_at=now, source='webhook')

    # Fan-out to live project SSE subscribers
    _push_to_project_subscribers(task_id, {'type': 'message', 'data': msg})

    # Create a user notification (for the bell badge)
    notif = {
        'id':          f"notif_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:6]}",
        'timestamp':   now,
        'type':        'agent_callback',
        'task_id':     task_id,
        'entity_path': entity_path,
        'task_type':   'project',
        'job_id':      job_id or '',
        # Lets the UI jump straight to the canvas block this callback created
        'message_id':  msg['id'],
        'label':       notif_label,
        'snippet':     content[:120] + ('…' if len(content) > 120 else ''),
        'read':        False,
    }
    _add_notification(username, notif)

    print(f"[webhook] task={task_id} user={username} job={job_id} "
          f"label={notif_label} ({label_source}) len={len(content)}")
    return jsonify({"success": True, "message_id": msg['id']})


# --- Notification API endpoints ---

@app.route('/api/notifications', methods=['GET'])
@login_required
def api_get_notifications():
    """List notifications for the current user (newest first)."""
    username = session.get('username')
    notifications = _load_notifications(username)
    unread = sum(1 for n in notifications if not n.get('read', False))
    return jsonify({"notifications": notifications, "unread": unread})


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def api_mark_all_notifications_read():
    """Mark all notifications as read."""
    username = session.get('username')
    notifications = _load_notifications(username)
    for n in notifications:
        n['read'] = True
    _save_notifications(username, notifications)
    _push_user_notification(username, {'type': 'count', 'data': {'unread': 0}})
    return jsonify({"success": True})


@app.route('/api/notifications/<notif_id>/read', methods=['POST'])
@login_required
def api_mark_notification_read(notif_id):
    """Mark a single notification as read."""
    username = session.get('username')
    notifications = _load_notifications(username)
    for n in notifications:
        if n.get('id') == notif_id:
            n['read'] = True
            break
    _save_notifications(username, notifications)
    unread = sum(1 for n in notifications if not n.get('read', False))
    _push_user_notification(username, {'type': 'count', 'data': {'unread': unread}})
    return jsonify({"success": True})


@app.route('/api/notifications/events', methods=['GET'])
@login_required
def api_notification_events():
    """Per-user SSE stream for live notification updates (badge count, new items)."""
    from flask import Response, stream_with_context

    username = session.get('username')
    sub_queue = queue.Queue()

    with _notif_subs_lock:
        if username not in _notif_subscribers:
            _notif_subscribers[username] = []
        _notif_subscribers[username].append(sub_queue)

    # Compute initial unread count
    notifications = _load_notifications(username)
    initial_unread = sum(1 for n in notifications if not n.get('read', False))

    def generate():
        # Push initial badge count on connect so the browser doesn't have to
        # make a separate /api/notifications request just for the count.
        try:
            payload = json.dumps({'unread': initial_unread}, ensure_ascii=False)
            yield f"event: count\ndata: {payload}\n\n"
        except Exception:
            pass
        try:
            while True:
                try:
                    event = sub_queue.get(timeout=30)
                    event_type   = event.get('type', 'count')
                    data_payload = json.dumps(event.get('data', {}), ensure_ascii=False)
                    safe_payload = data_payload.replace('\n', '\\n')
                    yield f"event: {event_type}\ndata: {safe_payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _notif_subs_lock:
                subs = _notif_subscribers.get(username, [])
                if sub_queue in subs:
                    subs.remove(sub_queue)
                if not subs and username in _notif_subscribers:
                    del _notif_subscribers[username]

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection':        'close',
        }
    )


# --- Enhanced AI Config API (with type/icon from aiconfig files) ---

def load_aiconfig_meta(aiconfig_path, username):
    """Load metadata (type, icon, provider) from an aiconfig JSON file."""
    if not aiconfig_path:
        return {}
    try:
        path = aiconfig_path.strip('"').strip()
        if not os.path.isabs(path):
            path = os.path.join(LOS_BASE_PATH, username, path)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        meta = {}
        if 'type' in data:
            meta['config_type'] = data['type']  # 'agent' or absent/null/llm
        if 'provider' in data:
            meta['provider'] = data['provider']
        if 'agent' in data:
            meta['agent'] = data['agent']
        if 'icon' in data:
            meta['icon'] = data['icon']
        if 'gateway_url' in data:
            meta['gateway_url'] = data['gateway_url']
        # Extract model info from first LLM entry
        llms = data.get('llms', [])
        if llms:
            first = llms[0]
            meta['model'] = first.get('model', '')
        return meta
    except Exception:
        return {}


@app.route('/api/ai/configs/full', methods=['GET'])
@login_required
def api_get_ai_configs_full():
    """Return all AI config labels with enriched metadata from aiconfig files.
    Includes type (llm/agent), icon, provider, model info."""
    username = session.get('username')
    configs = parse_ai_configs_from_config(username)

    sensitive_keys = {'token', 'auth_token', 'account_sid', 'api_key', 'origin_num',
                      'gateway_token'}
    result = []
    for cfg in configs:
        clean = {}
        for k, v in cfg.items():
            if k.lower() not in sensitive_keys:
                clean[k] = v

        # Load additional meta from the aiconfig file
        if 'aiconfig' in cfg:
            meta = load_aiconfig_meta(cfg['aiconfig'], username)
            # config_type from aiconfig file overrides .config type field
            # but .config type field takes precedence if set
            if 'type' in cfg:
                meta['config_type'] = cfg['type']
            clean.update(meta)
        # .config-level icon takes precedence over aiconfig file icon
        if 'icon' in cfg and cfg['icon']:
            clean['icon'] = cfg['icon']

        # Ensure config_type defaults to 'llm'
        if 'config_type' not in clean:
            clean['config_type'] = 'agent' if clean.get('type') == 'agent' else 'llm'

        result.append(clean)

    return jsonify(result)


# --- Streaming AI API ---
# Active streams: {stream_id: {'q': Queue, 'done': bool, 'thread': Thread}}
_active_streams = {}
_active_streams_lock = threading.Lock()

# Per-project SSE subscribers: {task_id: [Queue, ...]}
# Agents (local or remote) that POST to /api/project/<task_id>/report cause
# their message to be fanned out to every live browser subscribed here.
_project_subscribers: dict = {}
_project_subs_lock = threading.Lock()

# Per-user notification SSE subscribers: {username: [Queue, ...]}
_notif_subscribers: dict = {}
_notif_subs_lock   = threading.Lock()
NOTIFICATIONS_MAX  = 100  # keep at most 100 notifications per user


def ensure_project_token(proj_dir):
    """Get or create the per-project auth token (64-char hex string).

    The token is stored in <proj_dir>/.token with owner-only read permission.
    Agents receive the token via the LOS_PROJECT_TOKEN env var and include it
    as 'Authorization: Bearer <token>' when POSTing to /api/project/<id>/report.
    """
    token_file = os.path.join(proj_dir, '.token')
    if os.path.exists(token_file):
        try:
            with open(token_file, 'r') as f:
                token = f.read().strip()
            if len(token) == 64 and re.match(r'^[0-9a-f]+$', token):
                return token
        except OSError:
            pass
    token = secrets.token_hex(32)   # 64 lowercase hex chars
    os.makedirs(proj_dir, exist_ok=True)
    try:
        with open(token_file, 'w') as f:
            f.write(token)
        try:
            os.chmod(token_file, 0o600)   # owner-read only (best-effort)
        except OSError:
            pass
    except OSError as e:
        print(f"Warning: could not write project token to {token_file}: {e}")
    return token


def update_job_status(proj_dir, job_id, status=None, **kwargs):
    """Create or update a job manifest at <proj_dir>/jobs/<job_id>.json."""
    if not proj_dir or not job_id:
        return
    jobs_dir = os.path.join(proj_dir, 'jobs')
    os.makedirs(jobs_dir, exist_ok=True)
    job_file = os.path.join(jobs_dir, f"{job_id}.json")
    job = {}
    if os.path.exists(job_file):
        try:
            with open(job_file, 'r') as f:
                job = json.load(f)
        except Exception:
            job = {}
    job['id'] = job_id
    if status:
        job['status'] = status
    job['last_updated'] = datetime.now(timezone.utc).isoformat()
    for k, v in kwargs.items():
        job[k] = v
    try:
        with open(job_file, 'w') as f:
            json.dump(job, f, indent=2)
    except Exception as e:
        print(f"Error writing job manifest {job_file}: {e}")


def _push_to_project_subscribers(task_id, event_data):
    """Fan-out an event dict to all live SSE queues subscribed to task_id."""
    with _project_subs_lock:
        queues = list(_project_subscribers.get(task_id, []))
    for q in queues:
        try:
            q.put_nowait(event_data)
        except Exception:
            pass


def _cleanup_stream(stream_id):
    """Remove a stream from the active streams dict."""
    with _active_streams_lock:
        _active_streams.pop(stream_id, None)


def _run_aicall_streaming(stream_id, aicall_path, label, prompt, username, timeout,
                          project_id=None, entity_path=None, context_mode='task',
                          job_id=None):
    """Run aicall in a subprocess, pushing output lines to the stream queue.

    Env vars set for the subprocess control system-prompt assembly in aicall.py:
      LOS_CONTEXT_MODE         – 'task', 'entity', or 'none' (UI-supplied; default 'task')
      LOS_ENTITY_REL_PATH      – when context_mode='entity', the relative entity path
      LOS_SKIP_TASKS_DUMP      – 1 → omit the full agenda-wide CURRENT TASKS section
      LOS_SKIP_MEMORY_CONTEXT  – 1 → omit the memory-based RECENT CONVERSATION block
      LOS_PROJECT_TOKEN        – per-project Bearer token (new)
      LOS_REPORT_URL           – full URL agents POST to with their responses (new)
      LOS_JOB_ID               – job identifier for this invocation (new)
    """
    import subprocess
    q = _active_streams[stream_id]['q']

    # --- Job setup ---
    proj_dir = None
    if project_id:
        entity_abs = (
            os.path.normpath(os.path.join(LOS_BASE_PATH, username, entity_path))
            if entity_path else os.path.join(LOS_BASE_PATH, username)
        )
        proj_dir = os.path.join(entity_abs, 'data', 'project', str(project_id))

    if not job_id:
        job_id = f"job_{uuid.uuid4().hex[:12]}"

    project_token = ''
    report_url    = ''
    webhook_url   = ''
    if proj_dir:
        os.makedirs(proj_dir, exist_ok=True)
        project_token = ensure_project_token(proj_dir)
        entity_param  = entity_path or ''
        self_base     = get_self_base_url()
        report_url    = (
            f"{self_base}/api/project/{project_id}/report"
            f"?user={username}&path={entity_param}"
        )
        # Webhook URL for openclaw --deliver callbacks (token in URL, no session
        # needed).  Carries label= so the receiver knows which agent sent the
        # callback even if the agent rewrites job= or schedules follow-up work.
        webhook_url = _build_agent_webhook_url(
            self_base, project_id, username, entity_param, project_token,
            job_id, label)
        # Write job manifest (status = running)
        update_job_status(proj_dir, job_id,
                          status='running',
                          label=label,
                          started_at=datetime.now(timezone.utc).isoformat(),
                          project_id=project_id)

    # For UI-driven calls we always slim the system prompt:
    #   - The AI can pull todos/calendar via MCP when it actually needs them.
    #   - The canvas already shows convo history so we skip the memory_list block.
    env = {
        **os.environ,
        'USER': username, 'LOGNAME': username,
        'PYTHONUNBUFFERED': '1',
        'LOS_PROJECT_DIR':       proj_dir or '',
        'LOS_CONTEXT_MODE':      context_mode or 'task',
        'LOS_SKIP_TASKS_DUMP':   '1',
        'LOS_SKIP_MEMORY_CONTEXT': '1',
        'LOS_PROJECT_TOKEN':     project_token,
        'LOS_REPORT_URL':        report_url,
        'LOS_JOB_ID':            job_id,
        'LOS_WEBHOOK_URL':       webhook_url,
        # Full per-task/entity conversation history — read by aicall.py to feed
        # prior turns to the LLM so it has the context shown in the canvas.
        'LOS_HISTORY_FILE': (os.path.join(proj_dir, 'history.json') if proj_dir else ''),
    }
    if context_mode == 'entity' and entity_path is not None:
        env['LOS_ENTITY_REL_PATH'] = entity_path

    try:
        proc = subprocess.Popen(
            ['python3', aicall_path, label],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.join(LOS_BASE_PATH, username),
            env=env,
        )

        # Send prompt and close stdin
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass

        # Stream stderr (thinking/debug) in a separate thread
        def read_stderr():
            for line in proc.stderr:
                with _active_streams_lock:
                    stream = _active_streams.get(stream_id)
                if stream:
                    stream['q'].put(('thinking', line.rstrip('\n')))
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        # Stream stdout (main output) line by line; also fan-out to project SSE
        for line in proc.stdout:
            stripped = line.rstrip('\n')
            with _active_streams_lock:
                stream = _active_streams.get(stream_id)
            if not stream:
                break
            stream['q'].put(('output', stripped))
            # Fan out to any browser subscribed to this project's live feed
            if project_id and stripped:
                _push_to_project_subscribers(str(project_id), {
                    'type': 'output',
                    'data': {'job_id': job_id, 'line': stripped}
                })

        proc.wait(timeout=timeout)
        stderr_thread.join(timeout=5)

        returncode = proc.returncode
        with _active_streams_lock:
            stream = _active_streams.get(stream_id)
        if stream:
            stream['q'].put(('done', str(returncode)))
            stream['done'] = True

        # Update job manifest to done/error
        if proj_dir:
            final_status = 'done' if returncode == 0 else 'error'
            update_job_status(proj_dir, job_id,
                              status=final_status,
                              finished_at=datetime.now(timezone.utc).isoformat(),
                              returncode=returncode)
            # Notify project SSE subscribers that the job finished
            _push_to_project_subscribers(str(project_id) if project_id else '', {
                'type': 'job_done',
                'data': {'job_id': job_id, 'status': final_status, 'returncode': returncode}
            })

        # Consume any legacy .agent_pending.json written by older aicall versions
        if proj_dir:
            pending_file = os.path.join(proj_dir, '.agent_pending.json')
            if os.path.exists(pending_file):
                try:
                    with open(pending_file, 'r') as f:
                        pending_data = json.load(f)
                    pending_msg = pending_data.get('message', '')
                    if pending_msg:
                        # Write directly to history (no HTTP round-trip needed)
                        history_file = os.path.join(proj_dir, 'history.json')
                        history = []
                        if os.path.exists(history_file):
                            try:
                                with open(history_file, 'r', encoding='utf-8') as f:
                                    history = json.load(f)
                            except Exception:
                                history = []
                        now_str = datetime.now(timezone.utc).isoformat()
                        pending_msg_obj = {
                            'id':        f"msg_{int(datetime.now().timestamp() * 1000)}_{uuid.uuid4().hex[:8]}",
                            'role':      'assistant',
                            'label':     label,
                            'content':   pending_msg,
                            'job_id':    job_id,
                            'timestamp': now_str,
                        }
                        history.append(pending_msg_obj)
                        with open(history_file, 'w', encoding='utf-8') as f:
                            json.dump(history, f, indent=2, ensure_ascii=False)
                        _push_to_project_subscribers(str(project_id) if project_id else '', {
                            'type': 'message',
                            'data': pending_msg_obj
                        })
                    os.remove(pending_file)
                except Exception as e:
                    print(f"[stream] Error consuming pending agent file: {e}")

    except Exception as e:
        with _active_streams_lock:
            stream = _active_streams.get(stream_id)
        if stream:
            stream['q'].put(('error', str(e)))
            stream['q'].put(('done', '-1'))
            stream['done'] = True
        if proj_dir:
            update_job_status(proj_dir, job_id, status='error',
                              finished_at=datetime.now(timezone.utc).isoformat(),
                              error=str(e))


@app.route('/api/ai/stream', methods=['POST'])
@login_required
def api_ai_stream_start():
    """Start a streaming AI call. Returns a stream_id to connect to via SSE."""
    import subprocess
    data = request.json
    if not data:
        return jsonify({"error": "Invalid request data"}), 400

    label = data.get('label', '')
    prompt = data.get('prompt', '')

    if not label:
        return jsonify({"error": "AI configuration label is required"}), 400
    if not prompt:
        return jsonify({"error": "Prompt is required"}), 400

    username = session.get('username')

    # Validate label
    configs = parse_ai_configs_from_config(username)
    valid_labels = [c.get('label', '') for c in configs]
    if label not in valid_labels:
        return jsonify({"error": f"Invalid AI configuration label: {label}"}), 400

    matched_cfg = next((c for c in configs if c.get('label') == label), None)

    # Get timeout from aiconfig
    subprocess_timeout = 600
    if matched_cfg and matched_cfg.get('aiconfig'):
        try:
            aiconfig_path = matched_cfg['aiconfig'].strip('"').strip()
            if not os.path.isabs(aiconfig_path):
                aiconfig_path = os.path.join(LOS_BASE_PATH, username, aiconfig_path)
            with open(aiconfig_path, 'r') as f:
                aiconfig_data = json.load(f)
            llm_timeouts = [llm.get('timeout', 120) for llm in aiconfig_data.get('llms', [])]
            if llm_timeouts:
                subprocess_timeout = max(llm_timeouts) + 30
            elif aiconfig_data.get('timeout'):
                # Agent configs (no 'llms' array) can set top-level 'timeout'
                subprocess_timeout = int(aiconfig_data['timeout']) + 30
        except Exception:
            pass

    aicall_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'aicall', 'aicall.py')

    # Context mode controls how aicall.py assembles its system prompt.
    # Default to 'task' (preserves legacy behavior) if unspecified.
    context_mode = data.get('context_mode') or 'task'
    if context_mode not in ('task', 'entity', 'none'):
        context_mode = 'task'

    # Generate a job_id for tracking this invocation in the project's jobs/
    job_id    = f"job_{uuid.uuid4().hex[:12]}"
    stream_id = str(uuid.uuid4())
    q = queue.Queue()
    stream_info = {
        'q': q, 'done': False, 'label': label, 'username': username,
        'project_id': data.get('project_id'), 'entity_path': data.get('entity_path'),
        'context_mode': context_mode,
        'job_id': job_id,
    }

    with _active_streams_lock:
        _active_streams[stream_id] = stream_info

    t = threading.Thread(
        target=_run_aicall_streaming,
        args=(stream_id, aicall_path, label, prompt, username, subprocess_timeout),
        kwargs={
            'project_id':   data.get('project_id'),
            'entity_path':  data.get('entity_path'),
            'context_mode': context_mode,
            'job_id':       job_id,
        },
        daemon=True
    )
    t.start()
    stream_info['thread'] = t

    return jsonify({"stream_id": stream_id, "job_id": job_id})



@app.route('/api/ai/stream/<stream_id>', methods=['GET'])
@login_required
def api_ai_stream_events(stream_id):
    """SSE endpoint — connect here with EventSource to receive streaming output."""
    from flask import Response, stream_with_context

    username = session.get('username')

    with _active_streams_lock:
        stream = _active_streams.get(stream_id)

    if not stream:
        return jsonify({"error": "Stream not found"}), 404

    # Security: only the owner can read their stream
    if stream.get('username') != username:
        return jsonify({"error": "Unauthorized"}), 403

    def generate():
        try:
            while True:
                with _active_streams_lock:
                    s = _active_streams.get(stream_id)
                if not s:
                    yield "event: error\ndata: Stream lost\n\n"
                    return

                try:
                    event_type, content = s['q'].get(timeout=30)
                except queue.Empty:
                    # Send keepalive
                    yield ": keepalive\n\n"
                    continue

                # Escape newlines for SSE data field
                safe_content = content.replace('\n', '\\n')
                yield f"event: {event_type}\ndata: {safe_content}\n\n"

                if event_type == 'done':
                    _cleanup_stream(stream_id)
                    return
                elif event_type == 'error':
                    _cleanup_stream(stream_id)
                    return
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'close'
        }
    )


@app.route('/api/ai/stream/<stream_id>/status', methods=['GET'])
@login_required
def api_ai_stream_status(stream_id):
    """Check status of a streaming call."""
    username = session.get('username')
    with _active_streams_lock:
        stream = _active_streams.get(stream_id)

    if not stream:
        return jsonify({"active": False, "stream_id": stream_id})

    if stream.get('username') != username:
        return jsonify({"error": "Unauthorized"}), 403

    return jsonify({
        "active": True,
        "done": stream.get('done', False),
        "label": stream.get('label', ''),
        "stream_id": stream_id
    })


# --- Suppress noisy client-disconnect errors from the werkzeug logger ---
#
# Werkzeug logs "Error on request:\n<traceback>" via the standard Python
# logging module (logger name 'werkzeug') using BaseWSGIServer.log() →
# werkzeug._internal._log() → logging.getLogger('werkzeug').error(...).
# Overriding WSGIRequestHandler.log_error does NOT intercept this path.
# A logging.Filter on the 'werkzeug' logger is the correct approach.
import logging
from werkzeug.serving import WSGIRequestHandler

class _SuppressClientDisconnectFilter(logging.Filter):
    """Drop 'Error on request' log records that are purely about client disconnects."""
    _NOISE = ('BrokenPipeError', 'UNEXPECTED_EOF_WHILE_READING', 'ConnectionResetError')

    def filter(self, record):
        msg = record.getMessage()
        if 'Error on request' in msg and any(e in msg for e in self._NOISE):
            return False   # suppress
        return True        # keep everything else

logging.getLogger('werkzeug').addFilter(_SuppressClientDisconnectFilter())


class SilentDisconnectHandler(WSGIRequestHandler):
    """Request handler kept for any remaining log_error-path SSL messages."""

    def log_error(self, format, *args):
        # Werkzeug also calls log_error(self, 'SSL error occurred: %s', e)
        # for SSL errors raised in handle().  Suppress client-disconnect noise
        # from that path too.
        if args:
            exc_str = str(args[0])
            if 'UNEXPECTED_EOF_WHILE_READING' in exc_str or 'BrokenPipeError' in exc_str:
                return
        super().log_error(format, *args)


# --- Server Execution ---
if __name__ == '__main__':
    # Generate SSL certificate if it doesn't exist
    ssl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ssl')
    os.makedirs(ssl_dir, exist_ok=True)

    cert_path = os.path.join(ssl_dir, 'cert.pem')
    key_path = os.path.join(ssl_dir, 'key.pem')

    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        print("Generating self-signed SSL certificate...")
        try:
            # Use openssl command to generate cert and key
            os.system(f"openssl req -x509 -newkey rsa:4096 -nodes -out {cert_path} -keyout {key_path} -days 3650 -subj '/CN=localhost'")
            print("SSL certificate generated.")
        except Exception as e:
            print(f"Error generating SSL certificate: {e}")
            sys.exit(1)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert_path, key_path)
    except ssl.SSLError as e:
        print(f"Error loading SSL certificate: {e}")
        print("Please ensure the certificate and key files are valid.")
        sys.exit(1)
    except FileNotFoundError:
         print(f"Error: SSL certificate or key file not found at expected location ({ssl_dir}).")
         sys.exit(1)

    # Host/port/debug come from /los/sys/config.json via the gateway
    # (los gateway); the defaults preserve the previous standalone behaviour.
    host = os.environ.get('LOS_WEB_HOST', '0.0.0.0')
    port = int(os.environ.get('LOS_WEB_PORT', '14001'))
    debug = os.environ.get('LOS_WEB_DEBUG', '0') == '1'

    print(f"Starting Flask server with SSL on {host}:{port}...")
    app.run(host=host, port=port, ssl_context=context, debug=debug, use_reloader=False, request_handler=SilentDisconnectHandler)
