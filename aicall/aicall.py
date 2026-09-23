#!/usr/bin/env python3

import argparse
import sys
import json
import os
import re
import ast
import subprocess
import shutil
import hashlib
import requests
from datetime import date, datetime, timedelta, timezone

# The LOS user whose agenda we operate on.  LOS_USER is set by the gateway
# from /los/sys/config.json, so the configured LOS user is used even when the
# OS user differs (e.g. a service started from a different account).  Falls
# back to the system username, which is the historical behaviour.
USERNAME = (os.getenv('LOS_USER') or os.getenv('USER')
            or os.getenv('LOGNAME') or 'default')

GLOBAL_DEBUG_MODE = False
LINE_LABEL = None
LINE_TYPE = None  # the type= field of the current label from .config (e.g. 'whatsapp')
ALLOWED_AGENDAS = None  # resolved agenda filter paths for the current label (None = no filter)


def _is_whatsapp():
    """
    True when the CURRENT label is a WhatsApp channel — determined by the
    type= field of the label's section in .config, NOT by the label's name.
    Label names are user-configurable and may be renamed at any time; the
    type= field is the stable behavioral marker (type=whatsapp).
    """
    return bool(LINE_TYPE) and LINE_TYPE.startswith('whatsapp')

# --- Logging setup ---
# The log lives under the shared log/ directory (LOS_LOG, set by the
# gateway) rather than inside aicall/ itself, so it never ends up in the
# git working tree.  A standalone run without the gateway falls back to
# /los/sys/log, matching every other service's log location.
LOG_FILE = os.path.join(os.environ.get("LOS_LOG", "/los/sys/log"), "aicall.log")
try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
except OSError:
    pass

# Rotation policy: shared with every other LOS log via config.json's
# logging.* settings (rotate/max_bytes/keep).  Falls back to sane defaults
# when the los package or config.json is unavailable (e.g. this file was
# copied out and run somewhere else), so aicall never fails to log because
# of a rotation problem.
_DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_DEFAULT_LOG_KEEP = 3


def _log_rotation_settings():
    try:
        sys_dir = os.environ.get("LOS_SYS", "/los/sys")
        if sys_dir not in sys.path:
            sys.path.insert(0, sys_dir)
        from los import config as _los_config
        from los import logrotate as _los_logrotate
        cfg = _los_config.load(strict=False)
        return _los_logrotate.settings(cfg)
    except Exception:
        return True, _DEFAULT_LOG_MAX_BYTES, _DEFAULT_LOG_KEEP


def rotate_log_if_needed():
    """
    If the current log file exceeds the configured size, rename it to
    aicall.YYYYMMDD.log (using today's date).  If that name is already taken,
    use aicall.YYYYMMDD_2.log instead, and so on.  The active LOG_FILE is
    then left empty (or created fresh on the next write).  Older dated
    backups beyond the configured 'keep' count are pruned automatically.
    Disabled entirely when logging.rotate is false in config.json.
    """
    try:
        sys_dir = os.environ.get("LOS_SYS", "/los/sys")
        if sys_dir not in sys.path:
            sys.path.insert(0, sys_dir)
        from los import logrotate as _los_logrotate
        rotate, max_bytes, keep = _log_rotation_settings()
        _los_logrotate.dated(LOG_FILE, max_bytes=max_bytes, keep=keep,
                             rotate=rotate)
    except Exception:
        pass  # Silently ignore rotation failures — logging must never break aicall

# ANSI color helpers. Colors are intentionally preserved in aicall.log so
# that tail/cat in a color-capable terminal shows them.
ANSI_COLORS = {
    'reset': '\033[0m',
    'bold': '\033[1m',
    'dim': '\033[2m',
    'red': '\033[31m',
    'green': '\033[32m',
    'yellow': '\033[33m',
    'blue': '\033[34m',
    'magenta': '\033[35m',
    'cyan': '\033[36m',
    'white': '\033[37m',
    'orange': '\033[38;5;208m',
    'bright_red': '\033[91m',
    'bright_green': '\033[92m',
    'bright_yellow': '\033[93m',
    'bright_cyan': '\033[96m',
    'bright_white': '\033[97m',
}

ANSI_RE = re.compile(r'\033\[[0-9;]*m')

def color(text, *codes):
    """Wrap text in ANSI color/style codes."""
    if not codes:
        return text
    return ''.join(ANSI_COLORS.get(c, '') for c in codes) + text + ANSI_COLORS['reset']

def strip_ansi(text):
    """Remove ANSI escape sequences from text."""
    return ANSI_RE.sub('', text)

def log_message(category, message):
    """Append a timestamped log entry to the aicall log file.
    Errors and warnings are colored so they stand out in the log."""
    try:
        rotate_log_if_needed()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{category}] {message}"
        cat_lower = category.lower()
        if 'error' in cat_lower:
            line = color(line, 'bright_red', 'bold')
        elif 'warning' in cat_lower:
            line = color(line, 'orange', 'bold')
        with open(LOG_FILE, 'a') as lf:
            lf.write(line + '\n')
    except Exception:
        pass  # Silently ignore logging failures to not disrupt operation


def _log_messages_pretty(category_prefix, model, url, messages):
    """
    Write a prompt dump to the log with a single labeled header, then the
    full prompt body without any per-line prefix, then a labeled footer.
    Input prompts are intentionally NOT colored.
    """
    try:
        rotate_log_if_needed()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, 'a') as lf:
            lf.write(f"[{timestamp}] [{category_prefix}_BEGIN] model={model} url={url} msg_count={len(messages)}\n")
            for i, msg in enumerate(messages):
                role = msg.get('role', '?')
                content = str(msg.get('content', ''))
                # Summarize huge content on a single preview line.
                preview = strip_ansi(content)[:120].replace('\n', ' ')
                lf.write(f"[{timestamp}] [{category_prefix}_MSG {i} role={role}] preview={preview!r}\n")
                # Body lines are written raw (no timestamp/category prefix).
                lf.write(content)
                if not content.endswith('\n'):
                    lf.write('\n')
            lf.write(f"[{timestamp}] [{category_prefix}_END] ---\n")
    except Exception:
        pass


def _log_text_pretty(category_prefix, model, url, text, step=None):
    """
    Write a response text block to the log with a single labeled header,
    then the full bright-cyan text without per-line prefix, then a footer.
    """
    try:
        rotate_log_if_needed()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        step_tag = f" step={step}" if step is not None else ""
        colored_text = color(str(text), 'bright_cyan')
        with open(LOG_FILE, 'a') as lf:
            lf.write(f"[{timestamp}] [{category_prefix}_BEGIN] model={model} url={url}{step_tag} len={len(strip_ansi(colored_text))}\n")
            preview = strip_ansi(colored_text)[:120].replace('\n', ' ')
            lf.write(f"[{timestamp}] [{category_prefix}_PREVIEW] {preview!r}\n")
            # Body lines are written raw in bright cyan (no timestamp/category prefix).
            lf.write(colored_text)
            if not colored_text.endswith('\n'):
                lf.write('\n')
            lf.write(f"[{timestamp}] [{category_prefix}_END] ---\n")
    except Exception:
        pass


class TeeStderr:
    """Wraps stderr so that everything written to it is also logged to the log file."""
    def __init__(self, original_stderr):
        self.original_stderr = original_stderr

    def write(self, text):
        self.original_stderr.write(text)
        if text and text.strip():
            log_message("STDERR", text.rstrip('\n'))

    def flush(self):
        self.original_stderr.flush()

    def fileno(self):
        return self.original_stderr.fileno()

    def isatty(self):
        return self.original_stderr.isatty()

def parse_config_for_debug_flag(target_label):
    """
    Lightweight parse to check if debug=1 is set for the target_label.
    Sets GLOBAL_DEBUG_MODE if found.
    Debug traces are only emitted when GLOBAL_DEBUG_MODE is already enabled
    (e.g. via an earlier label or explicit caller) to avoid log noise.
    """
    global GLOBAL_DEBUG_MODE
    config_path = f"/los/{USERNAME}/.config"
    _debug = GLOBAL_DEBUG_MODE and not (_is_whatsapp())

    if _debug:
        print(f"--- DEBUG (parse_config_for_debug_flag) START ---", file=sys.stderr)
        print(f"DEBUG (parse_config_for_debug_flag): Checking for '{target_label}' debug flag in {config_path}", file=sys.stderr)

    try:
        with open(config_path, 'r') as f:
            lines = f.readlines()

            in_target_section_temp = False
            target_section_indent_temp = -1

            for line_num, line in enumerate(lines):
                stripped_line = line.strip()
                if _debug:
                    print(f"DEBUG (parse_config_for_debug_flag): Line {line_num+1}: '{stripped_line}'", file=sys.stderr)
                if not stripped_line or stripped_line.startswith('#'):
                    if _debug:
                        print("DEBUG (parse_config_for_debug_flag): Skipping empty or comment line.", file=sys.stderr)
                    continue

                current_indentation = len(line) - len(stripped_line)
                if _debug:
                    print(f"DEBUG (parse_config_for_debug_flag): Indentation: {current_indentation}", file=sys.stderr)

                if stripped_line.endswith(':'):
                    current_label_name = stripped_line[:-1]
                    if _debug:
                        print(f"DEBUG (parse_config_for_debug_flag): Found label: '{current_label_name}'", file=sys.stderr)
                    if current_label_name == target_label:
                        if _debug:
                            print(f"DEBUG (parse_config_for_debug_flag): Matching label '{target_label}' found!", file=sys.stderr)
                        in_target_section_temp = True
                        target_section_indent_temp = current_indentation
                    elif in_target_section_temp and current_indentation <= target_section_indent_temp:
                        if _debug:
                            print(f"DEBUG (parse_config_for_debug_flag): Exiting early for '{target_label}' - new label or shallower indentation.", file=sys.stderr)
                        break  # Left the target section, stop checking
                elif in_target_section_temp and current_indentation >= target_section_indent_temp:
                    if _debug:
                        print(f"DEBUG (parse_config_for_debug_flag): In section, processing line.", file=sys.stderr)
                    if '=' in stripped_line:
                        key, value = stripped_line.split('=', 1)
                        # Strip spaces, then remove outer quotes if present
                        stripped_value = value.strip()
                        if stripped_value.startswith('"') and stripped_value.endswith('"'):
                            stripped_value = stripped_value[1:-1]
                        # Remove comments
                        clean_value = stripped_value.split('#')[0].strip()
                        if key.strip() == 'debug' and clean_value == '1':
                            GLOBAL_DEBUG_MODE = True
                            if _debug or not (_is_whatsapp()):
                                print(f"DEBUG (parse_config_for_debug_flag): Found 'debug=1' for label '{target_label}'. Setting GLOBAL_DEBUG_MODE to True.", file=sys.stderr)
                                print(f"--- DEBUG (parse_config_for_debug_flag) END ---", file=sys.stderr)
                            return True
            if _debug:
                print(f"DEBUG (parse_config_for_debug_flag): 'debug=1' not found for '{target_label}'. GLOBAL_DEBUG_MODE remains False.", file=sys.stderr)
                print(f"--- DEBUG (parse_config_for_debug_flag) END ---", file=sys.stderr)
            return False  # debug=1 not found
    except FileNotFoundError:
        print(color(f"Error: Config file not found at {config_path}", 'bright_red', 'bold'), file=sys.stderr)
        return False
        

def parse_config(target_label):
    """
    A robust custom parser for the .config file format. This version
    accurately handles indentation-based sections and extracts key-value pairs.
    It stops parsing a section when it encounters an empty line (or multiple) followed by a
    new label, or EOF.
    This version includes unconditional debug prints for troubleshooting.
    """
    global GLOBAL_DEBUG_MODE
    config_path = f"/los/{USERNAME}/.config"


    
    config = {}
    actively_parsing_target_section = False
    target_section_indent = -1
    
    empty_line_count = 0

    try:
        with open(config_path, 'r') as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            stripped_line = line.strip()
            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print(f"DEBUG (parse_config): Line {i+1}: '{stripped_line}'", file=sys.stderr)

            # Handle empty lines for section termination
            if not stripped_line:
                empty_line_count += 1
                if actively_parsing_target_section:
                    if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                        print(f"DEBUG (parse_config): Line {i+1}: Empty line detected (count={empty_line_count}).", file=sys.stderr)
                continue
            else:
                empty_line_count = 0 # Reset empty line counter if content is found

            # Skip comments
            if stripped_line.startswith('#'):
                if actively_parsing_target_section:
                    if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                        print(f"DEBUG (parse_config): Line {i+1}: Comment, skipping.", file=sys.stderr)
                continue

            current_indentation = len(line) - len(stripped_line)
            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print(f"DEBUG (parse_config): Line {i+1}: Current indentation = {current_indentation}", file=sys.stderr)

            # Check for label definitions like "all:" or "web:public:"
            if stripped_line.endswith(':'):
                current_label_name = stripped_line[:-1]
                if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                    print(f"DEBUG (parse_config): Line {i+1}: Found label definition: '{current_label_name}'", file=sys.stderr)

                if current_label_name == target_label:
                    if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                        print(f"DEBUG (parse_config): Line {i+1}: Matching target label '{target_label}' found! Indentation={current_indentation}.", file=sys.stderr)
                    actively_parsing_target_section = True
                    target_section_indent = current_indentation
                    config['label'] = target_label # Store the label name itself

                elif actively_parsing_target_section and current_indentation <= target_section_indent:
                    # We were inside the target section, but encountered another label
                    # at the same or shallower indentation, so the target section has ended.
                    if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                        print(f"DEBUG (parse_config): Line {i+1}: Exiting section '{target_label}' due to new label '{current_label_name}' at same/shallower indentation.", file=sys.stderr)
                        print(f"DEBUG (parse_config): Returning config: {config}", file=sys.stderr)
                        print(f"--- DEBUG (parse_config) END ---", file=sys.stderr)
                    return config
                
            # Handle content within a section (key-value pairs, default, filters)
            elif actively_parsing_target_section:
                # If an empty line count is followed by content that's not a new label,
                # and this content is at an equal or shallower indentation, it means the section ended.
                if empty_line_count > 0 and current_indentation < target_section_indent: # Changed from <= to <
                    return config

                # Process lines belonging to the current section
                if current_indentation >= target_section_indent : # Ensures it's part of the current section's block
                    if '=' in stripped_line:
                        key, value = stripped_line.split('=', 1)
                        # Strip spaces
                        stripped_value = value.strip()
                        # Remove outer quotes if present
                        if stripped_value.startswith('"'):
                            first_quote = 0
                            second_quote = stripped_value.find('"', 1)
                            if second_quote > 0:
                                stripped_value = stripped_value[1:second_quote]
                            else:
                                stripped_value = stripped_value[1:]
                        # Remove comments after #
                        clean_value = stripped_value.split('#')[0].strip().replace('"', '').strip()
                        config[key.strip()] = clean_value
                    elif stripped_line == 'default':
                        return config
                    elif stripped_line.startswith('+') or stripped_line.startswith('-'):
                        # Agenda filter rule — collect for later resolution
                        sign = stripped_line[0]
                        agenda = stripped_line[1:].strip()
                        if agenda:
                            if 'filters' not in config:
                                config['filters'] = []
                            config['filters'].append((sign, agenda))
                else:
                    # Line is at a shallower indentation, but it's not a label.
                    # This implies the section has implicitly ended.
                    return config
            
            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print(f"DEBUG (parse_config): Line {i+1}: Current config state for '{target_label}': {config}", file=sys.stderr)
    except FileNotFoundError:
        print(color(f"Error: Config file not found at {config_path}", 'bright_red', 'bold'), file=sys.stderr)
        return None

    # If EOF is reached while still in the target section
    if actively_parsing_target_section:
        return config

    return None

def parse_aiconfig(path):
    """
    Parses the JSON aiconfig file.
    """
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(color(f"Error reading or parsing aiconfig file {path}: {e}", 'bright_red', 'bold'), file=sys.stderr)
        return None


def _discover_entities_recursive(abs_path, rel_path, result):
    """Walk 'entities' files to collect all sub-entity relative paths."""
    entities_file = os.path.join(abs_path, 'entities')
    if os.path.isfile(entities_file):
        try:
            with open(entities_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    entity_name = line.split()[0]
                    child_rel = f"{rel_path}/{entity_name}".lstrip('/')
                    child_abs = os.path.join(abs_path, entity_name)
                    if os.path.isdir(child_abs):
                        result.append(child_rel)
                        _discover_entities_recursive(child_abs, child_rel, result)
        except OSError:
            pass


def discover_entities(username):
    """
    Return all entity relative paths under /los/{username}/.
    The root is represented as '' (empty string).
    """
    base = f"/los/{username}"
    entities = ['']  # root
    _discover_entities_recursive(base, '', entities)
    return entities


def _rel_path_matches(rel_path, pattern):
    """
    Return True if rel_path matches the filter pattern.
    Prefix-match: 'level3' matches 'level3', 'level3/love', 'level3/sub/deep'.
    '*' matches everything.
    """
    if pattern == '*':
        return True
    return rel_path == pattern or rel_path.startswith(pattern + '/')


def resolve_allowed_agendas(filters, username):
    """
    Apply +/- filter rules (in order) to the discovered entity list and return
    a list of absolute agenda directory paths that are allowed.

    Rules:
      + *           → add all discovered entities
      + work        → add 'work' and all its sub-entities
      - level3      → remove 'level3' and all its sub-entities
      + level3/love → add 'level3/love' and its sub-entities

    Returns None if filters is empty/None (meaning no restriction).
    """
    if not filters:
        return None

    all_entities = discover_entities(username)
    allowed = set()

    for sign, pattern in filters:
        matching = [e for e in all_entities if _rel_path_matches(e, pattern)]
        # If the pattern explicitly names an entity not yet discovered, add it anyway
        if not matching and pattern != '*':
            matching = [pattern]
        if sign == '+':
            allowed.update(matching)
        elif sign == '-':
            allowed -= set(matching)

    base = f"/los/{username}"
    # Convert relative paths to absolute, normalise trailing slash
    result = []
    for rel in sorted(allowed):
        abs_path = os.path.join(base, rel) if rel else base
        result.append(abs_path.rstrip('/') + '/')
    return result if result else None


def store_conversation_memory(config, messages, label, user_prompt):
    """Store the conversation history in memory for future reference."""
    if not config or not config.get('memory'):
        return False

    # Store conversation logic - different for WhatsApp vs other labels
    user_msgs = [msg for msg in messages if msg["role"] == "user"]
    assistant_msgs = [msg for msg in messages if msg["role"] == "assistant"]

    # If there's no assistant reply at all (e.g. every LLM call failed) there
    # is nothing useful to store for non-whatsapp labels.  Skip instead of
    # writing a broken record and raising KeyError: 'type' later.
    if not assistant_msgs and not (label and _is_whatsapp()):
        return False

    conversation_summary = {
        "timestamp": messages[1]["content"] if len(messages) > 1 else user_prompt,  # First user message
        "context": label,
        "message_count": len(messages) // 2,  # Approximate message pairs
        "topic_tags": [],  # Could be extracted from LLM in future
        "last_activity": str(date.today()),
        # Default type — overridden below for the cases we recognise.  Without
        # this, paths that don't match any branch (e.g. whatsapp with no user
        # messages, or a single assistant "Tools:" response) would KeyError.
        "type": "interaction",
    }

    # Extract phone number from WhatsApp prompts for better context tracking
    phone_tags = []
    if _is_whatsapp():
        phone_match = re.search(r'from\s+(\d+)', user_prompt)
        if phone_match:
            phone_tags = [phone_match.group(1)]


    if _is_whatsapp() and not user_prompt.startswith("Incoming WhatsApp"):
        # Synthetic dispatch (cron/daily jobs driven by prompt files, not a
        # real user chat). Storing the giant synthetic prompt as a
        # "conversation" pollutes the RECENT CONVERSATION HISTORY block of
        # every future whatsapp_incoming dispatch — store it untagged instead.
        conversation_summary["type"] = "whatsapp_task"
        conversation_summary["user_message"] = user_prompt[:500]
        conversation_summary["summary"] = f"Automated WhatsApp task run for label {label}"
        if assistant_msgs:
            conversation_summary["ai_response"] = assistant_msgs[0]["content"][:500]

    elif _is_whatsapp():
        # For WhatsApp, store conversation context with phone number tagging
        if len(assistant_msgs) >= 1:
            # There's an AI response - store as conversation exchange
            conversation_summary["type"] = "whatsapp_message"
            if len(user_msgs) > 0:
                # Clean up user message (remove whatsapp prefix)
                user_content = user_msgs[0]["content"]
                if user_content.startswith("Incoming WhatsApp message from"):
                    # Extract just the user's message part
                    user_content = user_content.split(": ", 2)[-1] if ": " in user_content else user_content
                conversation_summary["user_message"] = user_content
            conversation_summary["ai_response"] = assistant_msgs[0]["content"]

            # Store topic information for memory (who/what was discussed)
            conversation_summary["topic"] = "general_chat"
            conversation_summary["summary"] = f"WhatsApp chat about {conversation_summary['topic']}: {conversation_summary['user_message'][:40]}..."
        else:
            # Just store incoming message for reference
            conversation_summary["type"] = "whatsapp_incoming"
            conversation_summary["user_message"] = user_msgs[0]["content"]
            conversation_summary["summary"] = f"Incoming WhatsApp: {conversation_summary['user_message'][:80]}..."

    elif len(user_msgs) >= 1 and len(assistant_msgs) >= 1:  # At least one exchange for other labels
        conversation_summary["type"] = "conversation_history"
        conversation_summary["summary"] = f"Chat with {len(user_msgs)} user messages about recent topics"

        # Build conversation transcript for better context
        transcript = []
        for i, msg in enumerate(messages):
            if msg["role"] in ["user", "assistant"]:
                role = "User" if msg["role"] == "user" else "Assistant"
                clean_content = msg["content"].replace("\n\n-- Current tasks", "").split("\n\n-- Your instruction:")[0]
                transcript.append(f"{role}: {clean_content[:100]}{'...' if len(clean_content) > 100 else ''}")

        conversation_summary["conversation"] = transcript[-4:]  # Store last few exchanges

    elif len(assistant_msgs) == 1 and "Tools:" not in str(assistant_msgs[0].get("content", "")):
        # Single tool result - store as task result
        conversation_summary["type"] = "task_result"
        conversation_summary["result"] = assistant_msgs[0].get("content", "")
        conversation_summary["task_type"] = "tool_execution"

    try:
        # Include phone number tags for WhatsApp conversations. Synthetic task
        # runs (cron/daily) deliberately do NOT get the "conversation" tag so
        # they stay out of conversational recall.
        if conversation_summary.get("type") == "whatsapp_task":
            tags = ["task", label]
        else:
            tags = ["conversation", label] + phone_tags + conversation_summary.get("topic_tags", [])
        success, result = call_tool("memory_store", {
            "content": json.dumps(conversation_summary),
            "category": conversation_summary["type"],
            "tags": tags
        }, config)
        return success
    except Exception as e:
        print(color(f"Warning: Could not store conversation memory: {e}", 'orange', 'bold'), file=sys.stderr)
        return False

def scan_data_directories():
    """
    Scans all data/ directories for task files and returns the contents.
    """
    tasks = []
    base_path = f"/los/{USERNAME}"
    for root, dirs, files in os.walk(os.path.join(base_path, "data")):
        for file in files:
            if file.startswith('todo') or file.startswith('calendar'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    task = json.loads(line)
                                    tasks.append({
                                        "path": filepath.replace(base_path, ""),
                                        "data": task
                                    })
                                except json.JSONDecodeError:
                                    pass
                except Exception as e:
                    pass
    return tasks

def get_memory_context(config, label=None, limit=3, phone=None):
    """Get recent relevant memory entries for context."""
    if not config or not config.get('memory'):
        return ""

    try:
        # For WhatsApp, get recent conversation entries scoped to the phone
        # number so one contact's history doesn't leak into another's. For
        # other labels use the general conversation tag.
        tags = ["conversation"]
        if label and _is_whatsapp():
            if phone:
                tags.append(phone)
            _, result = call_tool("memory_list", {
                "tags": tags,
                "limit": limit
            }, config)
        else:
            _, result = call_tool("memory_list", {
                "tags": tags,
                "limit": limit
            }, config)

        if isinstance(result, list):
            entries = result

            # Client-side scoping: only entries stored under THIS label (and
            # this phone, when known). The memory server's tag matching is
            # loose (ANY tag), which used to let the daily cron's entries leak
            # into whatsapp_incoming's RECENT CONVERSATION HISTORY.
            def _entry_in_scope(e):
                etags = e.get("tags") or []
                if isinstance(etags, str):
                    etags = [etags]
                if label and label not in etags:
                    return False
                if phone and phone not in etags:
                    return False
                return True

            entries = [e for e in entries if _entry_in_scope(e)]
            conversation_history = []

            for entry in entries[-limit:]:  # Most recent entries
                content = json.loads(entry.get("content", "{}"))
                category = entry.get("category", "")

                if category in ["whatsapp_message", "conversation_history"]:
                    # Extract the full conversation exchange
                    user_msg = content.get("user_message", "")
                    ai_response = content.get("ai_response", "")
                    if user_msg and ai_response:
                        # Build conversation history - clean the user message
                        clean_msg = user_msg
                        if clean_msg.startswith("Incoming WhatsApp message from"):
                            # Extract just the actual message text
                            parts = clean_msg.split(": ", 2)
                            if len(parts) >= 3:
                                clean_msg = parts[2]

                        conversation_history.append({
                            "user": clean_msg,
                            "assistant": ai_response[:200]  # Truncate AI response
                        })

            # Format conversation context for system prompt
            if conversation_history:
                context_text = "\n**RECENT CONVERSATION HISTORY:**\n"
                for exchange in conversation_history[-limit:]:  # Configurable limit
                    context_text += f"- User: {exchange['user'][:150]}{'...' if len(exchange['user']) > 150 else ''}\n"
                    context_text += f"- Assistant: {exchange['assistant'][:150]}{'...' if len(exchange['assistant']) > 150 else ''}\n"
                return context_text

    except Exception as e:
        print(color(f"Warning: Could not load memory context: {e}", 'orange', 'bold'), file=sys.stderr)
    return ""

def load_mcp_tools():
    """Dynamically load tool descriptions from MCP server configurations."""
    mcp_config_path = "/los/sys/aicall_mcp/mcp_config.json"

    try:
        with open(mcp_config_path, 'r') as f:
            mcp_config = json.load(f)
    except Exception as e:
        print(color(f"Warning: Could not load MCP config {mcp_config_path}: {e}", 'orange', 'bold'), file=sys.stderr)
        return {}

    dynamic_tools = {}
    for server in mcp_config.get("servers", []):
        try:
            response = requests.get(server["url"] + "/")
            response.raise_for_status()
            config = response.json()
            if "tools" in config:
                for tool_name, tool_def in config["tools"].items():
                    description = tool_def.get("description", "")
                    input_schema = tool_def.get("input_schema", {})
                    # Convert input_schema to the expected format
                    parameters = {}
                    if "properties" in input_schema:
                        parameters = input_schema["properties"]
                    dynamic_tools[tool_name] = {
                        "description": description,
                        "parameters": parameters
                    }
        except Exception as e:
            print(color(f"Warning: Could not fetch tools from {server['name']} at {server['url']}: {e}", 'orange', 'bold'), file=sys.stderr)

    return dynamic_tools


def get_tools_description():
    """
    Return the MCP tool descriptions used to build prompts.

    Loaded dynamically from the running MCP servers (load_mcp_tools); if those
    are unreachable, fall back to a hardcoded list. Shared by BOTH the LLM path
    (generate_system_prompt) and the external agent path
    (_build_agent_tool_context) so agents and LLMs always advertise the exact
    same set of tools.
    """
    tools_description = load_mcp_tools()
    if tools_description:
        return tools_description

    print(color("Warning: Using hardcoded tool descriptions as fallback", 'yellow', 'bold'), file=sys.stderr)
    return {
        "file_write": {
            "description": "Write content to a file. This will overwrite the file if it exists.",
            "parameters": {"path": "string", "content": "string"}
        },
        "file_read": {
            "description": "Read the content of a file.",
            "parameters": {"path": "string"}
        },
        "whatsapp_send": {
            "description": "Send a WhatsApp message to a specified number.",
            "parameters": {"to": "string", "message": "string"}
        },
        "whatsapp_send_image": {
            "description": "Send an image (or video) via WhatsApp to a recipient. Provide exactly ONE of: image_path (local file), image_url (HTTP URL — the server downloads it), or image_base64 (base64-encoded image data). Use this tool to share screenshots, photos, or any visual content.",
            "parameters": {"to": "string", "image_path": "string", "image_url": "string", "image_base64": "string", "caption": "string"}
        },
        "todo_add": {
            "description": "Add a new todo item to the agenda.",
            "parameters": {"path": "string", "text": "string", "priority": "number", "status": "string"}
        },
        "calendar_add": {
            "description": "Add a new calendar event.",
            "parameters": {"title": "string", "start": "string", "end": "string", "description": "string"}
        },
        "todo_update": {
            "description": "Update fields on an existing todo item by ID. If status is set to 'done', the item is moved to the done file automatically.",
            "parameters": {"id": "string", "fields": "object", "path": "string"}
        },
        "calendar_update": {
            "description": "Update fields on an existing calendar event by ID. Top-level fields (title, start, end, backgroundColor) updated directly; others (status, description, content) updated in extendedProps.",
            "parameters": {"id": "string", "fields": "object", "path": "string"}
        },
        "memory_store": {
            "description": "Store information in memory with optional tags and categories.",
            "parameters": {"content": "string", "tags": ["string"], "category": "string", "id": "string"}
        },
        "memory_retrieve": {
            "description": "Retrieve memory entries by ID.",
            "parameters": {"id": "string"}
        },
        "memory_list": {
            "description": "List memory entries with optional filtering.",
            "parameters": {"tags": ["string"], "category": "string", "limit": "integer", "offset": "integer"}
        },
        "memory_search": {
            "description": "Search memory entries by content text (full-text search).",
            "parameters": {"query": "string", "tags": ["string"], "category": "string", "limit": "integer"}
        },
        "memory_update": {
            "description": "Update an existing memory entry.",
            "parameters": {"id": "string", "content": "string", "tags": ["string"], "category": "string"}
        },
        "memory_delete": {
            "description": "Delete a memory entry by ID.",
            "parameters": {"id": "string"}
        },
        "calendar_pull": {
            "description": "Pull all calendar events and cron-scheduled entries for a date range across all accessible agendas. Returns calendar_events and cron_events.",
            "parameters": {"start_date": "string (YYYY-MM-DD)", "end_date": "string (YYYY-MM-DD)"}
        },
        "todo_pull": {
            "description": "Pull all active (pending/in_progress) todo items across all accessible agendas. Returns a list of todos sorted by priority.",


            "parameters": {"path": "string (optional, relative entity path)"}
        },
        "cron_add": {
            "description": (
                "Add a recurring/scheduled entry to the cron system. Three types:\n"
                "  'date'  – Annual or one-time date events (birthdays, holidays).\n"
                "            Required extra fields: month (1-12), day (1-31). Optional: year (omit for annual).\n"
                "  'cron'  – 5-field cron expression.\n"
                "            Required extra field: expression (e.g. '0 9 * * 1' = every Monday 9 AM).\n"
                "  'cycle' – Interval-based repeating event.\n"
                "            Required extra fields: interval ({days, hours, minutes}), anchor (ISO datetime).\n"
                "Common fields: type, title, description, action ('display'|'execute'|'notify'), enabled, path.\n"
                "Examples:\n"
                '  {"tool":"cron_add","parameters":{"type":"date","title":"Mom birthday","month":4,"day":12}}\n'
                '  {"tool":"cron_add","parameters":{"type":"cycle","title":"Water plants","interval":{"days":3},"anchor":"2026-02-18T08:00:00"}}'
            ),
            "parameters": {
                "type": "string ('date'|'cron'|'cycle')",
                "title": "string",
                "description": "string (optional)",
                "action": "string ('display'|'execute'|'notify', default 'display')",
                "enabled": "boolean (default true)",
                "path": "string (optional, relative entity path)",
                "month": "integer 1-12 [date type]",
                "day": "integer 1-31 [date type]",
                "year": "integer [date type, omit for annual]",
                "expression": "string [cron type, 5-field]",
                "interval": "object {days, hours, minutes} [cycle type]",
                "anchor": "string ISO datetime [cycle type]"
            }
        },
        "history_read": {
            "description": (
                "Read the conversation history for the current entity/project. "
                "Returns recent user/assistant turns with timestamps. Use this when you need "
                "to recall what was just discussed in the current entity context."
            ),
            "parameters": {
                "history_file": "string (optional, absolute path to history.json)",
                "entity_path": "string (optional, relative entity path, e.g. 'money/gainwell')",
                "project_id": "string (optional, default 'entity')",
                "hours": "number (optional, default 4)",
                "limit": "integer (optional, default 100)"
            }
        }
    }


# Tools that create or modify agenda data (calendar / todo / cron). They are
# hidden from the prompt in 'task' and 'none' context modes so read-only task
# work doesn't accidentally rewrite the user's agenda. They remain *callable* —
# just not advertised. In 'entity' mode (and for non-UI CLI/WhatsApp/cron calls
# where no context mode is set) the full set is shown.
AGENDA_WRITE_TOOLS = {
    "calendar_add", "calendar_update",
    "todo_add", "todo_update",
    "cron_add",
}

# Tools hidden from agent prompts by default. Agents have their own memory,
# file and command-execution capabilities; advertising these would only
# confuse the agent's decision-making. They remain *callable* if absolutely
# needed — just not listed in the agent's tool context block unless the label
# opts in via `expose_los_memory=1` in .config.
AGENT_HIDDEN_TOOLS = {
    "file_write", "file_read",
    "execute_command",  # dry-run stub anyway; agents have a real shell
    "memory_store", "memory_retrieve", "memory_list",
    "memory_search", "memory_update", "memory_delete",
}

# Tools that are noisy / rarely useful and can be dropped from all prompts.
# They remain callable if explicitly invoked.
GLOBAL_HIDDEN_TOOLS = {
    "update_delegatable_scores",
}

# Label prefixes → tool names that should be hidden for that label family.
# Matching is prefix-based, e.g. "whatsapp" hides whatsapp_* for non-whatsapp labels.
LABEL_TOOL_HIDES = {
    "whatsapp": set(),  # whatsapp tools are already handled by the whatsapp preamble
}


def _resolve_prompt_path(path_value, entity_rel_path='', base_dir=None):
    """
    Resolve a prompt file path from .config. Supports:
      - absolute paths (used as-is)
      - paths relative to base_dir (default /los/{USERNAME})
      - fallback relative to the entity directory if the first resolution fails
        and entity_rel_path is provided.
    Returns the resolved path or None if path_value is empty.
    """
    if not path_value:
        return None
    base = base_dir or f"/los/{USERNAME}"
    path = os.path.expanduser(path_value)
    if os.path.isabs(path):
        return path
    candidates = [os.path.join(base, path)]
    if entity_rel_path:
        candidates.append(os.path.join(base, entity_rel_path, path))
        candidates.append(os.path.join(base, entity_rel_path, 'ai', os.path.basename(path)))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _load_optional_prompt_file(path_value, entity_rel_path='', label='prompt'):
    """
    Load a prompt file if it exists. Returns its stripped content or ''.
    Only prints a warning if the explicitly configured file cannot be found
    after trying absolute, base-relative, and entity-relative locations.
    """
    if not path_value:
        return ''
    resolved = _resolve_prompt_path(path_value, entity_rel_path)
    if resolved is None:
        return ''
    try:
        if os.path.isfile(resolved):
            with open(resolved, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception as e:
        print(color(f"Warning: Could not read {label} {resolved}: {e}", 'orange', 'bold'), file=sys.stderr)
        return ''

    # If we reach here, the resolved path does not exist.
    print(color(f"Warning: Could not find {label} file at any resolved path (tried from {path_value})", 'orange', 'bold'), file=sys.stderr)
    return ''


def _build_role_preamble(label, config=None, entity_rel_path='', for_agent=False):
    """
    Build the top-of-system-prompt role block. Priority:
      1. label-specific role_prompt from .config
      2. whatsapp special-case preamble (agent variant when for_agent=True)
      3. generic assistant preamble
    """
    if config and config.get('role_prompt'):
        role_content = _load_optional_prompt_file(
            config['role_prompt'], entity_rel_path, label='role_prompt')
        if role_content:
            return role_content

    if _is_whatsapp():
        if for_agent:
            # External agent with its OWN native tools: the rules below define
            # the contract between the agent and LOS — plain text = to the
            # user, single-JSON = LOS MCP tool call.
            return (
                "You are the LOS WhatsApp assistant talking with a real WhatsApp user; "
                "LOS relays the conversation through you.\n\n"
                "OUTPUT CONTRACT (critical):\n"
                "- Your FINAL plain-text reply is forwarded verbatim to the user's WhatsApp. "
                "Keep it short and conversational. NEVER include JSON, tool-call syntax, "
                "code fences, or internal reasoning in it.\n"
                "- DO NOT use the whatsapp_send tool to reply to the current sender — your "
                "plain-text reply already reaches them. whatsapp_send is only for messaging "
                "OTHER people.\n"
                "- Never claim an action is done unless you actually completed it: an image or "
                "message counts as 'sent' ONLY after you made the corresponding tool call and "
                "received a success result. Do not promise future actions — this conversation "
                "ends when you reply, so finish the work first (using tools) or honestly say "
                "what is missing.\n\n"
                "TOOLS:\n"
                "- Use your own native agent tools freely for internal work (file ops, "
                "image_generate, shell, browser, etc.). Use them SILENTLY — NEVER emit JSON "
                "for a native tool.\n"
                "- JSON output is reserved ONLY for LOS MCP tools (whatsapp_send, "
                "whatsapp_send_image, calendar/todo/cron tools). When you need one of those, "
                "reply with ONLY a single JSON object: "
                "{\"tool\": \"<name>\", \"parameters\": {...}} — no other text. The tool result "
                "arrives as the next message; then continue.\n"
                "- When you use a data tool (like todo_add), wait for the tool result and then "
                "provide a conversational confirmation."
            )
        return (
            "You are an intelligent assistant communicating via WhatsApp. "
            "Respond helpfully and accurately. To reply to the user you are talking to, "
            "simply respond with plain text. DO NOT use the 'whatsapp_send' tool "
            "for the current conversation; only use it to message other people. "
            "When you use a data tool (like todo_add), wait for the tool result "
            "and then provide a conversational confirmation."
        )

    return (
        "You are an intelligent assistant operating inside the LOS system. "
        "Your job is to help the user by reasoning step by step, then either "
        "calling an LOS MCP tool (when data or action is needed) or responding "
        "with plain text. Always follow the operational rules below."
    )


def _filter_tools_for_label(tools_description, label):
    """
    Remove tools that are irrelevant or noisy for the given label.
    """
    if not label:
        return tools_description
    hide = set(GLOBAL_HIDDEN_TOOLS)
    for prefix, names in LABEL_TOOL_HIDES.items():
        if not label.startswith(prefix):
            hide.update(names)
    if not _is_whatsapp():
        hide.update({'whatsapp_send', 'whatsapp_send_image'})
    return {name: spec for name, spec in tools_description.items() if name not in hide}


def _render_compact_tools_section(tools_description):
    """
    Render MCP tools as a compact, token-efficient list.
    Each tool shows: name, one-line description, required params, and an example call.
    Full JSON schemas are intentionally omitted; the model only needs to know
    how to form a correct tool call. For details it can ask or use file_read.
    """
    if not tools_description:
        return "**AVAILABLE MCP TOOLS:** none"

    def _required_keys(params):
        """
        Return a list of required parameter names.

        Supports two parameter formats:
          - JSON-schema style: {'properties': {...}, 'required': [...]}
          - Flat style:        {'path': 'string', ...} (all keys are required)
        """
        if not isinstance(params, dict):
            return []
        explicit = params.get('required')
        if isinstance(explicit, list):
            return explicit
        # Flat style: treat every top-level key as required.
        return [k for k in params.keys() if k not in ('properties', 'required')]

    lines = ["**AVAILABLE MCP TOOLS:**"]
    for name, spec in sorted(tools_description.items()):
        desc = spec.get('description', '').replace('\\n', ' ').replace('\n', ' ').strip()
        # Cap long descriptions — some (e.g. cron_add) carry multi-paragraph
        # essays that bloat every prompt for zero decision-making benefit.
        if len(desc) > 400:
            desc = desc[:397].rstrip() + '...'
        params = spec.get('parameters', {})
        required = _required_keys(params)
        req_str = ', '.join(required) if required else 'none'
        example_params = {}
        for key in required:
            example_params[key] = '<value>'
        if not example_params:
            example_params = {}
        example = json.dumps({"tool": name, "parameters": example_params})
        lines.append(f"- {name}: {desc}")
        lines.append(f"  required params: {req_str}")
        lines.append(f"  example: {example}")

    lines.extend([
        "",
        "**TOOL CALL RULES:**",
        "1. When you need data or need to act, reply with ONLY a single JSON object in this exact format:",
        '   {"tool": "<tool_name>", "parameters": {<required params>}}',
        "2. Do not wrap the JSON in markdown, code fences, or explanatory text.",
        "3. After calling a tool you will receive its result as the next message; continue from there.",
        "4. If no tool is needed, respond with normal plain text only.",
    ])
    return '\n'.join(lines)


def filter_tools_for_context(tools_description, context_mode):
    """
    Return a copy of tools_description with agenda-mutating tools removed when
    context_mode is 'task' or 'none'. The full set is returned for 'entity'
    mode and when context_mode is None/unset (CLI, WhatsApp, cron).
    """
    if context_mode in ('task', 'none'):
        return {name: spec for name, spec in tools_description.items()
                if name not in AGENDA_WRITE_TOOLS}
    return tools_description


def _load_entity_context_block(entity_rel_path):
    """
    Build a compact entity-context block (identity/omni/goals) from an entity
    directory. Returns '' if nothing useful is found.
    Used when LOS_CONTEXT_MODE == 'entity' (set by the ssl_server when the user
    selects an entity in the Context dropdown).
    """
    if entity_rel_path is None:
        return ''
    base = f"/los/{USERNAME}"
    abs_path = os.path.normpath(os.path.join(base, entity_rel_path)) if entity_rel_path else base
    # Sanity: must stay within base
    if not abs_path.startswith(base):
        return ''

    loaded = {}
    for fname, header in (('identity', 'Identity'),
                          ('omni', 'Omni (rules)'),
                          ('goals', 'Goals')):
        fpath = os.path.join(abs_path, fname)
        try:
            if os.path.isfile(fpath):
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content:
                    loaded[header] = content
        except OSError:
            pass

    if not loaded:
        return ''

    label_path = entity_rel_path if entity_rel_path else USERNAME
    parts = [f"You are operating as the entity `{label_path}`. Adopt the following identity, rules, and goals for this conversation."]
    for header, content in loaded.items():
        parts.append(f"**{header}:** {content}")
    parts.append("Do not break character unless the user explicitly asks you to switch context.")
    return "\n\n".join(parts)


def _render_tools_section(tools_description):
    """
    Render the MCP tools as a compact JSON-ish block. We dump as JSON for
    structure, then un-escape literal '\n' sequences inside string values so
    multi-line tool descriptions display as actual newlines to the LLM
    (saves tokens and improves readability).
    """
    tools_json = json.dumps(tools_description, indent=2)
    # JSON-encoded newlines back to real newlines (the result is no longer
    # strict JSON but the LLM reads it as text, not JSON).
    tools_json = tools_json.replace('\\n', '\n')

    return (
        "**AVAILABLE MCP TOOLS:**\n"
        f"{tools_json}\n\n"
        "**TOOL CALL SYNTAX:**\n"
        "When you need to use a tool, your ENTIRE response must be a single "
        "valid JSON object. Do not wrap it in code fences, markdown, or any "
        "explanatory text. Do not include any text before or after the JSON.\n\n"
        "Use one of the exact JSON formats below, with a real tool name from "
        "the list above and real parameter values:\n\n"
        '  {"tool": "file_read", "parameters": {"path": "/los/' + USERNAME + '/data/todo/todo"}}\n'
        '  {"tool": "calendar_pull", "parameters": {"start_date": "2026-02-01", "end_date": "2026-02-28"}}\n'
        '  {"tool": "todo_pull", "parameters": {}}\n\n'
        "If no tool is needed, respond with normal plain text only."
    )


def generate_system_prompt(user_prompt, label, config=None):
    """
    Generates the system prompt content for the LLM.

    Assembly order (most important first):
      1. Role preamble (label-specific if configured; otherwise generic)
      2. Entity context (only when LOS_CONTEXT_MODE=='entity', from env)
      3. MCP tools (filtered by context mode + label + hidden tools, compact format)
      4. Date / filesystem context
      5. system_prompt from .config (if specified)
      6. default_prompt from .config (if specified)
      7. Conversational history (memory context) — skip if LOS_SKIP_MEMORY_CONTEXT=1
      8. CURRENT TASKS dump — skip if LOS_SKIP_TASKS_DUMP=1

    The user prompt is NOT embedded here -- it is sent as a separate user message.

    Env vars (set by ssl_server for UI-driven calls):
      LOS_CONTEXT_MODE       – 'task' | 'entity' | 'none'
      LOS_ENTITY_REL_PATH    – entity rel path (used when mode == 'entity')
      LOS_SKIP_TASKS_DUMP    – '1' → skip section 8
      LOS_SKIP_MEMORY_CONTEXT– '1' → skip section 7
    """
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    context_mode      = os.getenv('LOS_CONTEXT_MODE', 'task')
    entity_rel_path   = os.getenv('LOS_ENTITY_REL_PATH', '')
    skip_tasks_dump   = os.getenv('LOS_SKIP_TASKS_DUMP') == '1'
    skip_memory_ctx   = os.getenv('LOS_SKIP_MEMORY_CONTEXT') == '1'

    system_parts = []

    # ── 1. Role preamble (top priority) ───────────────────────────────
    system_parts.append(_build_role_preamble(label, config, entity_rel_path))

    # ── 2. Entity context (UI-driven 'entity' mode) ──────────────────
    if context_mode == 'entity':
        entity_block = _load_entity_context_block(entity_rel_path)
        if entity_block:
            system_parts.append(entity_block)

    # ── 3. MCP tools (compact, filtered) ──────────────────────────────
    # Apply context-mode filter first, then label-specific noise filter.
    tools_description = filter_tools_for_context(get_tools_description(),
                                                 os.getenv('LOS_CONTEXT_MODE'))
    tools_description = _filter_tools_for_label(tools_description, label)
    system_parts.append(_render_compact_tools_section(tools_description))

    # ── 4. Date / filesystem context ──────────────────────────────────
    system_parts.append(
        f"Today's date is {today_str}. "
        f"Calendar files are stored at /los/{USERNAME}/data/calendar/YYYY/MMDD format."
    )

    # ── 5. system_prompt from .config ─────────────────────────────────
    sp_content = _load_optional_prompt_file(
        (config or {}).get('system_prompt', ''), entity_rel_path, label='system_prompt')
    if sp_content:
        system_parts.append(sp_content)

    # ── 6. default_prompt from .config ────────────────────────────────
    dp_content = _load_optional_prompt_file(
        (config or {}).get('default_prompt', ''), entity_rel_path, label='default_prompt')
    if dp_content:
        system_parts.append(dp_content)

    # ── 7. Conversational history (memory context) ────────────────────
    # For WhatsApp, scope memory to the sender's phone number so each contact
    # gets their own conversation history.
    memory_phone = None
    if _is_whatsapp():
        phone_match = re.search(r'from\s+(\d+)', user_prompt)
        if phone_match:
            memory_phone = phone_match.group(1)
    if not skip_memory_ctx:
        memory_context = get_memory_context(config, label, phone=memory_phone) if config else ""
        if memory_context:
            system_parts.append(memory_context)

    # ── 8. CURRENT TASKS dump (heavy — opt-out via env) ───────────────
    if not skip_tasks_dump:
        tasks = scan_data_directories()
        tasks_text = "\n".join([
            f"File: {task['path']}\n{json.dumps(task['data'], indent=2)}"
            for task in tasks if task['data'].get('delegatable_score') is None
        ])
        if tasks_text:
            system_parts.append(f"**CURRENT TASKS:**\n{tasks_text}")

    return "\n\n".join(system_parts)


def call_llm(llm_config, messages):
    """
    Calls the LLM API with the provided configuration and messages list.
    Tries each LLM in the list until one succeeds or all fail.
    """
    for i, llm in enumerate(llm_config["llms"]):
        try:
            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print(f"DEBUG: Trying LLM at {llm['api_url']} with model {llm.get('model', 'N/A')}", file=sys.stderr)

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {llm.get('api_key', '')}"
            }

            # Prepare messages
            is_api_generate = llm["api_url"].endswith("/api/generate")

            if is_api_generate:
                # API /api/generate expects a "prompt" key for the main message
                # Concatenate all messages into a single prompt
                full_prompt = "\n\n".join([msg["content"] for msg in messages])
                options = llm.get("options", {}).copy()
                options.setdefault("stream", False)  # Ensure not streaming for JSON parsing
                data = {
                    "model": llm["model"],
                    "prompt": full_prompt,
                    **options
                }
            else:
                # Use the messages directly for chat models
                all_messages = []
                if "messages" in llm:
                    all_messages.extend(llm["messages"])
                all_messages.extend(messages)
                data = {
                    "model": llm["model"],
                    "messages": all_messages,
                    **llm.get("options", {})
                }

            # Log outgoing prompt to file (structured, human-readable)
            _log_messages_pretty("PROMPT", llm.get('model', 'N/A'), llm['api_url'], messages)

            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print(f"DEBUG: Sending request to {llm['api_url']} with data: {json.dumps(data)}", file=sys.stderr)
            response = requests.post(llm["api_url"], headers=headers, json=data, timeout=llm.get("timeout"))
            response.raise_for_status()
            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print(f"DEBUG: Received response text: {response.text}", file=sys.stderr)

            # Log incoming response to file (pretty, human-readable)
            _log_text_pretty("RESPONSE", llm.get('model', 'N/A'), llm['api_url'], response.text)

            # If a fallback LLM succeeded, rotate it to index 0 so all
            # subsequent iterative steps use it first.
            if i > 0:
                llm_config["llms"].insert(0, llm_config["llms"].pop(i))

            return response.json()
        except requests.exceptions.RequestException as e:
            # Always capture errors — otherwise users have no way of knowing
            # WHY a backup LLM failed (subscription required, model not found,
            # connection refused, timeout, etc.)
            body = ''
            status = ''
            try:
                if getattr(e, 'response', None) is not None:
                    status = f" status={e.response.status_code}"
                    body = f" body={e.response.text[:500]}"
            except Exception:
                pass
            err_msg = (f"LLM call failed model={llm.get('model','N/A')} "
                       f"url={llm['api_url']}{status}: {e}{body}")
            log_message("LLM_ERROR", err_msg)
            if not (_is_whatsapp()):
                print(color(err_msg, 'bright_red'), file=sys.stderr)
            continue  # Try next LLM
        except json.JSONDecodeError as e:
            raw = ''
            try:
                raw = f" raw={response.text[:500]}"
            except Exception:
                pass
            err_msg = (f"LLM JSON decode error model={llm.get('model','N/A')}: "
                       f"{e}{raw}")
            log_message("LLM_ERROR", err_msg)
            if not (_is_whatsapp()):
                print(color(err_msg, 'bright_red'), file=sys.stderr)
            continue  # Try next LLM

    # If all LLMs failed
    print(color("Error: All LLM API calls failed.", 'bright_red', 'bold'), file=sys.stderr)
    log_message("LLM_ERROR", "All LLM API calls failed — see LLM_ERROR entries above for per-model details")
    return None

def call_tool(tool_name, params, config=None):
    """Helper function to call MCP server tools. Returns (success, result_content)"""
    # Inject memory_path for memory_* tools if available
    if tool_name.startswith('memory_') and config is not None:
        memory_path = config.get('memory')
        if memory_path:
            if not os.path.isabs(memory_path):
                memory_path = os.path.join(f'/los/{USERNAME}/', memory_path)
            params = dict(params)  # Copy to avoid modifying original
            params['memory_path'] = memory_path

    # Determine server URL based on tool prefix.  Ports come from
    # /los/sys/config.json via the gateway; the defaults match the values
    # these servers have always used, so running aicall by hand still works.
    action_url = "http://127.0.0.1:%s" % os.environ.get('LOS_ACTION_PORT', '5100')
    if tool_name.startswith('memory_'):
        server_url = "http://127.0.0.1:%s" % os.environ.get('LOS_MEMORY_PORT', '5102')
    elif tool_name.startswith("whatsapp_"):
        server_url = "http://127.0.0.1:%s" % os.environ.get('LOS_WHATSAPP_PORT', '5101')
    else:
        server_url = action_url  # Action MCP server

    is_whatsapp = _is_whatsapp()
    try:
        if not is_whatsapp:
            print(f"Sending tool call to MCP {server_url}: {tool_name} with params: {params}", file=sys.stderr)
        # Build request headers — inject agenda filter for the action server
        req_headers = {"Content-Type": "application/json"}
        if ALLOWED_AGENDAS is not None and server_url == action_url:
            req_headers["X-LOS-Allowed-Agendas"] = ",".join(ALLOWED_AGENDAS)
            if not is_whatsapp:
                print(f"[agenda-filter] X-LOS-Allowed-Agendas: {req_headers['X-LOS-Allowed-Agendas']}", file=sys.stderr)
        mcp_response = requests.post(f"{server_url}/tools/{tool_name}", json=params, headers=req_headers)
        mcp_response.raise_for_status()
        data = mcp_response.json()
        if not is_whatsapp:
            print(f"MCP Tool Call Result: {data}", file=sys.stderr)
        if 'result' in data:
            if not is_whatsapp:
                print(f"Success: {data['result']}", file=sys.stderr)
            return True, data['result']
        elif 'error' in data:
            if not is_whatsapp:
                print(color(f"Error from MCP server: {data['error']}", 'bright_red', 'bold'), file=sys.stderr)
            return True, f"Error from MCP server: {data['error']}"
        else:
            if not is_whatsapp:
                print(f"Unexpected MCP response: {data}", file=sys.stderr)
            return True, str(data)
    except Exception as e:
        error_msg = f"Error calling MCP server: {e}"
        if not is_whatsapp:
            print(color(error_msg, 'bright_red', 'bold'), file=sys.stderr)
        return False, error_msg

def is_success(result):
    """Check if the tool result indicates a successful operation that should terminate the loop."""
    if not result:
        return False

    result_str = str(result).lower()
    
    # Success patterns for various tools
    success_patterns = [
        "whatsapp message sent to",
        "message sent to",
        "success",
        "completed",
        "error: none",
    ]
    
    return any(pattern in result_str for pattern in success_patterns)

def process_llm_response(response, llm_config, config=None):
    """
    Processes the LLM's response, calling the MCP server if a tool is used.
    Returns (tool_called, tool_result) where tool_result is the result content or None.
    """
    if not response:
        print(color("Invalid response from LLM (response is empty).", 'bright_red', 'bold'), file=sys.stderr)
        return False, None

    # Get the LLM used for this response (might not be the first one due to fallback)
    first_llm = llm_config["llms"][0]  # Use first for format detection, but response is from successful LLM
    is_api_generate = first_llm["api_url"].endswith("/api/generate")

    if is_api_generate:
        if "response" in response:
            content = response["response"]
        else:
            print(color(f"Invalid /api/generate response format: Missing 'response' key. Response: {response}", 'bright_red', 'bold'), file=sys.stderr)
            return False, None
    else:
        if "choices" not in response or not response["choices"]:
            print(color("Invalid response from LLM (missing 'choices' or empty).", 'bright_red', 'bold'), file=sys.stderr)
            return False, None
        message = response["choices"][0]["message"]
        if "content" in message:
            content = message["content"]
        else:
            print(color("Invalid response from LLM (missing 'content' in message).", 'bright_red', 'bold'), file=sys.stderr)
            return False, None

    if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
        print(f"LLM Response Content: {repr(content)}", file=sys.stderr)

    tool_called = False
    tool_result = None

    # For reasoning models, strip thinking blocks entirely (tags + content between them)
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL).strip()

    # Strip thought process if present (LLM sometimes includes reasoning before final answer)
    if '\n\n\n' in content:
        # Take the part after the final separator (usually the clean answer)
        content = content.split('\n\n\n')[-1].strip()

    # Handle multiple tool calls separated by \n\n or just newlines
    tool_calls_processed = []

    # Preamble handling - print text before the first JSON block if on WhatsApp
    if _is_whatsapp():
        start = content.find('{')
        if start > 0:
            preamble = content[:start].strip()
            if preamble:
                print(preamble)
        elif start < 0:
            # No tool call, it's just text. We'll handle it at the end.
            pass

    if '\n\n' in content:
        parts = content.split('\n\n')
    else:
        parts = content.split('\n')

    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            tool_call = json.loads(part)
            if isinstance(tool_call, dict) and "tool" in tool_call and "parameters" in tool_call:
                tool_called, tool_result = call_tool(tool_call["tool"], tool_call["parameters"], config)
                tool_calls_processed.append((tool_called, tool_result))
                if tool_called and is_success(tool_result):
                    return True, tool_result
        except json.JSONDecodeError:
            pass

    # Try to parse as tool call only if no tools were processed above
    if not tool_calls_processed:
        try:
            tool_call = json.loads(content)
            if "tool" in tool_call and "parameters" in tool_call:
                tool_called, tool_result = call_tool(tool_call["tool"], tool_call["parameters"], config)
                tool_calls_processed.append((tool_called, tool_result))
        except json.JSONDecodeError:
            pass

        # Try to match descriptive tool call
        match = re.search(r"Sending tool call to MCP:\s*(\w+)\s+with params:\s*(\{.*\})", content)
        if match:
            tool = match.group(1)
            params_str = match.group(2)
            try:
                params = json.loads(params_str)
            except json.JSONDecodeError:
                try:
                    params = ast.literal_eval(params_str)
                except (ValueError, SyntaxError):
                    params = None
            
            if params:
                tool_called, tool_result = call_tool(tool, params, config)
                if tool_called:
                    return tool_called, tool_result
                    
        # Try to extract JSON from within the content
        start = content.find('{')
        if start >= 0:
            try:
                potential_json = content[start:]
                tool_call = json.loads(potential_json)
                if "tool" in tool_call and "parameters" in tool_call:
                    tool_called, tool_result = call_tool(tool_call["tool"], tool_call["parameters"])
                else:
                    if not (_is_whatsapp()):
                        print(color("Extracted content is not a tool call:", 'orange', 'bold'), file=sys.stderr)
            except json.JSONDecodeError:
                # Try to find separate JSON blocks
                try:
                    json_blocks = re.findall(r'(?<=\{|^)\{[^{}]*(\{[^}]*\}[^{}]*)*\}', content)
                except:
                    json_blocks = []
                for block in json_blocks:
                    try:
                        potential = json.loads(block)
                        if "tool" in potential and "parameters" in potential:
                            tool_called, tool_result = call_tool(potential["tool"], potential["parameters"], config)
                            if tool_called:
                                return tool_called, tool_result
                    except json.JSONDecodeError:
                        pass
        else:
            # Print plain text response to stdout for all labels
            print(content.strip())
            if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
                print("LLM response has no '{' (plain text response):", file=sys.stderr)

    return tool_called, tool_result

# ─── Inbox report helper ──────────────────────────────────────────────────────

def _report_to_inbox(url, token, content, job_id=None, role='assistant',
                     label='agent', status=None):
    """POST a message to the ssl_server's per-project report endpoint.

    Used by both the external agent path and the LLM loop to deliver results
    back to the project history regardless of whether the stdout/SSE channel
    is still open.

    Args:
        url:     Full report URL (LOS_REPORT_URL), e.g.
                 https://host/api/project/<id>/report?user=x&path=y
        token:   Project token (LOS_PROJECT_TOKEN).
        content: Message text to store.
        job_id:  Optional LOS_JOB_ID for linking to a job manifest.
        role:    Message role ('assistant' by default).
        label:   Displayed label in the canvas.
        status:  Optional job status ('running'|'done'|'error').

    Returns True on success, False otherwise.
    """
    if not url or not token or not content:
        return False
    try:
        payload = {'content': content, 'role': role, 'label': label}
        if job_id:
            payload['job_id'] = job_id
        if status:
            payload['status'] = status
        resp = requests.post(
            url,
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type':  'application/json',
            },
            timeout=15,
            verify=False,   # self-signed cert on localhost
        )
        if resp.status_code == 200:
            log_message("INBOX_REPORT", f"ok url={url} job={job_id} status={status}")
            return True
        log_message("INBOX_REPORT",
                    f"error status={resp.status_code} url={url} body={resp.text[:200]}")
        return False
    except Exception as e:
        log_message("INBOX_REPORT", f"exception url={url}: {e}")
        return False


# ─── External Agent Dispatch ─────────────────────────────────────────────────

def _build_agent_tool_context(config=None, callback_block='', user_prompt='',
                              persistent_session=False):
    """
    Build the context block prepended to an external agent's prompt so the agent
    has the SAME prompt ordering as generate_system_prompt() for regular LLMs.

    Assembly order (most important first):
      1. Role preamble (label-specific if configured; otherwise generic)
      2. Entity context (only when LOS_CONTEXT_MODE == 'entity')
      3. MCP tools (filtered + compact)
      4. Date / filesystem context
      5. system_prompt from .config (if specified)
      6. default_prompt from .config (if specified)
      7. Conversational history (LOS memory context) — skipped when
         persistent_session=True (the agent's own session already holds the
         conversation, so LOS memory recall would be redundant noise) and when
         LOS_SKIP_MEMORY_CONTEXT=1
      8. Callback/cron instructions (if callback_block provided)
    Returns a string (may be empty if no tools could be loaded).
    """
    parts = []

    # ── 1. Role preamble (top priority) ───────────────────────────────
    label = LINE_LABEL or ''
    entity_rel_path = os.getenv('LOS_ENTITY_REL_PATH', '')
    parts.append(_build_role_preamble(label, config, entity_rel_path, for_agent=True))

    # ── 2. Entity context — only in UI-driven 'entity' mode ─────────
    context_mode = os.getenv('LOS_CONTEXT_MODE', 'task')
    if context_mode == 'entity':
        entity_block = _load_entity_context_block(entity_rel_path)
        if entity_block:
            parts.append(entity_block)

    # ── 3. MCP tools + JSON call syntax ──────────────────────────────
    # Start with the full tool set and apply context-mode filtering (same as
    # the LLM path).  For agents, then strip AGENT_HIDDEN_TOOLS — file_write
    # plus the LOS memory_* functions — unless the label explicitly opts in
    # via `expose_los_memory=1` in .config.  Agents have their own superior
    # memory/file capabilities, so advertising these LOS tools confuses their
    # decision-making; they remain *callable* if absolutely needed.
    tools_description = filter_tools_for_context(get_tools_description(),
                                                 os.getenv('LOS_CONTEXT_MODE'))
    tools_description = _filter_tools_for_label(tools_description, label)
    expose_los_memory = (config or {}).get('expose_los_memory') == '1'
    if not expose_los_memory:
        tools_description = {name: spec for name, spec in tools_description.items()
                             if name not in AGENT_HIDDEN_TOOLS}
    if tools_description:
        parts.append(_render_compact_tools_section(tools_description))

    # ── 4. Date context ───────────────────────────────────────────────
    today_str = date.today().strftime("%Y-%m-%d")
    parts.append(
        f"Today's date is {today_str}. "
        f"Calendar files are stored at /los/{USERNAME}/data/calendar/YYYY/MMDD format."
    )

    # ── 5. system_prompt from .config ─────────────────────────────────
    sp_content = _load_optional_prompt_file(
        (config or {}).get('system_prompt', ''), entity_rel_path, label='system_prompt')
    if sp_content:
        parts.append(sp_content)

    # ── 6. default_prompt from .config ────────────────────────────────
    dp_content = _load_optional_prompt_file(
        (config or {}).get('default_prompt', ''), entity_rel_path, label='default_prompt')
    if dp_content:
        parts.append(dp_content)

    # ── 7. Conversational history (LOS memory context) ────────────────
    # Agents with a PERSISTENT session (e.g. per-contact WhatsApp chats) get
    # no LOS memory recall: their own session already holds the conversation,
    # and the recall adds redundant (and previously cross-label-polluted)
    # noise to every message. One-shot sessions still get the recall so the
    # agent has some continuity. LOS_SKIP_MEMORY_CONTEXT=1 skips regardless.
    skip_memory_ctx = os.getenv('LOS_SKIP_MEMORY_CONTEXT') == '1' or persistent_session
    has_memory_cfg = bool(config and config.get('memory'))
    if GLOBAL_DEBUG_MODE and not _is_whatsapp():
        print(f"[agent] memory_context check: skip={skip_memory_ctx} "
              f"has_memory_cfg={has_memory_cfg} label={label}", file=sys.stderr)
    if not skip_memory_ctx:
        memory_phone = None
        if _is_whatsapp() and user_prompt:
            phone_match = re.search(r'from\s+(\d+)', user_prompt)
            if phone_match:
                memory_phone = phone_match.group(1)
        memory_context = get_memory_context(config, label, phone=memory_phone) if config else ""
        if GLOBAL_DEBUG_MODE and not _is_whatsapp():
            print(f"[agent] memory_context result: len={len(memory_context)}", file=sys.stderr)
        if memory_context:
            parts.append(memory_context)

    # ── 7b. Project conversation history ────────────────────────────
    # The canvas stores full conversation turns in history.json. For entity
    # mode we inject the last 4 hours into the prompt so the agent/LLM sees
    # the same recent context the user is looking at. This is independent of
    # (and more complete than) the summarized memory_list recall above.
    skip_history_ctx = os.getenv('LOS_SKIP_HISTORY_CONTEXT') == '1'
    if not skip_history_ctx:
        history_file = os.getenv('LOS_HISTORY_FILE', '')
        if history_file:
            history_hours = 4 if context_mode == 'entity' else None
            try:
                raw_history = load_project_history(history_file, label, hours=history_hours)
                if raw_history:
                    # For the agent we render a compact text block rather than
                    # injecting raw chat messages (the agent is an external
                    # CLI, not an OpenAI-style chat API).
                    parts.append(_format_history_block(raw_history,
                                                         header="RECENT PROJECT HISTORY"))
                    if GLOBAL_DEBUG_MODE and not _is_whatsapp():
                        print(f"[agent] project_history injected: len={len(raw_history)} "
                              f"hours={history_hours}", file=sys.stderr)
            except Exception as e:
                if GLOBAL_DEBUG_MODE and not _is_whatsapp():
                    print(f"[agent] project_history error: {e}", file=sys.stderr)

    # ── 8. Callback/cron instructions ─────────────────────────────────
    if callback_block:
        parts.append(callback_block)

    return "\n\n".join(parts)


def _extract_agent_tool_calls(content):
    """
    Parse an external agent reply for MCP tool calls.

    Returns (preamble, tool_calls):
      preamble   – plain prose appearing before the first '{' (may be '')
      tool_calls – list of {"tool": ..., "parameters": ...} dicts (may be empty)

    Uses the same JSON-extraction heuristics as process_llm_response().
    """
    cleaned = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', cleaned, flags=re.DOTALL).strip()

    brace = cleaned.find('{')
    preamble = cleaned[:brace].strip() if brace > 0 else ''

    tool_calls = []

    def _maybe_add(obj):
        if isinstance(obj, dict) and 'tool' in obj and 'parameters' in obj:
            tool_calls.append(obj)

    # 1) Split into candidate parts and try each as JSON.
    parts = cleaned.split('\n\n') if '\n\n' in cleaned else cleaned.split('\n')
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            _maybe_add(json.loads(part))
        except json.JSONDecodeError:
            pass

    # 2) If nothing matched, try the whole reply, then an embedded JSON tail.
    if not tool_calls:
        try:
            _maybe_add(json.loads(cleaned))
        except json.JSONDecodeError:
            if brace >= 0:
                try:
                    _maybe_add(json.loads(cleaned[brace:]))
                except json.JSONDecodeError:
                    pass

    return preamble, tool_calls


def _argv_too_long(argv):
    """
    Estimate whether the constructed argv will exceed a safe fraction of the
    kernel's ARG_MAX limit.  ARG_MAX is the maximum bytes for argv+envp; we
    leave headroom for environment variables and the trailing NULs.
    """
    try:
        arg_max = os.sysconf('SC_ARG_MAX')
    except (ValueError, AttributeError):
        arg_max = 2 * 1024 * 1024  # conservative fallback
    # Reserve ~20% for environment variables, delimiter bytes, and binary path.
    safe_max = int(arg_max * 0.75)
    total = sum(len(arg.encode('utf-8', errors='ignore')) for arg in argv)
    return total > safe_max


def _prepare_agent_message(cmd, message, agent_config):
    """
    Return (cmd, stdin_input, tmp_path) adjusted so the agent can receive an
    oversized message without overflowing ARG_MAX.

    - For normal messages the agent binary receives the message via its normal
      command-line flag (the caller already included '--message' <message>).
    - If the argv would be too long, we write the message to a temporary file
      and pass it using one of the following (in order of preference):
        1. `message_file_flag` from agent_config (e.g. '--message-file'), with
           the file path as the next argument.
        2. '--message-file <path>' as a common default.
      If no file-based flag is available and `supports_stdin` is true, fall
      back to passing the message on stdin.

    Returns a tuple:
      (cmd, stdin_input, tmp_path)
    - cmd:       the possibly-modified command list
    - stdin_input: string to feed to subprocess.run(input=...), or None
    - tmp_path:  path to a temp file created for oversized messages, or None.
                   The caller is responsible for deleting it when done.
    """
    message_file_flag = agent_config.get('message_file_flag')
    message_file_arg = agent_config.get('message_file_arg', True)
    supports_stdin = agent_config.get('supports_stdin', True)

    if not _argv_too_long(cmd + [message]):
        # Fast path: message fits on the command line.  Caller already placed
        # --message <message>; leave it as-is and do not use stdin.
        return cmd, None, None

    # Oversized message: write to temp file and pass by path or stdin.
    import tempfile
    fd, tmp_path = tempfile.mkstemp(prefix='los-agent-msg-', suffix='.txt', text=True)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(message)

    print(f"[agent] Message too long for argv ({len(message)} chars); "
          f"using temp file {tmp_path}", file=sys.stderr)

    # Strip the original '--message' argument (and its value) from the command;
    # we will pass the message via file or stdin instead.
    if '--message' in cmd:
        idx = cmd.index('--message')
        # Remove '--message' and the value that follows it.
        stripped = cmd[:idx] + cmd[idx + 2:]
    else:
        stripped = list(cmd)

    if message_file_flag:
        if message_file_arg:
            return stripped + [message_file_flag, tmp_path], None, tmp_path
        # Flag only — agent expects the filename as a positional argument.
        return stripped + [message_file_flag], None, tmp_path

    if supports_stdin:
        # Pass the message on stdin; tell the agent to read stdin by using '-'
        # as the --message value.
        return stripped + ['--message', '-'], message, tmp_path

    # Last resort: use the common default --message-file flag.
    return stripped + ['--message-file', tmp_path], None, tmp_path


def _is_unsupported_thinking_error(stderr_text):
    """Detect OpenClaw's 'Thinking level not supported for this model' error."""
    if not stderr_text:
        return False
    return bool(re.search(
        r'Thinking level\s+["\']?[^"\']+["\']?\s+is not supported',
        stderr_text, re.IGNORECASE))


def _invoke_hermes(bin_path, agent_id, message, thinking, timeout,
                   session_id=None, agent_config=None):
    """
    Run a single Hermes chat turn and return (response_text, model).

    Hermes does not share the OpenClaw `agent --message ... --json` protocol.
    It is invoked non-interactively with:

        hermes chat -q <prompt> -Q --continue <session_name> --create-if-missing

    Quiet mode (-Q) prints:
        session_id: <id>
        <response text>

    We use the LOS session_id as the Hermes session name so each step of the
    LOS tool loop resumes the same Hermes conversation.
    """
    cfg = agent_config or {}

    # Hermes reasoning levels: none, minimal, low, medium, high, xhigh, max, ultra.
    # LOS thinking values map directly for low/medium/high.
    thinking_map = {
        'low': 'low',
        'medium': 'medium',
        'high': 'high',
    }
    hermes_reasoning = thinking_map.get((thinking or '').lower(), 'medium')

    # Hermes max tool-calling iterations per conversation turn.
    max_turns = int(cfg.get('max_turns', 100))

    # Use the LOS session_id as the Hermes session name.  If no session_id was
    # supplied, generate a one-shot name.
    session_name = session_id or f"los-hermes-{int(datetime.now().timestamp())}"

    cmd = [bin_path, 'chat',
           '-Q',                         # quiet / programmatic output
           '--max-turns', str(max_turns),
           '--reasoning', hermes_reasoning,
           '--no-restore-cwd',
           '--continue', session_name,
           '--create-if-missing',
           '--accept-hooks']             # auto-approve unseen shell hooks

    tmp_path = None
    try:
        # Pass the prompt via -q when it fits, otherwise via --query-file.
        if _argv_too_long(cmd + ['-q', message]):
            import tempfile
            fd, tmp_path = tempfile.mkstemp(prefix='los-hermes-msg-', suffix='.txt', text=True)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(message)
            print(f"[agent] Hermes message too long for argv ({len(message)} chars); "
                  f"using temp file {tmp_path}", file=sys.stderr)
            cmd = cmd + ['--query-file', tmp_path]
        else:
            cmd = cmd + ['-q', message]

        # Give Hermes a little startup headroom beyond the configured timeout.
        subprocess_timeout = timeout + 30 if timeout else 600

        display_cmd = list(cmd)
        if len(display_cmd) > 8 and display_cmd[-2] in ('-q', '--query-file'):
            msg_preview = str(display_cmd[-1])[:80].replace('\n', ' ')
            display_cmd[-1] = f"{msg_preview}{'...' if len(str(cmd[-1])) > 80 else ''}"
        print(f"[agent] Running: {' '.join(display_cmd)} ...", file=sys.stderr)

        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=subprocess_timeout)

        # Forward Hermes stderr (warnings, provider errors, etc.).
        if result.stderr:
            print(result.stderr.rstrip('\n'), file=sys.stderr)

        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError("Hermes returned no output on stdout.")

        # Quiet mode prints: "session_id: <id>\n<response>"
        lines = stdout.split('\n', 1)
        if len(lines) > 1 and lines[0].startswith('session_id:'):
            response_text = lines[1]
        else:
            response_text = stdout

        # Hermes quiet mode does not report the model; use aiconfig model or provider.
        model = cfg.get('model') or cfg.get('provider', 'hermes')
        return response_text, model
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as e:
                print(color(f"[agent] Warning: could not remove temp message file {tmp_path}: {e}",
                            'orange', 'bold'), file=sys.stderr)


def _invoke_agent(bin_path, agent_id, message, thinking, timeout,
                  session_id=None, agent_config=None, provider=None):
    """
    Run a single external agent turn and return (response_text, model).

    Raises RuntimeError on recoverable failures (no output / no JSON / no
    payloads). subprocess.TimeoutExpired, FileNotFoundError and
    json.JSONDecodeError propagate to the caller for unified handling.
    """
    provider = (provider or '').lower()
    if provider == 'hermes':
        return _invoke_hermes(bin_path, agent_id, message, thinking, timeout,
                              session_id, agent_config)

    cmd = [bin_path, 'agent',
           '--agent', agent_id,
           '--message', message,
           '--thinking', thinking,
           '--timeout', str(timeout),
           '--json']
    if session_id:
        cmd += ['--session-id', session_id]

    # Guard against oversized messages (tool results can grow very large).
    cmd, stdin_input, tmp_path = _prepare_agent_message(cmd, message, agent_config or {})
    try:
        # Avoid printing the entire message here — it's already logged via
        # _log_messages_pretty("AGENT_PROMPT", ...). Show only the command
        # skeleton and a preview of the message argument.
        display_cmd = list(cmd)
        if len(display_cmd) > 6 and display_cmd[4] == '--message':
            msg_preview = str(display_cmd[5])[:80].replace('\n', ' ')
            display_cmd[5] = f"{msg_preview}{'...' if len(str(cmd[5])) > 80 else ''}"
        print(f"[agent] Running: {' '.join(display_cmd[:6])} ...", file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True,
                                input=stdin_input, timeout=timeout)

        # Some OpenClaw models do not support the requested thinking level and
        # fail with "Thinking level \"x\" is not supported for <model>. Use
        # one of: ...".  Rather than forcing the user to change every aiconfig
        # whenever the active model changes, retry once with thinking=off.
        stderr_str = result.stderr.strip() if result.stderr else ''
        if _is_unsupported_thinking_error(stderr_str) and thinking != 'off':
            print(color(f"[agent] OpenClaw rejected thinking={thinking}; retrying with thinking=off",
                        'orange', 'bold'), file=sys.stderr)
            log_message("AGENT_WARNING",
                        f"thinking={thinking} rejected by openclaw; retrying with off")
            # Rebuild the command with thinking=off, preserving message-file/stdin handling.
            cmd2 = [bin_path, 'agent',
                    '--agent', agent_id,
                    '--message', message,
                    '--thinking', 'off',
                    '--timeout', str(timeout),
                    '--json']
            if session_id:
                cmd2 += ['--session-id', session_id]
            cmd2, stdin_input2, tmp_path2 = _prepare_agent_message(cmd2, message, agent_config or {})
            try:
                display_cmd2 = list(cmd2)
                if len(display_cmd2) > 6 and display_cmd2[4] == '--message':
                    msg_preview2 = str(display_cmd2[5])[:80].replace('\n', ' ')
                    display_cmd2[5] = f"{msg_preview2}{'...' if len(str(cmd2[5])) > 80 else ''}"
                print(f"[agent] Running: {' '.join(display_cmd2[:6])} ...", file=sys.stderr)
                result = subprocess.run(cmd2, capture_output=True, text=True,
                                        input=stdin_input2, timeout=timeout)
            finally:
                if tmp_path2 and os.path.exists(tmp_path2):
                    try:
                        os.remove(tmp_path2)
                    except OSError as e:
                        print(color(f"[agent] Warning: could not remove temp message file {tmp_path2}: {e}",
                                    'orange', 'bold'), file=sys.stderr)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as e:
                print(color(f"[agent] Warning: could not remove temp message file {tmp_path}: {e}", 'orange', 'bold'),
                      file=sys.stderr)

    # Forward agent's own stderr (gateway errors, fallback notices, etc.)
    if result.stderr:
        print(result.stderr.rstrip('\n'), file=sys.stderr)

    stdout = result.stdout.strip()
    stderr_str = result.stderr.strip() if result.stderr else ''
    combined = (stdout + '\n' + stderr_str).strip()
    if not combined:
        raise RuntimeError("Agent returned no output on stdout or stderr.")

    # The JSON block starts at the first '{' in stdout; fall back to combined.
    source = stdout if '{' in stdout else combined
    start = source.find('{')
    if start < 0:
        raise RuntimeError(f"Agent output contains no JSON.\n{combined[:300]}")

    data = json.loads(source[start:])

    # Handle nested payload structures in modern agent responses.
    if 'result' in data and 'payloads' in data['result']:
        payloads = data['result']['payloads']
    else:
        payloads = data.get('payloads', [])
    if not payloads:
        raise RuntimeError("Agent response has no payloads.")

    response_text = '\n'.join(p.get('text', '') for p in payloads if p.get('text'))
    if not response_text:
        raise RuntimeError("Agent response is empty.")

    model = data.get('meta', {}).get('agentMeta', {}).get('model', '?')
    return response_text, model


def _looks_like_agent_llm_failure(response_text):
    """
    Detect common upstream agent/gateway failure messages so we can surface
    them as agent errors rather than treating them as a final plain-text answer.
    """
    if not response_text:
        return False
    lower = response_text.strip().lower()
    failure_prefixes = (
        "llm request failed",
        "llm request timed out",
        "gatewayclientrequesterror",
        "failovererror",
        "network connection was interrupted",
    )
    return any(lower.startswith(prefix) for prefix in failure_prefixes)


# Matches an external agent's *native* tool-call serialization
# (e.g. `<tools><call tool="exec" ...>` or its angle-bracket-stripped variant
# `tools  call tool="exec" ...`). When the agent emits this, LOS cannot execute
# it and — worse — the raw text would be forwarded verbatim to the user
# if we mistook it for a final answer.
AGENT_NATIVE_TOOLCALL_RE = re.compile(r'\bcall\s+tool\s*=\s*"[A-Za-z_]\w*"')

# Matches a SHORT "in-progress / deferred action" acknowledgement — the agent
# promises to work on it LATER, but an aicall dispatch ends when it replies, so
# "later" never comes and the user is left waiting forever.
#   "On it — generating the Backrooms version now."
#   "Let me generate that for you."
# Only replies up to AGENT_DEFERRED_MAX_LEN characters are checked: a real
# deferred ack is a one-liner, while a long multi-paragraph reply is already a
# finished answer. Checking full-length replies matched ordinary phrases
# ("…install a Breeze eSIM on it"), and those swallowed answers came back as
# confused meta-commentary about the "bridge". "on it" therefore matches only
# at the very start of a reply ("On it!"), never mid-sentence.
AGENT_DEFERRED_MAX_LEN = 300
AGENT_DEFERRED_RE = re.compile(
    r'^\s*on it\b|'
    r'\b(working on (it|this|that)|generat(ing|es) (the|a|your|now|that)|'
    r'creat(ing|es) (the|a|your|now|that)|let me (generate|create|make|work on|get|whip)|'
    r"i('ll| will) (generate|create|make|send|get back to|work on|whip)|"
    r'one (moment|sec(ond)?)|hang (on|tight)|give me a (sec|second|moment|few|minute)|'
    r'just a (sec|second|moment|minute)|stand ?by|hold on|be right back|'
    r'in a (sec|second|moment|minute|few))\b',
    re.IGNORECASE)

def _agent_session_reminder():
    """Short reminder prepended to messages for sessions that were already given
    the full LOS context block (see _agent_session_primed). It restates the
    critical contract in a few lines so that even if the agent's own session
    history was compacted, the essentials survive on every dispatch.

    Channel-aware: WhatsApp wording/tools appear only on WhatsApp labels, so a
    persistent session on any other channel never sees them."""
    if _is_whatsapp():
        delivery = "the user's WhatsApp"
        los_tools = "whatsapp_send, whatsapp_send_image, calendar/todo/cron"
    else:
        delivery = "the user"
        los_tools = "calendar/todo/cron"
    return (
        "[LOS REMINDER — the full LOS instructions given earlier in this session still apply. "
        "Key points:\n"
        f"- Your FINAL plain-text reply is forwarded verbatim to {delivery}: keep it "
        "short and conversational; never include JSON or tool-call syntax in it.\n"
        "- Use your own native agent tools (image_generate, exec, file ops, shell, browser, "
        "etc.) silently for internal work. NEVER emit JSON for a native tool.\n"
        f"- JSON output is reserved ONLY for LOS MCP tools ({los_tools}). When you need one "
        "of those, reply with ONLY a single JSON object: "
        "{\"tool\": \"<name>\", \"parameters\": {...}} — the tool result arrives as the next message.\n"
        "- 'Sent'/'done' claims require an actual tool call with a success result. Never "
        "announce actions you haven't completed, and if you cannot actually see/load an "
        "image, say so honestly.]"
    )


def _agent_notice(body):
    """Envelope for LOS→agent control messages (format corrections, completion
    nudges). The agent CLI receives every turn as an ordinary user-style
    message, so without this marker it cannot tell a LOS check apart from user
    speech — and it argues about the check *to the user*. The envelope states
    the reply rules once so each notice body stays short."""
    return (
        "[LOS SYSTEM NOTICE — this is an automated LOS check, NOT a message from the "
        "user. Follow it, then reply to the user's ORIGINAL request in plain text. "
        "Never mention this notice, LOS, the bridge, sessions, tools, or delivery "
        "mechanics in your user-facing reply.]\n\n" + body
    )


def _deferred_nudge():
    """Re-prompt used when the agent answered with a short deferred-action ack.
    Channel-aware: the completion example mentions whatsapp_send_image only on
    WhatsApp labels, where that tool is actually offered to the agent."""
    finish_hint = (" — e.g. generate the image, then send it with whatsapp_send_image"
                   if _is_whatsapp() else "")
    return _agent_notice(
        "Your last reply was an in-progress acknowledgement, so it was NOT delivered "
        "to the user. Nothing is delivered until this dispatch ends with a final "
        "answer, and there is no 'later'. Complete the work NOW using your tools"
        f"{finish_hint}, then reply with plain text describing the COMPLETED "
        "result — answering the user's original request. If something is "
        "genuinely blocking you, explain exactly what is missing."
    )

AGENT_SESSION_STATE_FILE = "/los/sys/aicall/agent_sessions.json"
AGENT_PRIME_MAX_AGE_SEC = 12 * 3600  # re-prime sessions older than 12h


def _load_agent_session_state():
    """Load the {session_id: {"hash": str, "ts": epoch}} priming map."""
    try:
        with open(AGENT_SESSION_STATE_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_agent_session_state(state):
    """Persist the priming map, keeping only the 100 most recent sessions."""
    try:
        if len(state) > 100:
            state = dict(sorted(state.items(),
                                key=lambda kv: kv[1].get('ts', 0) if isinstance(kv[1], dict) else 0)[-100:])
        tmp_path = AGENT_SESSION_STATE_FILE + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(state, f)
        os.replace(tmp_path, AGENT_SESSION_STATE_FILE)
    except Exception:
        pass


def _agent_session_primed(session_id, context_hash):
    """True if this session already received the full context block with the
    same content hash within AGENT_PRIME_MAX_AGE_SEC."""
    if not session_id:
        return False
    entry = _load_agent_session_state().get(session_id)
    if not isinstance(entry, dict):
        return False
    if entry.get('hash') != context_hash:
        return False
    age = datetime.now().timestamp() - float(entry.get('ts', 0))
    return age < AGENT_PRIME_MAX_AGE_SEC


def _mark_agent_session_primed(session_id, context_hash):
    """Record that session_id received the full context block (hash) now."""
    if not session_id:
        return
    state = _load_agent_session_state()
    state[session_id] = {"hash": context_hash, "ts": datetime.now().timestamp()}
    _save_agent_session_state(state)


def call_agent(agent_config, prompt, los_config=None):
    """
    Dispatch a task to an external agent CLI, giving the agent the SAME
    MCP-tool access the LLM path has (prompt-injection + tool loop).

    Flow:
      1. Prepend the MCP tool definitions + JSON call syntax (and entity/date
         context) to the prompt — identical to generate_system_prompt() for LLMs.
      2. Run the agent; parse its reply for JSON tool calls.
      3. If it called tool(s), execute them via call_tool() — which enforces the
         per-label agenda filter automatically through the global ALLOWED_AGENDAS
         (X-LOS-Allowed-Agendas header) — then feed the results back into the
         same session and loop.
      4. When the agent replies with plain text (no tool call), that's the final
         answer: stream it to stdout line-by-line for the ssl_server SSE endpoint.
    """
    # Locate the agent binary — prefer explicit 'bin' from aiconfig
    provider = agent_config.get('provider', '').lower()
    bin_path = agent_config.get('bin') or shutil.which(provider) or provider
    # .config-level 'agent' overrides aiconfig-level 'agent' (from file), default 'main'
    agent_id = (los_config or {}).get('agent') or agent_config.get('agent', 'main')
    thinking  = (los_config or {}).get('thinking') or agent_config.get('thinking', 'low')
    timeout   = int((los_config or {}).get('timeout') or agent_config.get('timeout', 600))
    # Max tool-loop turns — mirrors the LLM path's max_steps (default 9).
    max_steps = int((los_config or {}).get('max_tool_steps')
                    or agent_config.get('max_tool_steps', 9))

    # Log gateway_url if present (informational — some agents read it from their own config)
    gateway_url = agent_config.get('gateway_url', '')
    if gateway_url:
        print(color(f"[agent] Gateway: {gateway_url} (configured for provider={provider})", 'cyan'), file=sys.stderr)

    label = LINE_LABEL or 'agent'

    # ── Build the first message: MCP tool context + user prompt + callbacks ──
    # If a per-project webhook URL is available, build a compact instruction
    # block so the agent can POST back to LOS from any deferred/cron work.
    webhook_url = os.getenv('LOS_WEBHOOK_URL', '')
    callback_block = ''
    if webhook_url:
        # IMPORTANT: Do NOT use the agent's own delivery / reply-channel mechanism
        # for the webhook — it does not resolve HTTP targets for cron job delivery.
        # Instead, embed the curl command inside any scheduled work it creates.
        callback_block = (
            "[LOS-CALLBACK-INSTRUCTIONS]\n"
            "If you create any scheduled, deferred, or cron-based work:\n"
            "DO NOT use any delivery channel or webhook configuration inside the agent's\n"
            "scheduling system — those webhook targets do not resolve for cron jobs.\n\n"
            "INSTEAD, embed this exact curl command as the executable body of the\n"
            "scheduled job (use it as the 'command' field in cron_add with action='execute'):\n\n"
            f'  curl -sk -X POST "{webhook_url}" \\\n'
            '    -H "Content-Type: application/json" \\\n'
            '    -d \'{"message": "YOUR_RESULT_HERE"}\'\n\n'
            "Replace YOUR_RESULT_HERE with the actual message you want to deliver.\n"
            "The URL already contains the authentication token — no extra headers needed.\n"
            "Keep the URL query parameters exactly as given (user, path, token, label);\n"
            "the label parameter is what identifies you as the sender, so the result is\n"
            "attributed to the right agent. You may set job= to any id of your choosing.\n"
            "[/LOS-CALLBACK-INSTRUCTIONS]"
        )
        log_message("AGENT", f"callback_url_injected={webhook_url}")
        print(color("[agent] Callback instructions injected into system context", 'yellow'), file=sys.stderr)

    # Stable session id so tool results feed back into the SAME conversation.
    # For WhatsApp, derive the session id from the sender's phone number so the
    # external agent sees a single persistent conversation per contact.  The
    # caller can override the suffix with LOS_SESSION_SUFFIX for other stable
    # ids; otherwise we fall back to a timestamp (one-shot session).
    session_suffix = os.getenv('LOS_SESSION_SUFFIX', '')
    persistent_session = bool(session_suffix)
    if not session_suffix and _is_whatsapp():
        phone_match = re.search(r'from\s+(\d+)', prompt)
        if phone_match:
            session_suffix = phone_match.group(1)
            persistent_session = True
    if not session_suffix:
        session_suffix = str(int(datetime.now().timestamp()))
        persistent_session = False
    session_id = f"los-{label.replace(':', '_')}-{session_suffix}"

    # Build the system context. Callback instructions are part of the system
    # context (section 8), not appended to the user prompt.  For persistent
    # sessions the LOS memory recall is skipped (the agent's own session
    # already holds the conversation).
    context_block = _build_agent_tool_context(
        los_config, callback_block=callback_block, user_prompt=prompt,
        persistent_session=persistent_session)

    # ── Session priming ───────────────────────────────────────────────
    # A persistent agent session only needs the full context block ONCE — the
    # agent keeps it in its own session history. Re-sending the entire block
    # with every user message (the old behaviour) piles up dozens of stale
    # duplicate preambles in the session, which measurably confuses the agent.
    # After priming we send a short reminder of the contract instead.
    context_hash = hashlib.sha256(context_block.encode('utf-8')).hexdigest()[:16]
    if persistent_session and _agent_session_primed(session_id, context_hash):
        first_message = f"{_agent_session_reminder()}\n\n{prompt}"
        primed_already = True
        log_message("AGENT", f"session={session_id} primed=1 (ctx hash {context_hash}; "
                             f"sending reminder-only prompt)")
    else:
        first_message = ""
        if context_block:
            first_message += context_block + "\n\n"
        first_message += prompt
        primed_already = False
        log_message("AGENT", f"session={session_id} primed=0 (ctx hash {context_hash}; "
                             f"sending full context block)")

    log_message("AGENT",
                f"provider={provider} agent={agent_id} gateway={gateway_url} bin={bin_path} "
                f"session={session_id} prompt_len={len(prompt)} max_steps={max_steps}")

    # Log the agent's first-turn prompt in the same structured format as LLM calls.
    # The model is not known yet (it comes back from the agent on step 1), so log '?' for now.
    _log_messages_pretty("AGENT_PROMPT", "?", gateway_url or bin_path,
                         [{"role": "user", "content": first_message}])

    current_message = first_message
    final_text = ''
    model = '?'
    format_corrections = 0  # how many times we re-prompted over leaked tool syntax

    try:
        for step in range(1, max_steps + 1):
            print(color(f"[agent] --- Agent step {step}/{max_steps} (session={session_id}) ---", 'magenta', 'bold'),
                  file=sys.stderr)
            response_text, model = _invoke_agent(
                bin_path, agent_id, current_message, thinking, timeout,
                session_id, agent_config, provider=provider)

            # The session demonstrably received the full context once step 1
            # succeeded — record priming so later dispatches send the short
            # reminder instead of another copy of the context block.
            if step == 1 and persistent_session and not primed_already:
                _mark_agent_session_primed(session_id, context_hash)
                primed_already = True

            preamble, tool_calls = _extract_agent_tool_calls(response_text)

            if not tool_calls:
                # The agent leaked its NATIVE tool-call serialization (e.g.
                # `call tool="exec" ...`) — LOS can't execute it and the user
                # must never see it as a reply. Re-prompt with a correction
                # instead of treating it as the final answer.
                if AGENT_NATIVE_TOOLCALL_RE.search(response_text or ''):
                    log_message("AGENT_WARNING",
                                f"agent leaked native tool-call syntax (session={session_id}): "
                                f"{(response_text or '')[:200]!r}")
                    format_corrections += 1
                    if format_corrections <= 2 and step < max_steps:
                        print(color("[agent] leaked native tool-call syntax — re-prompting with correction",
                                    'orange', 'bold'), file=sys.stderr)
                        current_message = _agent_notice(
                            "Your last reply contained an internal tool-call serialization "
                            "(\"call tool=...\") that the user CANNOT see and LOS cannot execute. "
                            "Do NOT emit that format. Use your native tools silently without "
                            "printing them. If you need an LOS MCP tool, reply with ONLY one JSON "
                            "object {\"tool\": \"<name>\", \"parameters\": {...}}. Otherwise give "
                            "your final answer as plain text."
                        )
                        continue
                    # Out of correction attempts — substitute a safe user-facing message
                    final_text = ("Sorry, I hit a technical hiccup processing that on my side — "
                                  "could you try again?")
                    break

                # Deferred-action reply: the agent announced work it will do
                # "later", but this dispatch ends when it replies — "later"
                # never happens. Force it to complete the work NOW (or fail
                # honestly) instead of acknowledging and hanging up.
                if (len((response_text or '').strip()) <= AGENT_DEFERRED_MAX_LEN
                        and AGENT_DEFERRED_RE.search(response_text or '')):
                    log_message("AGENT_WARNING",
                                f"agent gave deferred-action reply (session={session_id}): "
                                f"{(response_text or '')[:200]!r}")
                    format_corrections += 1
                    if format_corrections <= 3 and step < max_steps:
                        print(color("[agent] deferred-action reply — forcing completion in this dispatch",
                                    'orange', 'bold'), file=sys.stderr)
                        current_message = _deferred_nudge()
                        continue
                    final_text = ("Sorry, I couldn't complete that in this run — "
                                  "please ask me again.")
                    break

                # Plain-text reply → final answer, unless it is an upstream
                # agent/gateway failure message (e.g. "LLM request failed").
                if _looks_like_agent_llm_failure(response_text):
                    print(color("agent responded with: LLM request filed", 'bright_red', 'bold'),
                          file=sys.stderr)
                    sys.exit(1)
                final_text = response_text
                break

            # Only log intermediate (tool-call) turns as AGENT_RESPONSE.
            # The final plain-text answer is logged once as AGENT_FINAL.
            _log_text_pretty("AGENT_RESPONSE", model, gateway_url or bin_path, response_text, step=step)

            if preamble:
                print(color(f"[agent] (preamble) {preamble[:300]}", 'dim'), file=sys.stderr)

            # Execute each requested tool. call_tool() injects the per-label
            # X-LOS-Allowed-Agendas filter via the global ALLOWED_AGENDAS, so the
            # agent is subject to exactly the same access control as the LLMs.
            known_mcp_tools = set(get_tools_description().keys())
            unknown_tools = [tc.get('tool') for tc in tool_calls if tc.get('tool') not in known_mcp_tools]
            if unknown_tools:
                # The agent emitted JSON for a tool that is NOT an LOS MCP tool
                # (e.g. its own native image_generate). Forwarding it to the
                # action server produces a confusing 404. Never execute it;
                # force a correction or, if we're out of patience, return a safe
                # user-facing message.
                log_message("AGENT_WARNING",
                            f"agent tried unknown/non-MCP tool(s) (session={session_id}): "
                            f"{unknown_tools!r}")
                format_corrections += 1
                if format_corrections <= 3 and step < max_steps:
                    print(color("[agent] unknown tool call — re-prompting with correction",
                                'orange', 'bold'), file=sys.stderr)
                    current_message = _agent_notice(
                        "Your last reply contained a JSON tool call for tool(s) that are NOT "
                        "LOS MCP tools: " + ", ".join(repr(t) for t in unknown_tools) + ". "
                        "LOS cannot execute those. Use your OWN native agent tools "
                        "silently (without emitting JSON). If you need an LOS MCP tool, reply with "
                        "ONLY one JSON object {\"tool\": \"<name>\", \"parameters\": {...}} "
                        "using a tool name from the LOS list. Otherwise give your final "
                        "answer as plain text."
                    )
                    continue
                # Out of correction attempts — do NOT run the unknown tool.
                final_text = ("Sorry, I hit a technical hiccup processing that on my side — "
                              "could you try again?")
                break

            results = []
            for tc in tool_calls:
                tool_name = tc.get('tool')
                params = tc.get('parameters', {}) or {}
                print(color(f"[agent] step {step}: tool -> {tool_name} {params}", 'yellow'), file=sys.stderr)
                ok, res = call_tool(tool_name, params, los_config)
                results.append((tool_name, res))

            # Feed tool results back for the next turn (same session).
            results_text = "\n\n".join(
                f"Tool `{name}` result:\n{res}" for name, res in results)
            current_message = (
                "TOOL RESULTS — you requested the following MCP tool call(s); "
                "here are the results:\n\n"
                f"{results_text}\n\n"
                "Continue the task using these results. When finished, reply with your "
                "final answer as PLAIN TEXT (no JSON). If you need another tool, reply "
                "with ONLY the next JSON tool-call object."
            )
        else:
            # Loop exhausted without a plain-text final answer.
            print(color(f"[agent] Reached maximum tool steps ({max_steps}).", 'red'), file=sys.stderr)
            if not final_text:
                final_text = ("Reached the maximum number of tool steps "
                              f"({max_steps}) without a final answer.")

        if not final_text:
            final_text = "Agent produced no final response."

        # Log the final agent answer as a single pretty text block.
        _log_text_pretty("AGENT_FINAL", model, gateway_url or bin_path, final_text)

        # Stream the final answer to stdout line-by-line (SSE-friendly).
        for line in final_text.split('\n'):
            print(line)
            sys.stdout.flush()

        # If running with a project directory, write pending file for late callback.
        project_dir = os.getenv('LOS_PROJECT_DIR')
        if project_dir and os.path.exists(project_dir):
            try:
                pending_file = os.path.join(project_dir, '.agent_pending.json')
                with open(pending_file, 'w') as f:
                    json.dump({'message': final_text}, f)
            except Exception as e:
                print(color(f"Error writing pending agent file: {e}", 'bright_red', 'bold'), file=sys.stderr)

        log_message("AGENT_RESPONSE",
                    f"provider={provider} agent={agent_id} session={session_id} "
                    f"len={len(final_text)} model={model}")

    except subprocess.TimeoutExpired:
        print(color(f"Error: Agent timed out after {timeout}s.", 'red', 'bold'), file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(color(f"Error: Failed to parse agent JSON: {e}", 'red', 'bold'), file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(color(f"Error: Agent binary not found at '{bin_path}'.", 'red', 'bold'), file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(color(f"Error: Agent failed: {e}", 'red', 'bold'), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(color(f"Error: Agent failed: {e}", 'red', 'bold'), file=sys.stderr)
        sys.exit(1)


def _parse_history_timestamp(ts):
    """Parse an ISO-8601 timestamp from history.json; return None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        # Treat naive timestamps as UTC so comparisons are safe.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _format_history_block(messages, header="RECENT PROJECT HISTORY"):
    """Turn a list of {role, content, ...} messages into a prompt text block."""
    if not messages:
        return ""
    lines = [f"**{header}:**"]
    for m in messages:
        role = m.get('role', '?')
        content = (m.get('content') or '').strip()
        if not content:
            continue
        label = m.get('label')
        ts = m.get('timestamp', '')
        ts_short = ''
        if ts:
            dt = _parse_history_timestamp(ts)
            if dt:
                ts_short = dt.strftime("%Y-%m-%d %H:%M") + " "
        role_label = 'User'
        if role == 'assistant':
            role_label = f'Assistant ({label})' if label else 'Assistant'
        # Keep the first line; if multi-line, show a compact indicator.
        first_line = content.splitlines()[0]
        suffix = " ..." if len(content.splitlines()) > 1 else ""
        lines.append(f"- {ts_short}{role_label}: {first_line}{suffix}")
    return "\n".join(lines)


def load_project_history(history_file, current_label, hours=None):
    """
    Load prior conversation turns from history.json for LLM context injection.

    Start point logic (the 'label-aware' rule the user requested):
      - Find the last assistant message in history whose label matches
        current_label.  Start from that exchange's paired user message so the
        AI sees the full context from the last time it was involved — including
        any subsequent turns handled by other AIs.
      - If current_label has never appeared in the history the AI is new to
        this conversation, so include everything from the beginning.

    Optional `hours` time window (e.g. 4):
      - When provided, only entries with a timestamp within the last N hours
        are kept. This is used in entity-mode chats so the prompt carries the
        recent conversation the canvas is showing, not everything since the
        dawn of time.

    The trailing user message is always dropped: ai.js pre-saves the current
    prompt to history.json just before launching the stream, so the final user
    entry equals the stdin prompt that main() will add explicitly — including
    it here would duplicate the current turn.

    Both user AND assistant messages are included (full transcript, no
    content truncation).  The `thinking`, `id`, `timestamp` metadata fields
    are stripped — only role + content reach the LLM.

    Leading orphan assistant messages are skipped so the sequence always
    begins with a user turn (required by most LLM APIs).
    """
    if not history_file or not os.path.exists(history_file):
        return []

    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(raw, list) or not raw:
        return []

    # Keep only user/assistant entries with non-empty content
    entries = [
        e for e in raw
        if isinstance(e, dict)
        and e.get('role') in ('user', 'assistant')
        and (e.get('content') or '').strip()
    ]

    if not entries:
        return []

    # Drop trailing user message(s): the current prompt was pre-saved by the
    # browser before streaming started, so the last user entry == our stdin
    # prompt.  Remove it here; main() adds it explicitly at the end.
    while entries and entries[-1]['role'] == 'user':
        entries.pop()

    if not entries:
        return []

    # ── Optional time-window filter ───────────────────────────────────────
    if hours is not None:
        try:
            hours = float(hours)
        except (ValueError, TypeError):
            hours = None
    if hours is not None and hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        entries = [
            e for e in entries
            if _parse_history_timestamp(e.get('timestamp')) is not None
            and _parse_history_timestamp(e.get('timestamp')) >= cutoff
        ]
        if not entries:
            return []

    # ── Determine start index ─────────────────────────────────────────────
    # Find the LAST assistant entry whose label == current_label.
    # Then back up to include its paired user message.
    # If the label has never appeared, start_idx stays 0 (full history).
    start_idx = 0

    if current_label:
        last_match_idx = None
        for i, e in enumerate(entries):
            if e['role'] == 'assistant' and e.get('label', '') == current_label:
                last_match_idx = i
        if last_match_idx is not None:
            # Include the user message that precedes this assistant turn
            pair_start = last_match_idx
            if last_match_idx > 0 and entries[last_match_idx - 1]['role'] == 'user':
                pair_start = last_match_idx - 1
            start_idx = pair_start

    relevant = entries[start_idx:]

    # Skip any leading orphan assistant messages so the first injected message
    # is always a user turn (required by OpenAI-style chat APIs).
    while relevant and relevant[0]['role'] == 'assistant':
        relevant = relevant[1:]

    if not relevant:
        return []

    # Return minimal {role, content} dicts — strip all metadata fields.
    return [{'role': e['role'], 'content': e['content']} for e in relevant]


def main():
    # Install stderr tee to capture all debug messages to the log file
    sys.stderr = TeeStderr(sys.stderr)

    parser = argparse.ArgumentParser(description="Call an LLM with a specific context and prompt.")
    parser.add_argument("label", help="The label from the .config file to use for context.")
    parser.add_argument("prompt_file", nargs='?', help="The file containing the prompt. If not provided, reads from stdin.")
    args = parser.parse_args()

    log_message("SESSION", f"=== aicall session started === label={args.label} prompt_file={args.prompt_file}")

    # Read the prompt
    prompt = ""
    if args.prompt_file and args.prompt_file != '-':
        with open(args.prompt_file, 'r') as f:
            prompt = f.read()
    else:
        prompt = sys.stdin.read()

    # Set global label for response processing
    global LINE_LABEL
    LINE_LABEL = args.label

    label_parts = args.label.split(':')
    primary_label = label_parts[0]

    config = parse_config(primary_label)
    if not config:
        print(color(f"Error: Label '{args.label}' not found in .config file.", 'bright_red', 'bold'), file=sys.stderr)
        sys.exit(1)

    # The label's type= field drives all channel-specific behavior (whatsapp
    # vs not) — label NAMES are user-configurable and may change at any time.
    global LINE_TYPE
    LINE_TYPE = (config.get('type') or '').strip().lower()

    # Determine if debugging is enabled for the target label
    if not _is_whatsapp():
        _ = parse_config_for_debug_flag(primary_label) # This will set GLOBAL_DEBUG_MODE if debug=1 is present

    # Resolve agenda filters for this label and store globally so call_tool() can use them
    global ALLOWED_AGENDAS
    filters = config.get('filters', [])
    if filters:
        ALLOWED_AGENDAS = resolve_allowed_agendas(filters, USERNAME)
        if not (_is_whatsapp()):
            print(f"[agenda-filter] Label '{primary_label}': {len(ALLOWED_AGENDAS) if ALLOWED_AGENDAS else 0} allowed agenda(s): {ALLOWED_AGENDAS}", file=sys.stderr)
    else:
        ALLOWED_AGENDAS = None  # No filters defined — all agendas accessible

    aiconfig_path = config.get("aiconfig")
    if not aiconfig_path:
        print(color(f"Error: 'aiconfig' not found for label '{args.label}' in .config file.", 'bright_red', 'bold'), file=sys.stderr)
        sys.exit(1)

    # Resolve the aiconfig path
    aiconfig_path = os.path.expanduser(aiconfig_path)
    if not os.path.isabs(aiconfig_path):
        aiconfig_path = os.path.join(f'/los/{USERNAME}/', aiconfig_path)

    llm_config = parse_aiconfig(aiconfig_path)
    if not llm_config:
        print(color(f"Error: Could not parse aiconfig file at '{aiconfig_path}'", 'bright_red', 'bold'), file=sys.stderr)
        sys.exit(1)

    # ── Agent dispatch — short-circuit before LLM loop ───────────────
    agent_type = llm_config.get('type', '').lower()
    if agent_type == 'agent':
        provider = llm_config.get('provider', '').lower()
        print(color(f"[aicall] Agent dispatch: provider={provider} label={args.label}", 'green', 'bold'), file=sys.stderr)
        if not provider:
            print(color("Error: aiconfig type='agent' but no 'provider' specified.", 'red', 'bold'), file=sys.stderr)
            sys.exit(1)
        call_agent(llm_config, prompt, config)
        return

    # ── Regular LLM flow ─────────────────────────────────────────────
    # Validate that the config has an 'llms' list (required for call_llm)
    if 'llms' not in llm_config or not llm_config['llms']:
        print(color(f"Error: aiconfig at '{aiconfig_path}' has no 'llms' list.", 'red'), file=sys.stderr)
        sys.exit(1)

    # Prepare the initial system prompt
    system_prompt = generate_system_prompt(prompt, args.label, config)

    # Load prior conversation history from the project's history.json (if any).
    # The server sets LOS_HISTORY_FILE so aicall knows which file to read.
    # Prior turns are injected between the system prompt and the current user
    # turn so the LLM has the full canvas context for this task/entity.
    history_file = os.getenv('LOS_HISTORY_FILE', '')
    prior_messages = []
    if history_file and not (_is_whatsapp()):
        # In entity-mode chats we want the recent conversation the canvas is
        # showing, limited to the last 4 hours. Task mode keeps the full
        # label-aware history window.
        context_mode = os.getenv('LOS_CONTEXT_MODE', 'task')
        history_hours = 4 if context_mode == 'entity' else None
        prior_messages = load_project_history(history_file, args.label, hours=history_hours)
        if prior_messages:
            print(color(f"[history] {len(prior_messages)} prior message(s) injected from {history_file}"
                        f" (context={context_mode}, hours={history_hours})", 'cyan'), file=sys.stderr)
        else:
            print(color(f"[history] No prior messages to inject (file={history_file or 'not set'}, context={context_mode})", 'dim'), file=sys.stderr)

    # Build the messages list: system + prior history + current user prompt
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(prior_messages)
    messages.append({"role": "user", "content": prompt})

    max_steps = 9
    step = 0

    while step < max_steps:
        step += 1
        if not (_is_whatsapp()):
            print(f"\n--- Step {step} ---", file=sys.stderr)

        response = call_llm(llm_config, messages)
        if not response:
            print(color("No response from LLM.", 'bright_red', 'bold'), file=sys.stderr)
            break

        # Extract content
        first_llm = llm_config["llms"][0]
        is_api_generate = first_llm["api_url"].endswith("/api/generate")

        if is_api_generate:
            content = response.get("response", "")
        else:
            content = response["choices"][0]["message"].get("content", "")

        if not content:
            print(color("Invalid response from LLM (content empty).", 'bright_red', 'bold'), file=sys.stderr)
            break

        if GLOBAL_DEBUG_MODE and not (_is_whatsapp()):
            print(f"LLM Response Content: {repr(content)}", file=sys.stderr)

        # Process the response
        tool_called, tool_result = process_llm_response(response, llm_config, config)

        if tool_called:
            # Special handling for WhatsApp memory tools
            if _is_whatsapp() and isinstance(tool_result, list):
                entries = tool_result
                if entries:
                    response_text = "**Your recent conversation history:**\n\n"
                    for entry in entries[-5:]:
                        try:
                            content_obj = json.loads(entry.get("content", "{}"))
                            user_msg = content_obj.get("user_message", "")
                            ai_resp = content_obj.get("ai_response", "")
                            if user_msg and ai_resp:
                                if user_msg.startswith("Incoming WhatsApp"):
                                    user_msg = user_msg.split(": ", 1)[-1] if ": " in user_msg else user_msg
                                response_text += f"**You:** {user_msg}\n"
                                response_text += f"**AI:** {ai_resp}\n\n"
                        except:
                            continue
                    print(response_text.strip())
                    store_conversation_memory(config, messages, args.label, prompt)
                    return
                else:
                    print("No conversation history found.")
                    store_conversation_memory(config, messages, args.label, prompt)
                    return

            messages.append({"role": "assistant", "content": content})
            if tool_result is None or (isinstance(tool_result, str) and not tool_result.strip()):
                tool_result_str = "Tool returned no content (empty result)."
            else:
                tool_result_str = str(tool_result) if not isinstance(tool_result, str) else tool_result

            messages.append({"role": "tool", "content": tool_result_str})

            if tool_result and is_success(tool_result):
                break
        else:
            messages.append({"role": "assistant", "content": content})

        if _is_whatsapp() and not tool_called:
            break

        if not tool_called:
            if not (_is_whatsapp()):
                print("No tool call detected. Task completed.", file=sys.stderr)
            break

    if step >= max_steps:
        if not (_is_whatsapp()):
            print(color(f"Reached maximum steps ({max_steps}).", 'orange', 'bold'), file=sys.stderr)

    store_conversation_memory(config, messages, args.label, prompt)


if __name__ == "__main__":
    main()
