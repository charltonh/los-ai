"""Terminal output helpers — colors, prompts and tables.

Mirrors the naming used by aicall.py's color() so the two tools feel like
one program.  Colour is suppressed when stdout is not a tty or when
NO_COLOR is set.
"""

import os
import sys

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "grey": "90",
    "gray": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_magenta": "95",
    "bright_cyan": "96",
    "orange": "33",
}


def use_color(stream=None):
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("LOS_COLOR") == "1":
        return True
    try:
        return stream.isatty()
    except Exception:
        return False


def color(text, *styles, **kw):
    """Wrap text in ANSI styles, e.g. color('hi', 'green', 'bold')."""
    stream = kw.get("stream")
    if not styles or not use_color(stream):
        return text
    codes = [_CODES[s] for s in styles if s in _CODES]
    if not codes:
        return text
    return "\033[%sm%s\033[0m" % (";".join(codes), text)


# ── Message helpers ─────────────────────────────────────────────────────

def info(msg):
    print("%s %s" % (color("::", "bright_blue", "bold"), msg))


def ok(msg):
    print("%s %s" % (color("✓", "bright_green", "bold"), msg))


def warn(msg):
    print("%s %s" % (color("!", "bright_yellow", "bold"), msg), file=sys.stderr)


def error(msg):
    print("%s %s" % (color("✗", "bright_red", "bold"), msg), file=sys.stderr)


def step(msg):
    print("%s %s" % (color("→", "cyan"), msg))


def heading(msg):
    print()
    print(color(msg, "bold"))
    print(color("─" * max(len(msg), 20), "grey"))


def hr(width=64):
    print(color("─" * width, "grey"))


# ── Input helpers ───────────────────────────────────────────────────────

def ask(prompt, default=None, allow_empty=False):
    """Prompt for a string, showing the current value as the default."""
    while True:
        if default not in (None, ""):
            shown = "%s [%s]: " % (prompt, color(str(default), "cyan"))
        else:
            shown = "%s: " % prompt
        try:
            answer = input(shown).strip()
        except EOFError:
            print()
            warn("standard input ended — using the default")
            return default
        if not answer:
            if default is not None:
                return default
            if allow_empty:
                return ""
            warn("A value is required.")
            continue
        return answer


def ask_yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            answer = input("%s [%s]: " % (prompt, color(hint, "cyan"))).strip().lower()
        except EOFError:
            print()
            warn("standard input ended — using the default (%s)"
                 % ("yes" if default else "no"))
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        warn("Please answer y or n.")


def ask_int(prompt, default=None, minimum=None, maximum=None):
    while True:
        raw = ask(prompt, default)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            warn("Please enter a number.")
            continue
        if minimum is not None and value < minimum:
            warn("Must be at least %d." % minimum)
            continue
        if maximum is not None and value > maximum:
            warn("Must be at most %d." % maximum)
            continue
        return value


def ask_choice(prompt, choices, default=None):
    """Prompt for one of a fixed set of string choices."""
    joined = "/".join(choices)
    while True:
        answer = ask("%s (%s)" % (prompt, joined), default)
        if answer in choices:
            return answer
        warn("Choose one of: %s" % joined)


def pause(msg="Press Enter to continue"):
    try:
        input(color(msg, "grey"))
    except EOFError:
        pass


# ── Table ───────────────────────────────────────────────────────────────

def table(headers, rows, styles=None):
    """Print a simple aligned table.

    styles: optional list, one entry per row, of style tuples applied to
    the whole row (used by `los status` to colour by state).
    """
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            cell = str(row[i]) if i < len(row) else ""
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(str(headers[i]).ljust(widths[i]) for i in range(cols))
    print(color(header_line.rstrip(), "bold"))

    for idx, row in enumerate(rows):
        cells = []
        for i in range(cols):
            cell = str(row[i]) if i < len(row) else ""
            cells.append(cell.ljust(widths[i]))
        line = "  ".join(cells).rstrip()
        if styles and idx < len(styles) and styles[idx]:
            line = color(line, *styles[idx])
        print(line)
