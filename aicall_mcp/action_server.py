from flask import Flask, request, jsonify
import json
import os
import requests
import calendar as cal_module
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Get system username
USERNAME = os.getenv('USER') or os.getenv('LOGNAME') or 'default'

def expand_los_path(path):
    """Expand ~ to /los/{USERNAME} instead of system home directory."""
    if path and path.startswith('~'):
        return f"/los/{USERNAME}{path[1:]}"
    return path

app = Flask(__name__)

# ── Agenda Filter Helpers ────────────────────────────────────────────────────

def get_allowed_agendas(req):
    """
    Read the X-LOS-Allowed-Agendas header from the incoming request.
    Returns a list of absolute allowed-agenda paths, or None if no filter is set
    (meaning all paths are allowed).
    """
    header = req.headers.get('X-LOS-Allowed-Agendas', '').strip()
    if not header:
        return None  # No filter — all agendas allowed
    paths = [p.strip() for p in header.split(',') if p.strip()]
    return paths if paths else None


def is_path_allowed(path, allowed_agendas):
    """
    Check whether *path* falls inside at least one of the allowed_agendas.

    An agenda is represented as an absolute directory path that ends with '/'.
    A file at /los/user/work/data/todo/todo is allowed when '/los/user/work/'
    (or '/los/user/') appears in allowed_agendas.

    If allowed_agendas is None, everything is allowed.
    """
    if allowed_agendas is None:
        return True
    abs_path = os.path.abspath(path)
    for agenda in allowed_agendas:
        # Normalise: ensure agenda ends with /
        agenda_norm = agenda.rstrip('/') + '/'
        if abs_path.startswith(agenda_norm) or abs_path == agenda.rstrip('/'):
            return True
    return False


# ── Cron Evaluation Helpers (mirrors loscron logic) ─────────────────────────

def _cron_field_matches(field_expr, current_value):
    if field_expr == '*':
        return True
    for part in field_expr.split(','):
        part = part.strip()
        step = 1
        if '/' in part:
            part, step_str = part.split('/', 1)
            try:
                step = int(step_str)
            except ValueError:
                continue
        if '-' in part and part != '*':
            try:
                lo, hi = part.split('-', 1)
                lo, hi = int(lo), int(hi)
                if lo <= current_value <= hi and (current_value - lo) % step == 0:
                    return True
            except ValueError:
                continue
        elif part == '*':
            if step > 0 and current_value % step == 0:
                return True
        else:
            try:
                if int(part) == current_value:
                    return True
            except ValueError:
                continue
    return False


def _py_weekday_to_cron(py_weekday):
    """Python weekday: 0=Mon..6=Sun  →  cron: 0=Sun..6=Sat"""
    return (py_weekday + 1) % 7


def _cron_expression_matches(expression, dt):
    parts = expression.strip().split()
    if len(parts) != 5:
        return False
    minute_e, hour_e, dom_e, month_e, dow_e = parts
    return (
        _cron_field_matches(minute_e, dt.minute) and
        _cron_field_matches(hour_e, dt.hour) and
        _cron_field_matches(dom_e, dt.day) and
        _cron_field_matches(month_e, dt.month) and
        _cron_field_matches(dow_e, _py_weekday_to_cron(dt.weekday()))
    )


def _compute_easter(year):
    a = year % 19; b = year // 100; c = year % 100
    d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30; i = c // 4; k = c % 4
    l = (32 + 2*e + 2*i - h - k) % 7; m = (a + 11*h + 22*l) // 451
    month = (h + l - 7*m + 114) // 31
    day = ((h + l - 7*m + 114) % 31) + 1
    return datetime(year, month, day)


def _resolve_cron_date(entry, year):
    rule = entry.get('rule')
    try:
        if rule == 'easter':
            return _compute_easter(year) + timedelta(days=entry.get('offset', 0))
        elif rule == 'nth_weekday':
            m = entry.get('month'); n = entry.get('nth'); wd = entry.get('weekday')
            if None in (m, n, wd):
                return None
            first = datetime(year, m, 1)
            ahead = wd - first.weekday()
            if ahead < 0:
                ahead += 7
            target = first + timedelta(days=ahead) + timedelta(weeks=n - 1)
            return target if target.month == m else None
        elif rule == 'last_weekday':
            m = entry.get('month'); wd = entry.get('weekday')
            if None in (m, wd):
                return None
            last = datetime(year, m, cal_module.monthrange(year, m)[1])
            while last.weekday() != wd:
                last -= timedelta(days=1)
            return last
        else:
            return datetime(year, entry['month'], entry['day'])
    except Exception:
        return None


def _cron_entry_fires_on_date(entry, check_date):
    """Return True if a cron-file entry fires on check_date (date object)."""
    etype = entry.get('type', '')
    if not entry.get('enabled', True):
        return False

    if etype == 'date':
        entry_year = entry.get('year')
        if entry_year is not None and check_date.year != entry_year:
            return False
        resolved = _resolve_cron_date(entry, check_date.year)
        return resolved is not None and resolved.date() == check_date

    elif etype == 'cron':
        expression = entry.get('expression')
        if not expression:
            schedule = entry.get('schedule', {})
            if schedule:
                expression = "{} {} {} {} {}".format(
                    schedule.get('minute', '*'), schedule.get('hour', '*'),
                    schedule.get('dayOfMonth', '*'), schedule.get('month', '*'),
                    schedule.get('dayOfWeek', '*'))
            else:
                return False
        # Check at a representative time (00:00) — we only care about the date
        dt = datetime(check_date.year, check_date.month, check_date.day, 0, 0)
        # For date-range purposes check all minutes would be expensive;
        # instead test whether the date portion (dom / month / dow) matches
        parts = expression.strip().split()
        if len(parts) != 5:
            return False
        _, _, dom_e, month_e, dow_e = parts
        return (
            _cron_field_matches(dom_e, check_date.day) and
            _cron_field_matches(month_e, check_date.month) and
            _cron_field_matches(dow_e, _py_weekday_to_cron(check_date.weekday()))
        )

    elif etype == 'cycle':
        interval = entry.get('interval')
        anchor = entry.get('anchor')
        if not interval or not anchor:
            return False
        try:
            anchor_dt = datetime.fromisoformat(anchor)
        except (ValueError, TypeError):
            return False
        delta = timedelta(
            days=interval.get('days', 0),
            hours=interval.get('hours', 0),
            minutes=interval.get('minutes', 0))
        if delta.total_seconds() <= 0:
            return False
        last_run = entry.get('last_run')
        ref = anchor_dt
        if last_run:
            try:
                ref = datetime.fromisoformat(last_run)
            except (ValueError, TypeError):
                pass
        check_dt = datetime(check_date.year, check_date.month, check_date.day)
        next_fire = ref + delta
        return check_dt.date() >= next_fire.date()

    return False


def _find_entity_dirs(base_path):
    """
    Recursively collect all entity directory absolute paths under base_path.
    Follows 'entities' files just like loscron does.
    The base_path itself is always included.
    """
    result = [base_path]
    entities_file = os.path.join(base_path, 'entities')
    if os.path.isfile(entities_file):
        try:
            with open(entities_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    entity_name = line.split()[0]
                    sub = os.path.join(base_path, entity_name)
                    if os.path.isdir(sub):
                        result.extend(_find_entity_dirs(sub))
        except OSError:
            pass
    return result


# --- Helper Functions ---
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

# This is a placeholder for the MCP server configuration.
# In a real-world scenario, this would be more dynamic.
MCP_CONFIG = {
    "name": "aicall-actions",
    "tools": {
        "file_write": {
            "description": "Write content to a file. This will overwrite the file if it exists.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        },
        "file_read": {
            "description": "Read the content of a file.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        },
        "execute_command": {
            "description": "Execute a system command. For security, this is currently a dry run and will only log the command.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"}
                },
                "required": ["command"]
            }
        },
        "update_delegatable_scores": {
            "description": "Update delegatable_score fields in task files based on a list of updates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "score": {"type": "number"}
                            },
                            "required": ["id", "score"]
                        }
                    }
                },
                "required": ["updates"]
            }
        },
        "whatsapp_send": {
            "description": "Send a WhatsApp message to a specified number.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number"},
                    "message": {"type": "string"}
                },
                "required": ["to", "message"]
            }
        },
        "todo_add": {
            "description": "Add a new todo item to the agenda. Specify the relative entity path (e.g., 'work/project1' or empty for root).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the agenda (empty for root)", "default": ""},
                    "text": {"type": "string", "description": "The main text of the todo item"},
                    "priority": {"type": "number", "description": "Priority from 0.0 to 9.9 (higher is more important)", "default": 1.0, "minimum": 0.0, "maximum": 9.9},
                    "status": {"type": "string", "description": "Status of the todo item", "default": "pending", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    "assigned_to": {"type": "string", "description": "Optional person assigned to this task"},
                    "deadline": {"type": "string", "description": "Optional deadline in ISO format", "format": "date-time"},
                    "delegatable_score": {"type": "number", "description": "Delegation score from 0.0 to 1.0", "minimum": 0.0, "maximum": 1.0}
                },
                "required": ["text"]
            }
        },
        "calendar_add": {
            "description": "Add a new calendar event to the agenda. Specify the relative entity path (e.g., 'work/project1' or empty for root).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path within the agenda (empty for root)", "default": ""},
                    "title": {"type": "string", "description": "Title of the event"},
                    "start": {"type": "string", "description": "Start time in ISO format", "format": "date-time"},
                    "end": {"type": "string", "description": "End time in ISO format (optional)", "format": "date-time"},
                    "description": {"type": "string", "description": "Optional description of the event"},
                    "content": {"type": "string", "description": "Optional content or details of the event"},
                    "backgroundColor": {"type": "string", "description": "Background color (e.g., '#3788d8')", "default": "#3788d8"},
                    "status": {"type": "string", "description": "Status of the event", "default": "tentative", "enum": ["tentative", "confirmed", "cancelled"]}
                },
                "required": ["title", "start"]
            }
        },
        "todo_update": {
            "description": "Update fields on an existing todo item by ID. Pass any fields you want to change (e.g. status, priority, text, assigned_to, deadline, delegatable_score). If status is set to 'done', the item is moved from the todo file to the done file automatically.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The ID of the todo item to update"},
                    "fields": {
                        "type": "object",
                        "description": "Key-value pairs of fields to update (e.g. {\"status\": \"done\"}, {\"priority\": 3.5, \"text\": \"Updated task\"}, {\"assigned_to\": \"Alice\"})"
                    },
                    "path": {"type": "string", "description": "Relative entity path to narrow the search (empty for root)", "default": ""}
                },
                "required": ["id", "fields"]
            }
        },
        "calendar_update": {
            "description": "Update fields on an existing calendar event by ID. Pass any fields you want to change. Top-level fields like title, start, end, backgroundColor are updated directly. Fields like status, description, content are updated inside extendedProps.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The ID of the calendar event to update"},
                    "fields": {
                        "type": "object",
                        "description": "Key-value pairs of fields to update (e.g. {\"status\": \"confirmed\"}, {\"title\": \"New Title\"}, {\"description\": \"Updated desc\"})"
                    },
                    "path": {"type": "string", "description": "Relative entity path to narrow the search (empty for root)", "default": ""}
                },
                "required": ["id", "fields"]
            }
        },
        "calendar_pull": {
            "description": "Pull all calendar events and scheduled cron entries for a date range across all accessible agendas. Returns calendar events (from data/calendar files) and cron events (from data/cron files) that fall within the specified dates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format (inclusive)"},
                    "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format (inclusive)"}
                },
                "required": ["start_date", "end_date"]
            }
        },
        "todo_pull": {
            "description": "Pull all todo items across all accessible agendas. Returns pending and in-progress todos. Optionally narrow to a specific entity path.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional relative entity path to narrow scope (empty for all agendas)", "default": ""}
                },
                "required": []
            }
        },
        "cron_add": {
            "description": (
                "Add a new entry to the cron/schedule system. Supports three types:\n"
                "  'date'  – Annual or one-time date events (birthdays, anniversaries, holidays).\n"
                "            Required: month (1-12), day (1-31). Optional: year (omit for annual).\n"
                "  'cron'  – Traditional 5-field cron expression (minute hour dom month dow).\n"
                "            Required: expression (e.g. '0 9 * * 1' = every Monday 9 AM).\n"
                "  'cycle' – Interval-based repeating event.\n"
                "            Required: interval ({days, hours, minutes}), anchor (ISO datetime string).\n"
                "action can be 'display' (show in calendar, default), 'execute' (run command), or 'notify'.\n"
                "Examples:\n"
                "  Birthday: {type:'date', title:'Mom birthday', month:4, day:12}\n"
                "  Weekly:   {type:'cron', title:'Review', expression:'0 10 * * 1'}\n"
                "  Cycle:    {type:'cycle', title:'Water plants', interval:{days:3}, anchor:'2026-02-18T08:00:00'}"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Entry type: 'date', 'cron', or 'cycle'", "enum": ["date", "cron", "cycle"]},
                    "title": {"type": "string", "description": "Display title for this entry"},
                    "description": {"type": "string", "description": "Optional longer description", "default": ""},
                    "action": {"type": "string", "description": "What to do when fired: 'display', 'execute', or 'notify'", "default": "display", "enum": ["display", "execute", "notify"]},
                    "command": {"type": "string", "description": "Shell command to run (only for action='execute')"},
                    "enabled": {"type": "boolean", "description": "Whether this entry is active", "default": True},
                    "path": {"type": "string", "description": "Relative entity path (empty for root agenda)", "default": ""},
                    "month": {"type": "integer", "description": "[date type] Month (1-12)"},
                    "day": {"type": "integer", "description": "[date type] Day of month (1-31)"},
                    "year": {"type": "integer", "description": "[date type] Year for one-time events (omit for annual recurring)"},
                    "expression": {"type": "string", "description": "[cron type] 5-field cron expression: 'minute hour dom month dow'"},
                    "interval": {
                        "type": "object",
                        "description": "[cycle type] Repeat interval — specify at least one of days/hours/minutes",
                        "properties": {
                            "days": {"type": "integer", "default": 0},
                            "hours": {"type": "integer", "default": 0},
                            "minutes": {"type": "integer", "default": 0}
                        }
                    },
                    "anchor": {"type": "string", "description": "[cycle type] ISO datetime when the cycle starts (e.g. '2026-02-18T08:00:00')"}
                },
                "required": ["type", "title"]
            }
        },
        "history_read": {
            "description": (
                "Read the conversation history for an entity/project. Returns the most recent "
                "user/assistant turns with timestamps and speaker labels. Use this when you need "
                "to recall what was just discussed in the current entity context. "
                "Either provide history_file directly, or provide entity_path + project_id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "history_file": {"type": "string", "description": "Absolute path to history.json (overrides entity_path/project_id)"},
                    "entity_path": {"type": "string", "description": "Relative entity path, e.g. 'money/gainwell' (empty for root)", "default": ""},
                    "project_id": {"type": "string", "description": "Project id under data/project/", "default": "entity"},
                    "hours": {"type": "number", "description": "Only include entries from the last N hours", "default": 4},
                    "limit": {"type": "integer", "description": "Maximum number of entries to return", "default": 100}
                },
                "required": []
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
    print(f"[{timestamp}] [ACTION_MCP] Tool called: {tool_name}")
    if tool_name not in MCP_CONFIG["tools"]:
        print(f"[{timestamp}] [ACTION_MCP] Tool not found in config: {tool_name}")
        return jsonify({"error": "Tool not found"}), 404

    try:
        data = request.get_json()
        print(f"[{timestamp}] [ACTION_MCP] Input received for '{tool_name}': {json.dumps(data, default=str)}")
    except Exception as e:
        print(f"[{timestamp}] [ACTION_MCP] Invalid JSON in request: {e}")
        return jsonify({"error": f"Invalid JSON in request: {e}"}), 400

    if tool_name == "file_write":
        path = expand_los_path(data.get("path"))
        content = data.get("content")
        if not path or not content:
            return jsonify({"error": "Missing 'path' or 'content' for file_write"}), 400
        try:
            # Basic security check to prevent writing outside of /los/
            if not os.path.abspath(path).startswith('/los/'):
                 return jsonify({"error": "File path is outside the allowed directory"}), 403
            with open(path, 'w') as f:
                f.write(content)
            return jsonify({"result": f"Successfully wrote to {path}"})
        except Exception as e:
            return jsonify({"error": f"Failed to write to file: {e}"}), 500

    elif tool_name == "file_read":
        path = expand_los_path(data.get("path"))
        if not path:
            return jsonify({"error": "Missing 'path' for file_read"}), 400
        # Basic security check to prevent reading from outside of /los/
        if not os.path.abspath(path).startswith('/los/'):
            return jsonify({"error": "File path is outside the allowed directory"}), 403
        # Agenda filter enforcement.  Agenda filters are meant to restrict
        # access to entity-specific directories; global user data under
        # /los/{USERNAME}/data/ (memory files, project notes, etc.) is not
        # tied to a single agenda and should remain readable via file_read.
        allowed_agendas = get_allowed_agendas(request)
        user_data_prefix = f"/los/{USERNAME}/data/"
        if not path.startswith(user_data_prefix) and not is_path_allowed(path, allowed_agendas):
            print(f"[ACTION_MCP] file_read BLOCKED by agenda filter: {path}")
            return jsonify({"error": f"Access to '{path}' is not permitted for the current label/filter."}), 403
        try:
            if not os.path.exists(path):
                return jsonify({"result": f"File does not exist: {path}"})
            if not os.path.isfile(path):
                return jsonify({"result": f"Path is not a file: {path}"})
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            if not content.strip():
                return jsonify({"result": f"File exists but is empty: {path}"})
            return jsonify({"result": content})
        except Exception as e:
            print(f"Error reading file {path}: {e}")
            return jsonify({"result": f"Error reading file {path}: {e}"})

    elif tool_name == "execute_command":
        command = data.get("command")
        if not command:
            return jsonify({"error": "Missing 'command' for execute_command"}), 400
        # For now, we just log the command for security.
        print(f"MCP Action Server would have executed: {command}")
        return jsonify({"result": f"Dry run: Command '{command}' was logged."})

    elif tool_name == "update_delegatable_scores":
        updates = data.get("updates")
        if not updates or not isinstance(updates, list):
            return jsonify({"error": "Missing or invalid 'updates' for update_delegatable_scores"}), 400
        try:
            modified_ids = []
            base_path = f"/los/{USERNAME}"
            task_files = {}
            
            # Collect all tasks by file
            for root, dirs, files in os.walk(os.path.join(base_path, "data")):
                for file in files:
                    if file.startswith('todo') or file.startswith('calendar'):
                        filepath = os.path.join(root, file)
                        try:
                            with open(filepath, 'r') as f:
                                lines = f.readlines()
                            tasks = []
                            for line in lines:
                                line = line.strip()
                                if line:
                                    try:
                                        task = json.loads(line)
                                        tasks.append(task)
                                    except json.JSONDecodeError:
                                        pass
                            task_files[filepath.replace(base_path, "")] = (filepath, tasks)
                        except Exception as e:
                            pass
            
            # Update scores
            for update in updates:
                task_id = update.get('id')
                score = update.get('score')
                if not task_id or score is None:
                    continue
                found = False
                for path_rel, (path_abs, tasks) in task_files.items():
                    for task in tasks:
                        if task.get('id') == task_id:
                            task['delegatable_score'] = score
                            modified_ids.append(task_id)
                            found = True
                            break
                    if found:
                        break
            
            # Write back updated files
            for path_rel, (path_abs, tasks) in task_files.items():
                if any(t.get('id') in modified_ids for t in tasks):
                    with open(path_abs, 'w') as f:
                        for task in tasks:
                            f.write(json.dumps(task) + '\n')
            
            return jsonify({"result": f"Updated delegatable scores for: {modified_ids}"})
        except Exception as e:
            return jsonify({"error": f"Failed to update delegatable scores: {e}"}), 500

    elif tool_name == "whatsapp_send":
        recipient_number = (data.get("to") or data.get("phone") or data.get("recipient") or
                            data.get("to_number") or data.get("number") or data.get("phone_number"))
        message = data.get("message")
        if not recipient_number or not message:
            return jsonify({"error": "Missing recipient (to/phone/recipient/...) or 'message' for whatsapp_send"}), 400
        try:
            import requests
            # Call the whatsapp_mcp server.  The port comes from
            # /los/sys/config.json via the gateway; 5101 is the long-standing
            # default for a standalone run.
            _wa_port = os.environ.get("LOS_WHATSAPP_PORT", "5101")
            whatsapp_response = requests.post(
                f"http://127.0.0.1:{_wa_port}/tools/whatsapp_send", json=data)
            if whatsapp_response.status_code == 200:
                return jsonify({"result": f"WhatsApp message sent to {recipient_number}: {message}"})
            else:
                return jsonify({"error": f"Failed to send WhatsApp message: {whatsapp_response.text}"}), 500
        except requests.exceptions.RequestException as e:
            return jsonify({"error": f"Error calling WhatsApp server: {e}"}), 500

    elif tool_name == "todo_add":
        entity_rel_path = data.get("path", "")
        text = data.get("text")
        if not text or not text.strip():
            return jsonify({"error": "'text' content is required"}), 400

        # Construct data and set defaults
        priority = data.get("priority", 1.0)
        if not isinstance(priority, (int, float)) or priority < 0.0 or priority > 9.9:
            priority = 1.0

        status = data.get("status", "pending")
        allowed_statuses = ["pending", "in_progress", "completed", "cancelled"]
        if status not in allowed_statuses:
            status = "pending"

        assigned_to = data.get("assigned_to")
        deadline = data.get("deadline")
        delegatable_score = data.get("delegatable_score")
        # No validation for delegatable_score, keep as provided (matches ssl_server)

        # Generate new fields
        now = datetime.now(timezone.utc)
        new_id = f"todo_{int(now.timestamp())}_{os.urandom(4).hex()}"
        created_at = now.isoformat()

        # Construct the new Todo object
        new_todo_obj = {
            "id": new_id,
            "text": text.strip(),
            "priority": priority,
            "status": status,
            "created_at": created_at,
            "completed_at": None,
            "assigned_to": assigned_to,
            "deadline": deadline,
            "delegatable_score": delegatable_score
        }

        # Get target file path
        try:
            LOS_BASE_PATH = "/los"
            if '..' in entity_rel_path or entity_rel_path.startswith('/'):
                return jsonify({"error": "Invalid entity path format"}), 400
            entity_abs_path = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME, entity_rel_path))
            # Security check
            user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME))
            if not entity_abs_path.startswith(user_root + os.sep) and entity_abs_path != user_root:
                return jsonify({"error": "Invalid entity path"}), 400
            target_file = os.path.abspath(os.path.join(entity_abs_path, 'data', 'todo', 'todo'))
            if not target_file.startswith(user_root + os.sep):
                return jsonify({"error": "Invalid target file path"}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to determine file path: {e}"}), 400

        # Append the new todo (as a JSON string line)
        line_to_add = json.dumps(new_todo_obj)

        # Special handling to convert old array format to lines
        if os.path.exists(target_file):
            try:
                with open(target_file, 'r') as f:
                    content = f.read().strip()
                if content.startswith('['):
                    # Old array format, convert to lines
                    current = json.loads(content)
                    if isinstance(current, list):
                        with open(target_file, 'w', encoding='utf-8') as f:
                            for item in current:
                                f.write(json.dumps(item) + '\n')
                            f.write(line_to_add + '\n')
                        return jsonify({"result": f"Added todo item '{text}'"})
            except Exception:
                pass

        if append_safe(target_file, line_to_add):
            return jsonify({"result": f"Added todo item '{text}'"})
        else:
            return jsonify({"error": "Failed to save todo"}), 500

    elif tool_name == "calendar_add":
        print("DEBUG: === CALENDAR_ADD START ===")
        entity_rel_path = data.get("path", "")
        print(f"DEBUG: entity_rel_path: '{entity_rel_path}'")

        # Extract title from various possible field names
        title_candidates = {
            "title": data.get("title"),
            "name": data.get("name"),
            "summary": data.get("summary"),
            "subject": data.get("subject"),
            "entry": data.get("entry"),
            "text": data.get("text"),
            "content": data.get("content")
        }
        title = (data.get("title") or data.get("name") or data.get("summary") or data.get("subject") or data.get("entry") or data.get("text") or data.get("content"))
        print(f"DEBUG: Title candidates: {title_candidates}")
        print(f"DEBUG: Selected title: '{title}'")

        start = data.get("start")
        print(f"DEBUG: start (raw): '{start}'")

        # Handle different time formats flexibly
        if not start:
            date = data.get("date")
            # Look for time in various possible field names
            time = (data.get("time") or data.get("start_time"))
            end_time = data.get("end_time")

            print(f"DEBUG: No start provided, checking flexible format - date: '{date}', time: '{time}', end_time: '{end_time}'")

            if date:
                if time:
                    # Format: 14:00 -> 14:00:00, treat as local time (no timezone)
                    if len(time.split(':')) == 2:
                        time = time + ":00"
                    start = f"{date}T{time}"  # Local time, no timezone suffix
                else:
                    # All-day event (time not specified)
                    start = f"{date}T09:00:00"  # Default to 9 AM local if no time specified

                if end_time:
                    if len(end_time.split(':')) == 2:
                        end_time = end_time + ":00"
                    end = f"{date}T{end_time}"
                    print(f"DEBUG: Generated start: '{start}', end: '{end}'")
                else:
                    end = None

                print(f"DEBUG: Used flexible format - generated start: '{start}', end: '{end}'")
            else:
                print("DEBUG: Flexible format fallback failed - missing date")
                return jsonify({"error": "'title' is required, and either 'start', or 'date' with optional 'time'/'start_time'"}), 400

        if not title or not title.strip():
            print("DEBUG: No valid title found")
            return jsonify({"error": "'title' is required"}), 400

        print(f"DEBUG: Final title: '{title}', start: '{start}'")

        # Validate and parse start time
        print(f"DEBUG: Parsing start time: '{start}'")
        try:
            start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
            print(f"DEBUG: Parsed start_dt: {start_dt}, tzinfo: {start_dt.tzinfo}")
        except ValueError as e:
            print(f"DEBUG: Failed to parse start time: {e}")
            return jsonify({"error": "Invalid 'start' format, use ISO format"}), 400

        end = data.get("end")
        print(f"DEBUG: end: '{end}'")
        if end:
            try:
                datetime.fromisoformat(end.replace('Z', '+00:00'))
            except ValueError as e:
                print(f"DEBUG: Failed to parse end time: {e}")
                return jsonify({"error": "Invalid 'end' format, use ISO format"}), 400

        description = data.get("description") or data.get("content") or ""
        backgroundColor = data.get("backgroundColor", "#3788d8")
        status = data.get("status", "tentative")
        allowed_statuses = ["tentative", "confirmed", "cancelled"]
        if status not in allowed_statuses:
            status = "tentative"

        print(f"DEBUG: Before timezone handling - start_dt: {start_dt}, tzinfo: {start_dt.tzinfo}")
        # Handle timezone properly - ensure we have local time for storage
        if start_dt.tzinfo is None or start_dt.tzinfo.utcoffset(start_dt) is None:
            # No timezone specified - treat as local time
            local_tz = datetime.now(timezone.utc).astimezone().tzinfo
            start_dt = start_dt.replace(tzinfo=local_tz)
            print(f"DEBUG: Set local timezone for naively parsed time - local_tz: {local_tz}, start_dt: {start_dt}")
        else:
            # Timezone specified - convert to local time for storage
            start_dt = start_dt.astimezone()
            print(f"DEBUG: Converted to local timezone - start_dt: {start_dt}")

        # Prepare start and end times for local timezone storage
        # FullCalendar interprets timezone-naive ISO strings as local time
        start_local = start_dt.strftime('%Y-%m-%dT%H:%M:%S')
        print(f"DEBUG: start_local (for storage): '{start_local}'")

        end_local = None
        if end:
            try:
                end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                if end_dt.tzinfo is None or end_dt.tzinfo.utcoffset(end_dt) is None:
                    end_dt = end_dt.replace(tzinfo=start_dt.tzinfo)  # Use same timezone as start
                else:
                    end_dt = end_dt.astimezone()  # Convert to local
                end_local = end_dt.strftime('%Y-%m-%dT%H:%M:%S')
                print(f"DEBUG: end_local (for storage): '{end_local}'")
            except ValueError:
                print(f"DEBUG: Could not parse end time '{end}', skipping")
                end_local = None

        # Generate event ID in milliseconds to match JS Date.now().toString()
        import time
        new_id = str(int(time.time() * 1000))
        print(f"DEBUG: Generated new_id: {new_id}")

        # Construct event object with local times for FullCalendar
        event_obj = {
            "title": title.strip(),
            "start": start_local,
            "end": end_local,
            "backgroundColor": backgroundColor,
            "borderColor": backgroundColor,
            "extendedProps": {
                "description": description,
                "content": data.get("content", ""),
                "status": status
            },
            "id": new_id
        }
        print(f"DEBUG: Constructed event_obj: {event_obj}")

        # Get target file path
        try:
            LOS_BASE_PATH = "/los"
            print(f"DEBUG: LOS_BASE_PATH: {LOS_BASE_PATH}, USERNAME: {USERNAME}, entity_rel_path: '{entity_rel_path}'")
            if '..' in entity_rel_path or entity_rel_path.startswith('/'):
                return jsonify({"error": "Invalid entity path format"}), 400
            entity_abs_path = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME, entity_rel_path))
            print(f"DEBUG: entity_abs_path: {entity_abs_path}")

            # Security check
            user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME))
            if not entity_abs_path.startswith(user_root + os.sep) and entity_abs_path != user_root:
                return jsonify({"error": "Invalid entity path"}), 400

            # Determine file path based on local time of start
            start_dt_local = start_dt.astimezone()
            print(f"DEBUG: start_dt_local: {start_dt_local}")
            year_str = start_dt_local.strftime('%Y')
            month_day_str = start_dt_local.strftime('%m%d')
            calendar_base = os.path.join(entity_abs_path, 'data', 'calendar')
            target_file = os.path.abspath(os.path.join(calendar_base, year_str, month_day_str))
            print(f"DEBUG: Calendar file - year: {year_str}, month_day: {month_day_str}")
            print(f"DEBUG: calendar_base: {calendar_base}")
            print(f"DEBUG: target_file: {target_file}")
            if not target_file.startswith(user_root + os.sep):
                return jsonify({"error": "Invalid target file path"}), 400
        except Exception as e:
            print(f"DEBUG: Failed to determine file path: {e}")
            return jsonify({"error": f"Failed to determine file path: {e}"}), 400

        # Append the event (as JSON line)
        line_to_add = json.dumps(event_obj)
        print(f"DEBUG: line_to_add: {line_to_add}")

        # Special handling to convert old array format to lines
        if os.path.exists(target_file):
            print(f"DEBUG: target_file exists, checking for old array format")
            try:
                with open(target_file, 'r') as f:
                    content = f.read().strip()
                if content.startswith('['):
                    print("DEBUG: Found old array format, converting to lines")
                    # Old array format, convert to lines
                    current = json.loads(content)
                    if isinstance(current, list):
                        with open(target_file, 'w', encoding='utf-8') as f:
                            for item in current:
                                f.write(json.dumps(item) + '\n')
                            f.write(line_to_add + '\n')
                        print(f"DEBUG: Successfully converted array format and saved event")
                        return jsonify({"result": f"Added calendar event '{title}'"})
            except Exception as e:
                print(f"DEBUG: Error handling array format: {e}")

        print(f"DEBUG: Saving event to file, calling append_safe")
        if append_safe(target_file, line_to_add):
            print(f"DEBUG: === CALENDAR_ADD SUCCESS: Added event '{title}' to {target_file} ===")
            return jsonify({"result": f"Added calendar event '{title}'"})
        else:
            print("DEBUG: append_safe failed")
            return jsonify({"error": "Failed to save calendar event"}), 500

    elif tool_name == "todo_update":
        todo_id = data.get("id")
        fields = data.get("fields")
        entity_rel_path = data.get("path", "")

        if not todo_id:
            return jsonify({"error": "Missing 'id' for todo_update"}), 400
        if not fields or not isinstance(fields, dict):
            return jsonify({"error": "Missing or invalid 'fields' for todo_update"}), 400

        try:
            LOS_BASE_PATH = "/los"
            if '..' in entity_rel_path or entity_rel_path.startswith('/'):
                return jsonify({"error": "Invalid entity path format"}), 400
            user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME))
            search_root = os.path.abspath(os.path.join(user_root, entity_rel_path)) if entity_rel_path else user_root
            if not search_root.startswith(user_root):
                return jsonify({"error": "Invalid entity path"}), 400

            # Walk all data/todo/todo files under the search root
            found = False
            found_file = None
            found_lines = None
            found_index = None
            found_todo = None

            for dirpath, dirnames, filenames in os.walk(search_root):
                # Look for data/todo/todo pattern
                if os.path.basename(dirpath) == 'todo' and os.path.basename(os.path.dirname(dirpath)) == 'data':
                    todo_file = os.path.join(dirpath, 'todo')
                    if not os.path.isfile(todo_file):
                        continue
                    try:
                        with open(todo_file, 'r', encoding='utf-8') as f:
                            lines = f.readlines()
                        for i, line in enumerate(lines):
                            line_stripped = line.strip()
                            if not line_stripped:
                                continue
                            try:
                                todo = json.loads(line_stripped)
                                if isinstance(todo, dict) and str(todo.get('id')) == str(todo_id):
                                    found = True
                                    found_file = todo_file
                                    found_lines = lines
                                    found_index = i
                                    found_todo = todo
                                    break
                            except json.JSONDecodeError:
                                continue
                    except Exception as e:
                        print(f"[ACTION_MCP] Error reading {todo_file}: {e}")
                        continue
                if found:
                    break

            if not found:
                return jsonify({"error": f"Todo item with ID '{todo_id}' not found"}), 404

            # Apply field updates
            now = datetime.now(timezone.utc)
            for key, value in fields.items():
                found_todo[key] = value
            found_todo['lastModified'] = now.isoformat()

            # Check if status is being set to "done" or "completed"
            new_status = fields.get('status', '').lower()
            move_to_done = new_status in ('done', 'completed')

            if move_to_done:
                found_todo['status'] = 'done'
                found_todo['completed_at'] = now.isoformat()

                # Build lines without the found item
                lines_to_keep = []
                for i, line in enumerate(found_lines):
                    if i != found_index:
                        lines_to_keep.append(line)

                # Write the todo file without the completed item
                with open(found_file, 'w', encoding='utf-8') as f:
                    f.writelines(lines_to_keep)

                # Append the completed item to the done file in the same directory
                done_file = os.path.join(os.path.dirname(found_file), 'done')
                if not append_safe(done_file, json.dumps(found_todo)):
                    return jsonify({"error": "Failed to write to done file"}), 500

                print(f"[ACTION_MCP] Todo '{todo_id}' marked as done and moved to {done_file}")
                return jsonify({"result": f"Todo '{found_todo.get('text', todo_id)}' marked as done and moved to done file"})

            else:
                # Update in place - rewrite the file with the modified item
                found_lines[found_index] = json.dumps(found_todo) + '\n'
                with open(found_file, 'w', encoding='utf-8') as f:
                    f.writelines(found_lines)

                updated_fields_str = ', '.join(f"{k}={v}" for k, v in fields.items())
                print(f"[ACTION_MCP] Todo '{todo_id}' updated: {updated_fields_str}")
                return jsonify({"result": f"Todo '{found_todo.get('text', todo_id)}' updated ({updated_fields_str})"})

        except Exception as e:
            print(f"[ACTION_MCP] Error in todo_update: {e}")
            return jsonify({"error": f"Failed to update todo: {e}"}), 500

    elif tool_name == "calendar_update":
        event_id = data.get("id")
        fields = data.get("fields")
        entity_rel_path = data.get("path", "")

        if not event_id:
            return jsonify({"error": "Missing 'id' for calendar_update"}), 400
        if not fields or not isinstance(fields, dict):
            return jsonify({"error": "Missing or invalid 'fields' for calendar_update"}), 400

        # Fields that live at the top level of the calendar event object
        TOP_LEVEL_FIELDS = {'title', 'start', 'end', 'backgroundColor', 'borderColor', 'id', 'allDay'}
        # Fields that live inside extendedProps
        EXTENDED_PROPS_FIELDS = {'status', 'description', 'content', 'delegatable_score'}

        try:
            LOS_BASE_PATH = "/los"
            if '..' in entity_rel_path or entity_rel_path.startswith('/'):
                return jsonify({"error": "Invalid entity path format"}), 400
            user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME))
            search_root = os.path.abspath(os.path.join(user_root, entity_rel_path)) if entity_rel_path else user_root
            if not search_root.startswith(user_root):
                return jsonify({"error": "Invalid entity path"}), 400

            # Walk all data/calendar/YYYY/MMDD files under the search root
            found = False
            found_file = None
            found_lines = None
            found_index = None
            found_event = None

            for dirpath, dirnames, filenames in os.walk(search_root):
                # Look for data/calendar pattern
                if os.path.basename(os.path.dirname(dirpath)) == 'calendar' and os.path.basename(os.path.dirname(os.path.dirname(dirpath))) == 'data':
                    # This is a YYYY directory inside data/calendar/
                    for day_file_name in filenames:
                        day_file = os.path.join(dirpath, day_file_name)
                        if not os.path.isfile(day_file):
                            continue
                        try:
                            with open(day_file, 'r', encoding='utf-8') as f:
                                lines = f.readlines()
                            for i, line in enumerate(lines):
                                line_stripped = line.strip()
                                if not line_stripped:
                                    continue
                                try:
                                    event = json.loads(line_stripped)
                                    if isinstance(event, dict) and str(event.get('id')) == str(event_id):
                                        found = True
                                        found_file = day_file
                                        found_lines = lines
                                        found_index = i
                                        found_event = event
                                        break
                                except json.JSONDecodeError:
                                    continue
                        except Exception as e:
                            print(f"[ACTION_MCP] Error reading {day_file}: {e}")
                            continue
                        if found:
                            break
                if found:
                    break

            if not found:
                return jsonify({"error": f"Calendar event with ID '{event_id}' not found"}), 404

            # Apply field updates with intelligent merging
            now = datetime.now(timezone.utc)
            if 'extendedProps' not in found_event or not isinstance(found_event.get('extendedProps'), dict):
                found_event['extendedProps'] = {}

            for key, value in fields.items():
                if key in TOP_LEVEL_FIELDS:
                    found_event[key] = value
                elif key in EXTENDED_PROPS_FIELDS:
                    found_event['extendedProps'][key] = value
                else:
                    # Unknown field — put it in extendedProps as a safe default
                    found_event['extendedProps'][key] = value

            found_event['extendedProps']['lastModified'] = now.isoformat()

            # Update borderColor to match backgroundColor if backgroundColor was changed
            if 'backgroundColor' in fields:
                found_event['borderColor'] = fields['backgroundColor']

            # Rewrite the file with the updated event
            found_lines[found_index] = json.dumps(found_event) + '\n'
            with open(found_file, 'w', encoding='utf-8') as f:
                f.writelines(found_lines)

            updated_fields_str = ', '.join(f"{k}={v}" for k, v in fields.items())
            print(f"[ACTION_MCP] Calendar event '{event_id}' updated: {updated_fields_str}")
            return jsonify({"result": f"Calendar event '{found_event.get('title', event_id)}' updated ({updated_fields_str})"})

        except Exception as e:
            print(f"[ACTION_MCP] Error in calendar_update: {e}")
            return jsonify({"error": f"Failed to update calendar event: {e}"}), 500

    elif tool_name == "calendar_pull":
        start_date_str = data.get("start_date")
        end_date_str = data.get("end_date")
        if not start_date_str or not end_date_str:
            return jsonify({"error": "Missing 'start_date' or 'end_date' for calendar_pull"}), 400

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError as e:
            return jsonify({"error": f"Invalid date format (use YYYY-MM-DD): {e}"}), 400

        if end_date < start_date:
            return jsonify({"error": "end_date must be >= start_date"}), 400

        allowed_agendas = get_allowed_agendas(request)
        user_root = os.path.abspath(f"/los/{USERNAME}")

        # Collect all allowed entity dirs
        all_entity_dirs = _find_entity_dirs(user_root)
        entity_dirs = [d for d in all_entity_dirs if is_path_allowed(d, allowed_agendas)]

        calendar_events = []
        cron_events = []

        # Build date range
        delta_days = (end_date - start_date).days + 1
        date_range = [start_date + timedelta(days=i) for i in range(delta_days)]

        for entity_dir in entity_dirs:
            # Relative path label for output (e.g. "" for root, "work" for work)
            rel = os.path.relpath(entity_dir, user_root)
            agenda_label = "" if rel == "." else rel

            # ── Calendar events ──────────────────────────────────────────
            cal_base = os.path.join(entity_dir, "data", "calendar")
            for check_date in date_range:
                year_str = check_date.strftime("%Y")
                mmdd_str = check_date.strftime("%m%d")
                day_file = os.path.join(cal_base, year_str, mmdd_str)
                if not os.path.isfile(day_file):
                    continue
                try:
                    with open(day_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            try:
                                event = json.loads(line)
                                if isinstance(event, dict):
                                    event["_agenda"] = agenda_label
                                    event["_date"] = check_date.isoformat()
                                    calendar_events.append(event)
                            except json.JSONDecodeError:
                                pass
                except OSError:
                    pass

            # ── Cron entries ─────────────────────────────────────────────
            cron_dir = os.path.join(entity_dir, "data", "cron")
            if os.path.isdir(cron_dir):
                cron_entries = []
                try:
                    for fname in os.listdir(cron_dir):
                        if fname in ("log",) or fname.startswith("."):
                            continue
                        fpath = os.path.join(cron_dir, fname)
                        if not os.path.isfile(fpath):
                            continue
                        with open(fpath, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line or line.startswith("#"):
                                    continue
                                try:
                                    entry = json.loads(line)
                                    if isinstance(entry, dict) and "id" in entry:
                                        cron_entries.append(entry)
                                except json.JSONDecodeError:
                                    pass
                except OSError:
                    pass

                for check_date in date_range:
                    for entry in cron_entries:
                        if _cron_entry_fires_on_date(entry, check_date):
                            cron_events.append({
                                "id": entry.get("id"),
                                "type": entry.get("type"),
                                "title": entry.get("title", ""),
                                "description": entry.get("description", ""),
                                "action": entry.get("action", "display"),
                                "date": check_date.isoformat(),
                                "_agenda": agenda_label
                            })

        print(f"[ACTION_MCP] calendar_pull: {len(calendar_events)} calendar events, "
              f"{len(cron_events)} cron events for {start_date_str}..{end_date_str} "
              f"across {len(entity_dirs)} agenda(s)")
        return jsonify({"result": {
            "start_date": start_date_str,
            "end_date": end_date_str,
            "calendar_events": calendar_events,
            "cron_events": cron_events
        }})

    elif tool_name == "todo_pull":
        entity_rel_path = data.get("path", "") if data else ""
        allowed_agendas = get_allowed_agendas(request)
        user_root = os.path.abspath(f"/los/{USERNAME}")

        # Validate and build search root
        if entity_rel_path:
            if ".." in entity_rel_path or entity_rel_path.startswith("/"):
                return jsonify({"error": "Invalid entity path format"}), 400
            search_root = os.path.abspath(os.path.join(user_root, entity_rel_path))
            if not search_root.startswith(user_root):
                return jsonify({"error": "Invalid entity path"}), 400
        else:
            search_root = user_root

        # Collect allowed entity dirs within search_root
        all_entity_dirs = _find_entity_dirs(search_root)
        entity_dirs = [d for d in all_entity_dirs if is_path_allowed(d, allowed_agendas)]

        todos = []
        ACTIVE_STATUSES = {"pending", "in_progress", None}

        for entity_dir in entity_dirs:
            rel = os.path.relpath(entity_dir, user_root)
            agenda_label = "" if rel == "." else rel

            todo_file = os.path.join(entity_dir, "data", "todo", "todo")
            if not os.path.isfile(todo_file):
                continue
            try:
                with open(todo_file, "r", encoding="utf-8") as f:
                    content_raw = f.read().strip()

                # Handle legacy array format
                if content_raw.startswith("["):
                    try:
                        items = json.loads(content_raw)
                        lines_parsed = [json.dumps(item) for item in items]
                    except json.JSONDecodeError:
                        lines_parsed = []
                else:
                    lines_parsed = content_raw.splitlines()

                for line in lines_parsed:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        todo = json.loads(line)
                        if not isinstance(todo, dict):
                            continue
                        status = todo.get("status")
                        if status in ACTIVE_STATUSES or status not in ("done", "completed", "cancelled"):
                            todo["_agenda"] = agenda_label
                            todos.append(todo)
                    except json.JSONDecodeError:
                        pass
            except OSError as e:
                print(f"[ACTION_MCP] Error reading {todo_file}: {e}")

        # Sort by priority descending
        todos.sort(key=lambda t: -(t.get("priority") or 0))

        print(f"[ACTION_MCP] todo_pull: {len(todos)} active todos across {len(entity_dirs)} agenda(s)")
        return jsonify({"result": {"todos": todos, "count": len(todos)}})

    elif tool_name == "history_read":
        # Resolve history file path
        history_file = data.get("history_file")
        if not history_file:
            entity_rel_path = data.get("entity_path", "")
            project_id = data.get("project_id", "entity")
            if '..' in entity_rel_path or entity_rel_path.startswith('/'):
                return jsonify({"error": "Invalid entity_path"}), 400
            user_root = os.path.abspath(f"/los/{USERNAME}")
            entity_abs = os.path.abspath(os.path.join(user_root, entity_rel_path)) if entity_rel_path else user_root
            if not entity_abs.startswith(user_root + os.sep) and entity_abs != user_root:
                return jsonify({"error": "Invalid entity_path"}), 400
            history_file = os.path.join(entity_abs, 'data', 'project', str(project_id), 'history.json')

        history_file = os.path.abspath(history_file)
        if not history_file.startswith('/los/'):
            return jsonify({"error": "history_file must be inside /los/"}), 403

        # Agenda filter enforcement (skip for global user data paths)
        allowed_agendas = get_allowed_agendas(request)
        user_data_prefix = f"/los/{USERNAME}/data/"
        if not history_file.startswith(user_data_prefix) and not is_path_allowed(history_file, allowed_agendas):
            print(f"[ACTION_MCP] history_read BLOCKED by agenda filter: {history_file}")
            return jsonify({"error": f"Access to '{history_file}' is not permitted for the current label/filter."}), 403

        if not os.path.isfile(history_file):
            return jsonify({"result": f"No history file found at {history_file}"})

        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return jsonify({"result": f"Could not read history file: {e}"})

        if not isinstance(raw, list):
            return jsonify({"result": "History file is not a list."})

        hours = data.get("hours", 4)
        try:
            hours = float(hours)
        except (ValueError, TypeError):
            hours = 4
        limit = data.get("limit", 100)
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            limit = 100

        def _parse_ts(ts):
            if not ts:
                return None
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        entries = []
        for e in raw:
            if not isinstance(e, dict):
                continue
            if e.get('role') not in ('user', 'assistant'):
                continue
            content = (e.get('content') or '').strip()
            if not content:
                continue
            ts = _parse_ts(e.get('timestamp'))
            if ts is None or ts < cutoff:
                continue
            entries.append(e)

        entries = entries[-limit:]

        if not entries:
            return jsonify({"result": f"No conversation history in the last {hours} hours."})

        lines = [f"Project conversation history (last {hours} hours):"]
        for e in entries:
            role = e.get('role', '?')
            label = e.get('label', '')
            ts = _parse_ts(e.get('timestamp'))
            ts_short = ts.strftime("%Y-%m-%d %H:%M") if ts else ''
            role_label = 'User' if role == 'user' else f'Assistant ({label})' if label else 'Assistant'
            first_line = e.get('content', '').splitlines()[0]
            suffix = " ..." if len(e.get('content', '').splitlines()) > 1 else ""
            lines.append(f"- {ts_short} {role_label}: {first_line}{suffix}")

        return jsonify({"result": "\n".join(lines)})

    elif tool_name == "cron_add":
        entry_type = data.get("type")
        title = data.get("title", "").strip()
        entity_rel_path = data.get("path", "")

        if not entry_type or entry_type not in ("date", "cron", "cycle"):
            return jsonify({"error": "'type' must be 'date', 'cron', or 'cycle'"}), 400
        if not title:
            return jsonify({"error": "'title' is required"}), 400

        # Type-specific validation
        if entry_type == "date":
            month = data.get("month")
            day = data.get("day")
            if month is None or day is None:
                return jsonify({"error": "date type requires 'month' and 'day'"}), 400
            try:
                month = int(month)
                day = int(day)
                if not (1 <= month <= 12) or not (1 <= day <= 31):
                    raise ValueError
            except (ValueError, TypeError):
                return jsonify({"error": "'month' (1-12) and 'day' (1-31) must be valid integers"}), 400

        elif entry_type == "cron":
            expression = data.get("expression", "").strip()
            if not expression:
                return jsonify({"error": "cron type requires 'expression' (5-field cron string)"}), 400
            if len(expression.split()) != 5:
                return jsonify({"error": "'expression' must be a 5-field cron string (minute hour dom month dow)"}), 400

        elif entry_type == "cycle":
            interval = data.get("interval")
            anchor = data.get("anchor")
            if not interval or not isinstance(interval, dict):
                return jsonify({"error": "cycle type requires 'interval' object with days/hours/minutes"}), 400
            if not anchor:
                return jsonify({"error": "cycle type requires 'anchor' ISO datetime string"}), 400
            total_seconds = (
                interval.get("days", 0) * 86400 +
                interval.get("hours", 0) * 3600 +
                interval.get("minutes", 0) * 60
            )
            if total_seconds <= 0:
                return jsonify({"error": "'interval' must have at least one positive value (days, hours, or minutes)"}), 400
            try:
                datetime.fromisoformat(anchor)
            except (ValueError, TypeError):
                return jsonify({"error": "'anchor' must be a valid ISO datetime string"}), 400

        # Resolve entity path
        try:
            LOS_BASE_PATH = "/los"
            if ".." in entity_rel_path or entity_rel_path.startswith("/"):
                return jsonify({"error": "Invalid entity path format"}), 400
            user_root = os.path.abspath(os.path.join(LOS_BASE_PATH, USERNAME))
            entity_abs_path = os.path.abspath(os.path.join(user_root, entity_rel_path)) if entity_rel_path else user_root
            if not entity_abs_path.startswith(user_root):
                return jsonify({"error": "Invalid entity path"}), 400
            target_file = os.path.abspath(os.path.join(entity_abs_path, "data", "cron", "cron"))
            if not target_file.startswith(user_root + os.sep):
                return jsonify({"error": "Invalid target file path"}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to resolve file path: {e}"}), 400

        # Build the cron entry object
        now = datetime.now(timezone.utc)
        new_id = f"cron_{int(now.timestamp())}_{os.urandom(3).hex()}"
        entry_obj = {
            "id": new_id,
            "type": entry_type,
            "title": title,
            "description": data.get("description", ""),
            "enabled": data.get("enabled", True),
            "action": data.get("action", "display"),
            "command": data.get("command", None),
            "last_run": None,
            "created_at": now.isoformat()
        }

        if entry_type == "date":
            entry_obj["month"] = month
            entry_obj["day"] = day
            year = data.get("year")
            entry_obj["year"] = int(year) if year is not None else None
            # rule stays None for plain fixed-date entries
            entry_obj["rule"] = None

        elif entry_type == "cron":
            entry_obj["expression"] = expression
            entry_obj["schedule"] = None

        elif entry_type == "cycle":
            entry_obj["interval"] = {
                "days": interval.get("days", 0),
                "hours": interval.get("hours", 0),
                "minutes": interval.get("minutes", 0)
            }
            entry_obj["anchor"] = anchor

        line_to_add = json.dumps(entry_obj)
        if append_safe(target_file, line_to_add):
            print(f"[ACTION_MCP] cron_add: added '{title}' (type={entry_type}) to {target_file}")
            year_str = f" for {entry_obj.get('year')}" if entry_type == "date" and entry_obj.get("year") else ""
            return jsonify({"result": f"Added {entry_type} cron entry '{title}'{year_str} (id={new_id})"})
        else:
            return jsonify({"error": "Failed to save cron entry"}), 500

    else:
        return jsonify({"error": "Tool not implemented"}), 501

if __name__ == '__main__':
    # Note: In a real deployment, you'd use a production-ready WSGI server
    # like Gunicorn or uWSGI.
    #
    # Host/port come from /los/sys/config.json via the gateway (los gateway).
    # The defaults preserve standalone behaviour: running this file directly
    # still listens on 127.0.0.1:5100 exactly as before.  use_reloader is off
    # so the gateway supervises exactly one process per service.
    _host = os.environ.get('LOS_ACTION_HOST', '127.0.0.1')
    _port = int(os.environ.get('LOS_ACTION_PORT', '5100'))
    _debug = os.environ.get('LOS_DEBUG', '0') == '1'
    print(f"Action MCP Server starting on {_host}:{_port}...")
    app.run(host=_host, port=_port, debug=_debug, use_reloader=False)
