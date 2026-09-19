from flask import Flask, request, jsonify
import json
import os
import uuid
from datetime import datetime
import re

app = Flask(__name__)

# --- Helper Functions ---
def append_safe(file_path, line):
    """Safely appends a line to a file, adding newline if needed."""
    print(f"[DEBUG] append_safe: file_path='{file_path}'")
    try:
        dirname = os.path.dirname(file_path)
        print(f"[DEBUG] append_safe: creating directory '{dirname}'")
        os.makedirs(dirname, exist_ok=True)
        print(f"[DEBUG] append_safe: directory created/exists")
        # Ensure file exists before appending
        if not os.path.exists(file_path):
            print(f"[DEBUG] append_safe: file doesn't exist, creating")
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(line + '\n')
            print(f"[DEBUG] append_safe: file created successfully")
        else:
            print(f"[DEBUG] append_safe: file exists, appending")
            # Open in append+read ('a+') mode to allow reading the last char
            with open(file_path, 'a+', encoding='utf-8') as f:
                # Add newline if file doesn't end with one
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(f.tell() - 1, os.SEEK_SET)
                    if f.read(1) != '\n':
                        f.write('\n')
                f.write(line + '\n')
            print(f"[DEBUG] append_safe: file appended successfully")
        return True
    except Exception as e:
        print(f"[DEBUG] Error appending to file {file_path}: {e}")
        import traceback
        print(f"[DEBUG] Traceback: {traceback.format_exc()}")
        return False

def get_memory_file_path(memory_path):
    """Get the full path to the memory file within the memory directory."""
    if not memory_path:
        return None
    return os.path.join(memory_path, "memory.jsonl")

def read_memory_file(memory_path):
    """Read all memory entries from the memory file."""
    if not memory_path:
        return {"error": "No memory_path provided"}

    memory_file = get_memory_file_path(memory_path)
    if not os.path.exists(memory_path):
        os.makedirs(memory_path, exist_ok=True)

    entries = []
    if os.path.exists(memory_file):
        try:
            with open(memory_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            entries.append(entry)
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            return {"error": f"Failed to read memory file: {e}"}

    return entries

def write_memory_file(memory_path, entries):
    """Write all memory entries back to the memory file."""
    if not memory_path:
        return False

    memory_file = get_memory_file_path(memory_path)
    memory_dir = memory_path
    if not os.path.exists(memory_dir):
        os.makedirs(memory_dir, exist_ok=True)

    try:
        with open(memory_file, 'w', encoding='utf-8') as f:
            for entry in entries:
                f.write(json.dumps(entry) + '\n')
        return True
    except Exception as e:
        print(f"Error writing to memory file {memory_file}: {e}")
        return False

# --- Memory MCP Configuration ---
MCP_CONFIG = {
    "name": "memory-management",
    "tools": {
        "memory_store": {
            "description": "Store information in memory with optional tags and categories.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_path": {"type": "string", "description": "Path to the memory directory"},
                    "content": {"type": "string", "description": "The content to store"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for categorization"},
                    "category": {"type": "string", "description": "Optional category name"},
                    "id": {"type": "string", "description": "Optional custom ID (auto-generated if not provided)"}
                },
                "required": ["memory_path", "content"]
            }
        },
        "memory_retrieve": {
            "description": "Retrieve memory entries by ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_path": {"type": "string", "description": "Path to the memory directory"},
                    "id": {"type": "string", "description": "ID of the memory entry to retrieve"}
                },
                "required": ["memory_path", "id"]
            }
        },
        "memory_list": {
            "description": "List memory entries with optional filtering.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_path": {"type": "string", "description": "Path to the memory directory"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags"},
                    "category": {"type": "string", "description": "Filter by category"},
                    "limit": {"type": "integer", "description": "Maximum number of entries to return", "default": 20},
                    "offset": {"type": "integer", "description": "Offset for pagination", "default": 0}
                },
                "required": ["memory_path"]
            }
        },
        "memory_search": {
            "description": "Search memory entries by content text (full-text search).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_path": {"type": "string", "description": "Path to the memory directory"},
                    "query": {"type": "string", "description": "Search query string"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag filter"},
                    "category": {"type": "string", "description": "Optional category filter"},
                    "limit": {"type": "integer", "description": "Maximum number of entries to return", "default": 20}
                },
                "required": ["memory_path", "query"]
            }
        },
        "memory_update": {
            "description": "Update an existing memory entry.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_path": {"type": "string", "description": "Path to the memory directory"},
                    "id": {"type": "string", "description": "ID of the entry to update"},
                    "content": {"type": "string", "description": "New content"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "New tags"},
                    "category": {"type": "string", "description": "New category"}
                },
                "required": ["memory_path", "id"]
            }
        },
        "memory_delete": {
            "description": "Delete a memory entry by ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_path": {"type": "string", "description": "Path to the memory directory"},
                    "id": {"type": "string", "description": "ID of the entry to delete"}
                },
                "required": ["memory_path", "id"]
            }
        }
    }
}

@app.route('/', methods=['GET'])
def get_config():
    return jsonify(MCP_CONFIG)

@app.route('/tools/<tool_name>', methods=['POST'])
def use_tool(tool_name):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [MEMORY_MCP] Tool called: {tool_name}")
    if tool_name not in MCP_CONFIG["tools"]:
        print(f"[{timestamp}] [MEMORY_MCP] Tool not found in config: {tool_name}")
        return jsonify({"error": "Tool not found"}), 404

    try:
        data = request.get_json()
        print(f"[{timestamp}] [MEMORY_MCP] Input received for '{tool_name}': {json.dumps(data, default=str)}")
    except Exception as e:
        print(f"[{timestamp}] [MEMORY_MCP] Invalid JSON in request: {e}")
        return jsonify({"error": f"Invalid JSON in request: {e}"}), 400

    memory_path = data.get("memory_path")
    if not memory_path:
        return jsonify({"error": "memory_path is required"}), 400

    print(f"[{timestamp}] [MEMORY_MCP] memory_path='{memory_path}'")

    try:
        if tool_name == "memory_store":
            content = data.get("content")
            if not content:
                return jsonify({"error": "content is required"}), 400

            entry_id = data.get("id", str(uuid.uuid4()))
            tags = data.get("tags", [])
            category = data.get("category", "")

            # Check for duplicate ID
            entries = read_memory_file(memory_path)
            if isinstance(entries, dict) and "error" in entries:
                return jsonify(entries), 500

            if any(e.get("id") == entry_id for e in entries):
                return jsonify({"error": f"Entry with ID {entry_id} already exists"}), 400

            # Create new entry
            now = datetime.now().isoformat()
            new_entry = {
                "id": entry_id,
                "content": content,
                "tags": tags,
                "category": category,
                "timestamp": now,
                "updated": now
            }

            if not append_safe(get_memory_file_path(memory_path), json.dumps(new_entry)):
                return jsonify({"error": "Failed to save memory entry"}), 500

            return jsonify({"result": f"Memory entry stored with ID: {entry_id}"})

        elif tool_name == "memory_retrieve":
            entry_id = data.get("id")
            if not entry_id:
                return jsonify({"error": "id is required"}), 400

            entries = read_memory_file(memory_path)
            if isinstance(entries, dict) and "error" in entries:
                return jsonify(entries), 500

            for entry in entries:
                if entry.get("id") == entry_id:
                    return jsonify({"result": entry})

            return jsonify({"result": {}})  # Empty result if not found

        elif tool_name == "memory_list":
            tags_filter = data.get("tags", [])
            category_filter = data.get("category", "")
            limit = data.get("limit", 20)
            offset = data.get("offset", 0)

            entries = read_memory_file(memory_path)
            if isinstance(entries, dict) and "error" in entries:
                return jsonify(entries), 500

            # Apply filters.  tags_filter is AND semantics: every requested tag
            # must be present on the entry.  This prevents WhatsApp contact A's
            # history from leaking into contact B's retrieval when the caller
            # asks for ["conversation", "<phone_number>"].
            filtered = []
            for entry in entries:
                if tags_filter and not all(tag in entry.get("tags", []) for tag in tags_filter):
                    continue
                if category_filter and entry.get("category") != category_filter:
                    continue
                filtered.append(entry)

            # Sort by timestamp descending and apply pagination
            filtered.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            paginated = filtered[offset:offset + limit]

            return jsonify({"result": paginated})

        elif tool_name == "memory_search":
            query = data.get("query", "").lower()
            if not query:
                return jsonify({"error": "query is required"}), 400

            tags_filter = data.get("tags", [])
            category_filter = data.get("category", "")
            limit = data.get("limit", 20)

            entries = read_memory_file(memory_path)
            if isinstance(entries, dict) and "error" in entries:
                return jsonify(entries), 500

            # Search content for query, using AND semantics for tags_filter.
            matches = []
            for entry in entries:
                if tags_filter and not all(tag in entry.get("tags", []) for tag in tags_filter):
                    continue
                if category_filter and entry.get("category") != category_filter:
                    continue

                content = entry.get("content", "").lower()
                if query in content:
                    matches.append(entry)

            # Sort by timestamp descending and limit
            matches.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
            limited = matches[:limit]

            return jsonify({"result": limited})

        elif tool_name == "memory_update":
            entry_id = data.get("id")
            if not entry_id:
                return jsonify({"error": "id is required"}), 400

            entries = read_memory_file(memory_path)
            if isinstance(entries, dict) and "error" in entries:
                return jsonify(entries), 500

            # Find and update entry
            updated = False
            for entry in entries:
                if entry.get("id") == entry_id:
                    if "content" in data:
                        entry["content"] = data["content"]
                    if "tags" in data:
                        entry["tags"] = data["tags"]
                    if "category" in data:
                        entry["category"] = data["category"]
                    entry["updated"] = datetime.now().isoformat()
                    updated = True
                    break

            if not updated:
                return jsonify({"error": f"Entry with ID {entry_id} not found"}), 404

            if not write_memory_file(memory_path, entries):
                return jsonify({"error": "Failed to update memory file"}), 500

            return jsonify({"result": f"Memory entry {entry_id} updated"})

        elif tool_name == "memory_delete":
            entry_id = data.get("id")
            if not entry_id:
                return jsonify({"error": "id is required"}), 400

            entries = read_memory_file(memory_path)
            if isinstance(entries, dict) and "error" in entries:
                return jsonify(entries), 500

            # Remove entry
            original_count = len(entries)
            entries = [e for e in entries if e.get("id") != entry_id]

            if len(entries) == original_count:
                return jsonify({"error": f"Entry with ID {entry_id} not found"}), 404

            if not write_memory_file(memory_path, entries):
                return jsonify({"error": "Failed to update memory file"}), 500

            return jsonify({"result": f"Memory entry {entry_id} deleted"})

    except Exception as e:
        return jsonify({"error": f"Internal error: {str(e)}"}), 500

    return jsonify({"error": "Tool implementation not found"}), 501

if __name__ == '__main__':
    # Host/port come from /los/sys/config.json via the gateway (los gateway).
    # The defaults preserve standalone behaviour: `python memory_server.py`
    # still listens on 127.0.0.1:5102 exactly as before.
    _host = os.environ.get('LOS_MEMORY_HOST', '127.0.0.1')
    _port = int(os.environ.get('LOS_MEMORY_PORT', '5102'))
    _debug = os.environ.get('LOS_DEBUG', '0') == '1'
    print(f"Memory MCP Server starting on {_host}:{_port}...")
    app.run(host=_host, port=_port, debug=_debug, use_reloader=False)
