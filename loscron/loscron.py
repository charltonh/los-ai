#!/usr/bin/env python3
"""
loscron - LOS Cron Daemon

Scans all user entities under /los/ for data/cron/ files and executes
entries that are due. Supports three entry types:
  - date:  Annual/one-time date events (birthdays, holidays)
  - cron:  Traditional 5-field cron expressions
  - cycle: Interval-based recurring entries (every N days/hours/minutes)

Usage:
  loscron.py [--once] [--verbose]

  --once     Run a single pass and exit (don't loop)
  --verbose  Print verbose output to stdout in addition to log
"""

import os
import sys
import json
import time
import signal
import subprocess
import logging
import calendar as cal_module
from datetime import datetime, timedelta
from pathlib import Path

# --- Configuration ---
# Paths and the check interval come from /los/sys/config.json via the
# gateway (los gateway).  The fallbacks reproduce the historical layout, so
# `python loscron.py` on its own behaves exactly as it always has.
LOS_BASE_PATH = os.environ.get("LOS_BASE", "/los")
SYS_PATH = os.environ.get("LOS_SYS", os.path.join(LOS_BASE_PATH, "sys"))
LOSCRON_DIR = os.path.join(SYS_PATH, "loscron")

# When managed, logs and pids live in the shared run/ and log/ directories
# so the git working tree stays clean.
LOG_DIR = os.environ.get("LOS_LOG", LOSCRON_DIR)
RUN_DIR = os.environ.get("LOS_RUN", LOSCRON_DIR)
LOG_FILE = os.path.join(LOG_DIR, "loscron.log")
PID_FILE = os.path.join(RUN_DIR, "loscron.pid")

try:
    CHECK_INTERVAL = int(os.environ.get("LOS_CRON_INTERVAL", "60"))
except ValueError:
    CHECK_INTERVAL = 60  # seconds between checks

# Directories to skip when scanning /los/
SKIP_DIRS = {'sys', 'lost+found'}

# --- Logging Setup ---
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RUN_DIR, exist_ok=True)

logger = logging.getLogger('loscron')
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(file_handler)

# Console handler (added if --verbose)
console_handler = None

# --- Signal Handling ---
running = True

def signal_handler(signum, frame):
    global running
    logger.info(f"Received signal {signum}, shutting down...")
    running = False

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


# --- Cron Expression Parser ---
def matches_cron_field(field_expr, current_value, min_val, max_val):
    """Check if a cron field expression matches the current value."""
    if field_expr == '*':
        return True

    for part in field_expr.split(','):
        part = part.strip()

        # Handle step values: */5 or 1-10/2
        step = 1
        if '/' in part:
            part, step_str = part.split('/', 1)
            try:
                step = int(step_str)
            except ValueError:
                continue

        # Handle ranges: 1-5
        if '-' in part and part != '*':
            try:
                range_start, range_end = part.split('-', 1)
                range_start = int(range_start)
                range_end = int(range_end)
                if range_start <= current_value <= range_end:
                    if (current_value - range_start) % step == 0:
                        return True
            except ValueError:
                continue
        elif part == '*':
            # */step
            if (current_value - min_val) % step == 0:
                return True
        else:
            # Single value
            try:
                if int(part) == current_value:
                    return True
            except ValueError:
                continue

    return False


def matches_cron_expression(expression, now):
    """Check if a 5-field cron expression matches the current time."""
    parts = expression.strip().split()
    if len(parts) != 5:
        return False

    minute, hour, dom, month, dow = parts

    return (
        matches_cron_field(minute, now.minute, 0, 59) and
        matches_cron_field(hour, now.hour, 0, 23) and
        matches_cron_field(dom, now.day, 1, 31) and
        matches_cron_field(month, now.month, 1, 12) and
        matches_cron_field(dow, now.weekday() if now.weekday() != 6 else 0, 0, 6)
        # Note: weekday() returns 0=Mon..6=Sun, cron uses 0=Sun..6=Sat
        # Adjusted: Python weekday 6 (Sun) -> cron 0, else Python weekday + 1... 
        # Actually let's fix this properly:
    )


def python_weekday_to_cron(python_weekday):
    """Convert Python's weekday (0=Mon..6=Sun) to cron (0=Sun..6=Sat)."""
    return (python_weekday + 1) % 7


def matches_cron_expression_fixed(expression, now):
    """Check if a 5-field cron expression matches the current time."""
    parts = expression.strip().split()
    if len(parts) != 5:
        return False

    minute_expr, hour_expr, dom_expr, month_expr, dow_expr = parts
    cron_dow = python_weekday_to_cron(now.weekday())

    return (
        matches_cron_field(minute_expr, now.minute, 0, 59) and
        matches_cron_field(hour_expr, now.hour, 0, 23) and
        matches_cron_field(dom_expr, now.day, 1, 31) and
        matches_cron_field(month_expr, now.month, 1, 12) and
        matches_cron_field(dow_expr, cron_dow, 0, 6)
    )


# --- Date Rule Computation ---

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
    weekday: 0=Monday..6=Sunday (Python convention). nth: 1-based."""
    try:
        first_day = datetime(year, month, 1)
        days_ahead = weekday - first_day.weekday()
        if days_ahead < 0:
            days_ahead += 7
        first_occurrence = first_day + timedelta(days=days_ahead)
        target = first_occurrence + timedelta(weeks=nth - 1)
        if target.month != month:
            return None
        return target
    except (ValueError, OverflowError):
        return None


def compute_last_weekday(year, month, weekday):
    """Compute the last occurrence of a weekday in a given month."""
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
    Handles fixed dates, nth_weekday, last_weekday, and easter rules."""
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
        # Fixed date (no rule)
        month = entry.get('month')
        day = entry.get('day')
        if month is None or day is None:
            return None
        try:
            return datetime(year, month, day)
        except ValueError:
            return None


# --- Entry Evaluation ---
def should_fire_date(entry, now):
    """Check if a date-type entry should fire right now.
    Supports fixed dates, nth_weekday rules, last_weekday rules, and easter rules."""
    entry_year = entry.get('year')

    # If year is specified, only fire in that year
    if entry_year is not None and now.year != entry_year:
        return False

    # Resolve the actual date for this year using rules
    resolved_date = resolve_cron_date(entry, now.year)
    if resolved_date is None:
        return False

    # Check if today matches the resolved date
    if now.date() != resolved_date.date():
        return False

    # Avoid duplicate fires: check last_run
    last_run = entry.get('last_run')
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run)
            if last_run_dt.date() == now.date():
                return False
        except (ValueError, TypeError):
            pass

    return True


def should_fire_cron(entry, now):
    """Check if a cron-type entry should fire right now."""
    expression = entry.get('expression')
    if not expression:
        # Try to build expression from schedule dict
        schedule = entry.get('schedule')
        if schedule:
            expression = "{} {} {} {} {}".format(
                schedule.get('minute', '*'),
                schedule.get('hour', '*'),
                schedule.get('dayOfMonth', '*'),
                schedule.get('month', '*'),
                schedule.get('dayOfWeek', '*')
            )
        else:
            return False

    if not matches_cron_expression_fixed(expression, now):
        return False

    # Check last_run to avoid firing multiple times in the same minute
    last_run = entry.get('last_run')
    if last_run:
        try:
            last_run_dt = datetime.fromisoformat(last_run)
            # If already fired this minute, skip
            if (last_run_dt.year == now.year and
                last_run_dt.month == now.month and
                last_run_dt.day == now.day and
                last_run_dt.hour == now.hour and
                last_run_dt.minute == now.minute):
                return False
        except (ValueError, TypeError):
            pass

    return True


def should_fire_cycle(entry, now):
    """Check if a cycle-type entry should fire right now."""
    interval = entry.get('interval')
    anchor = entry.get('anchor')

    if not interval or not anchor:
        return False

    try:
        anchor_dt = datetime.fromisoformat(anchor)
    except (ValueError, TypeError):
        logger.warning(f"Invalid anchor datetime for entry {entry.get('id')}: {anchor}")
        return False

    # Calculate interval as timedelta
    days = interval.get('days', 0)
    hours = interval.get('hours', 0)
    minutes = interval.get('minutes', 0)
    delta = timedelta(days=days, hours=hours, minutes=minutes)

    if delta.total_seconds() <= 0:
        return False

    # Determine the reference point: last_run or anchor
    last_run = entry.get('last_run')
    reference_dt = anchor_dt

    if last_run:
        try:
            reference_dt = datetime.fromisoformat(last_run)
        except (ValueError, TypeError):
            reference_dt = anchor_dt

    # Check if enough time has passed since the reference
    next_fire = reference_dt + delta
    if now >= next_fire:
        return True

    return False


# --- File Operations ---
def find_all_cron_dirs():
    """Find all data/cron/ directories across all users and their sub-entities."""
    cron_dirs = []

    if not os.path.isdir(LOS_BASE_PATH):
        logger.error(f"LOS base path not found: {LOS_BASE_PATH}")
        return cron_dirs

    # Scan /los/ for user directories
    try:
        for user_dir in os.listdir(LOS_BASE_PATH):
            if user_dir in SKIP_DIRS or user_dir.startswith('.'):
                continue

            user_path = os.path.join(LOS_BASE_PATH, user_dir)
            if not os.path.isdir(user_path):
                continue

            # Recursively find all data/cron/ directories within this user
            _find_cron_dirs_recursive(user_path, cron_dirs)
    except OSError as e:
        logger.error(f"Error scanning {LOS_BASE_PATH}: {e}")

    return cron_dirs


def _find_cron_dirs_recursive(base_path, result_list):
    """Recursively find data/cron/ directories."""
    cron_dir = os.path.join(base_path, 'data', 'cron')
    if os.path.isdir(cron_dir):
        result_list.append(cron_dir)

    # Look for sub-entities by checking for an 'entities' file
    entities_file = os.path.join(base_path, 'entities')
    if os.path.isfile(entities_file):
        try:
            with open(entities_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    entity_name = line.split()[0]
                    sub_path = os.path.join(base_path, entity_name)
                    if os.path.isdir(sub_path):
                        _find_cron_dirs_recursive(sub_path, result_list)
        except OSError as e:
            logger.warning(f"Error reading entities file {entities_file}: {e}")


def read_cron_entries_from_dir(cron_dir):
    """Read all cron entries from all files in a data/cron/ directory."""
    entries = []

    if not os.path.isdir(cron_dir):
        return entries

    try:
        for filename in os.listdir(cron_dir):
            if filename == 'log' or filename.startswith('.'):
                continue

            filepath = os.path.join(cron_dir, filename)
            if not os.path.isfile(filepath):
                continue

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        try:
                            entry = json.loads(line)
                            if isinstance(entry, dict) and 'id' in entry:
                                entry['_source_file'] = filepath
                                entry['_source_dir'] = cron_dir
                                entries.append(entry)
                        except json.JSONDecodeError:
                            logger.debug(f"Skipping non-JSON line in {filepath}:{line_num}")
            except OSError as e:
                logger.warning(f"Error reading cron file {filepath}: {e}")
    except OSError as e:
        logger.warning(f"Error listing cron directory {cron_dir}: {e}")

    return entries


def update_entry_last_run(entry, now):
    """Update the last_run field of an entry in its source file."""
    source_file = entry.get('_source_file')
    entry_id = entry.get('id')

    if not source_file or not entry_id:
        logger.warning(f"Cannot update last_run: missing source_file or id for entry")
        return False

    try:
        # Read all lines
        with open(source_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Find and update the entry
        updated = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                new_lines.append(line)
                continue

            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict) and obj.get('id') == entry_id:
                    obj['last_run'] = now.isoformat()
                    new_lines.append(json.dumps(obj) + '\n')
                    updated = True
                else:
                    new_lines.append(line)
            except json.JSONDecodeError:
                new_lines.append(line)

        if updated:
            with open(source_file, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            return True
        else:
            logger.warning(f"Entry {entry_id} not found in {source_file} for last_run update")
            return False

    except OSError as e:
        logger.error(f"Error updating last_run in {source_file}: {e}")
        return False


# --- Action Execution ---
def execute_entry(entry, now):
    """Execute the action for a fired entry."""
    action = entry.get('action', 'display')
    title = entry.get('title', 'Unknown')
    entry_id = entry.get('id', '?')
    entry_type = entry.get('type', '?')

    if action == 'display':
        # Display-only entries don't execute anything
        logger.info(f"[DISPLAY] {entry_type} entry fired: '{title}' (id={entry_id})")
        update_entry_last_run(entry, now)
        return

    if action == 'execute':
        command = entry.get('command')
        if not command:
            logger.warning(f"[EXECUTE] Entry '{title}' (id={entry_id}) has no command to execute")
            update_entry_last_run(entry, now)
            return

        logger.info(f"[EXECUTE] Running command for '{title}' (id={entry_id}): {command}")

        try:
            # Determine working directory from the source directory
            source_dir = entry.get('_source_dir', '')
            # Go up from data/cron/ to the entity root
            work_dir = os.path.dirname(os.path.dirname(source_dir)) if source_dir else LOS_BASE_PATH

            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=work_dir
            )

            if result.returncode == 0:
                logger.info(f"[EXECUTE] Command succeeded for '{title}' (id={entry_id})")
                if result.stdout.strip():
                    logger.info(f"  stdout: {result.stdout.strip()[:500]}")
            else:
                logger.warning(f"[EXECUTE] Command failed for '{title}' (id={entry_id}), rc={result.returncode}")
                if result.stderr.strip():
                    logger.warning(f"  stderr: {result.stderr.strip()[:500]}")

        except subprocess.TimeoutExpired:
            logger.error(f"[EXECUTE] Command timed out for '{title}' (id={entry_id})")
        except Exception as e:
            logger.error(f"[EXECUTE] Error running command for '{title}' (id={entry_id}): {e}")

        update_entry_last_run(entry, now)
        return

    if action == 'notify':
        logger.info(f"[NOTIFY] Notification for '{title}' (id={entry_id}) - notification system not yet implemented")
        update_entry_last_run(entry, now)
        return

    logger.warning(f"Unknown action '{action}' for entry '{title}' (id={entry_id})")


# --- Main Loop ---
def run_check():
    """Run a single check of all cron entries."""
    now = datetime.now()
    cron_dirs = find_all_cron_dirs()

    total_entries = 0
    fired_entries = 0

    for cron_dir in cron_dirs:
        entries = read_cron_entries_from_dir(cron_dir)
        total_entries += len(entries)

        for entry in entries:
            if not entry.get('enabled', True):
                continue

            entry_type = entry.get('type', '')
            should_fire = False

            if entry_type == 'date':
                should_fire = should_fire_date(entry, now)
            elif entry_type == 'cron':
                should_fire = should_fire_cron(entry, now)
            elif entry_type == 'cycle':
                should_fire = should_fire_cycle(entry, now)

            if should_fire:
                fired_entries += 1
                try:
                    execute_entry(entry, now)
                except Exception as e:
                    logger.error(f"Error executing entry {entry.get('id')}: {e}")

    logger.debug(f"Check complete: {total_entries} entries scanned, {fired_entries} fired "
                 f"across {len(cron_dirs)} cron directories")


def write_pid():
    """Write PID file."""
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.warning(f"Could not write PID file: {e}")


def remove_pid():
    """Remove PID file."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except OSError:
        pass


def main():
    global running, console_handler

    # Parse arguments
    once_mode = '--once' in sys.argv
    verbose = '--verbose' in sys.argv

    if verbose:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        ))
        logger.addHandler(console_handler)

    logger.info("=" * 60)
    logger.info(f"loscron starting (pid={os.getpid()}, mode={'once' if once_mode else 'daemon'})")
    logger.info("=" * 60)

    write_pid()

    try:
        if once_mode:
            run_check()
        else:
            while running:
                try:
                    run_check()
                except Exception as e:
                    logger.error(f"Unexpected error during check: {e}")

                # Sleep in small increments to allow signal handling
                sleep_end = time.time() + CHECK_INTERVAL
                while running and time.time() < sleep_end:
                    time.sleep(1)
    finally:
        remove_pid()
        logger.info("loscron stopped")


if __name__ == '__main__':
    main()
