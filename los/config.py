"""Configuration for a LOS installation: /los/sys/config.json.

The file is plain JSON so it can be hand-edited; `los config` reads and
writes the same file.  Writes are atomic and validated, and a .bak copy is
kept of the previous contents.

Dotted keys ("services.ssl_server.port") are used throughout so the CLI,
the interactive editor and the service registry all address settings the
same way.
"""

import json
import os
import re
import secrets
import shutil
import uuid

from . import paths

SCHEMA_VERSION = 2


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


# ── Defaults ────────────────────────────────────────────────────────────

def _default_user():
    """Best guess at the primary LOS user: a /los/<name> that looks like an
    entity (has an 'identity' file) — falling back to $USER."""
    try:
        for name in sorted(os.listdir(paths.BASE_DIR)):
            if name in ("sys", "lost+found") or name.startswith("."):
                continue
            d = os.path.join(paths.BASE_DIR, name)
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "identity")):
                return name
    except OSError:
        pass
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def defaults():
    """A complete default configuration.

    Ports and hosts match the values that were previously hardcoded in
    each service, so an installation behaves identically out of the box.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "install_id": "",
        "user": _default_user(),
        "services": {
            "memory_mcp": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 5102,
            },
            "action_mcp": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 5100,
            },
            "loscron": {
                "enabled": True,
                "interval": 60,
            },
            "ssl_server": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 14001,
                "secret_key": "",
                "debug": False,
            },
        },
        "channels": {
            "whatsapp": {
                "enabled": False,
                "host": "127.0.0.1",
                "port": 5101,
                "headless": True,
                "session_id": "LOS-AI",
                "origin_num": "",
                "owner_num": "",
                "aicall_label": "whatsapp_incoming",
            },
        },
        "ai": {
            "ollama": {
                "enabled": True,
                "managed": False,
                "url": "http://127.0.0.1:11434",
            },
        },
        "gateway": {
            "restart_on_failure": True,
            "max_restarts": 5,
            "restart_window": 600,
            "start_timeout": 45,
            "stop_timeout": 10,
        },
        "logging": {
            # Applies to every service log under log/ (gateway.log,
            # ssl_server.log, action_mcp.log, ...) as well as aicall.log.
            # A single default keeps every log file's behaviour consistent
            # and easy to reason about; "rotate": false disables it
            # everywhere without deleting anything already on disk.
            "rotate": True,
            "max_bytes": 52428800,
            "keep": 3,
        },
        "update": {
            "channel": "stable",
            "repo": "charltonh/los-ai",
            "auto_check": True,
            "auto_apply": False,
        },
        "reporting": {
            "endpoint": "https://los.dynet.com/api/v1/report",
            "interval": 86400,
        },
    }


# ── Dotted key access ───────────────────────────────────────────────────

def get_key(data, dotted, default=None):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_key(data, dotted, value):
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
    return data


def unset_key(data, dotted):
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return False
    return node.pop(parts[-1], None) is not None


def flatten(data, prefix=""):
    """Return [(dotted_key, value), ...] for every leaf in the config."""
    out = []
    for key in sorted(data):
        value = data[key]
        dotted = "%s.%s" % (prefix, key) if prefix else key
        if isinstance(value, dict):
            out.extend(flatten(value, dotted))
        else:
            out.append((dotted, value))
    return out


def coerce(raw):
    """Turn a CLI string into a bool/int/float/str as appropriate."""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


# ── Merge / migrate ─────────────────────────────────────────────────────

def _merge(base, override):
    """Recursively fill missing keys in override from base."""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def migrate(data):
    """Bring an older config file up to the current schema.

    A newer-than-known schema is refused rather than silently downgraded.
    """
    version = data.get("schema_version", 0)
    if version > SCHEMA_VERSION:
        raise ConfigError(
            "config.json uses schema version %s but this LOS understands %s. "
            "Run `los update`." % (version, SCHEMA_VERSION)
        )

    if version < 2:
        # log rotation used to be gateway-only (gateway.log_max_bytes /
        # gateway.log_keep) and did not cover aicall.log at all.  Carry
        # forward an explicit old setting; otherwise the new shared
        # default applies to every log including aicall.log.
        gw = data.get("gateway") or {}
        old_bytes = gw.pop("log_max_bytes", None)
        old_keep = gw.pop("log_keep", None)
        if old_bytes is not None or old_keep is not None:
            logging_cfg = data.setdefault("logging", {})
            if old_bytes is not None:
                logging_cfg.setdefault("max_bytes", old_bytes)
            if old_keep is not None:
                logging_cfg.setdefault("keep", old_keep)

    data["schema_version"] = SCHEMA_VERSION
    return _merge(defaults(), data)


# ── Validation ──────────────────────────────────────────────────────────

_PORT_KEYS = (
    "services.memory_mcp.port",
    "services.action_mcp.port",
    "services.ssl_server.port",
    "channels.whatsapp.port",
)

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,29}$")


def validate(data):
    """Return a list of human-readable problems (empty means valid)."""
    problems = []

    user = data.get("user") or ""
    if not user:
        problems.append("user: not set — run `los config`")
    elif not _USERNAME_RE.match(user):
        problems.append("user: %r is not a valid LOS username" % user)
    elif not os.path.isdir(paths.user_dir(user)):
        problems.append("user: %s does not exist" % paths.user_dir(user))

    # Ports must be sane, and unique among the enabled services.
    seen = {}
    for key in _PORT_KEYS:
        port = get_key(data, key)
        if port is None:
            continue
        if not isinstance(port, int) or not (1 <= port <= 65535):
            problems.append("%s: %r is not a valid port" % (key, port))
            continue
        owner = key.rsplit(".", 1)[0]
        if not get_key(data, owner + ".enabled", False):
            continue
        if port in seen:
            problems.append(
                "%s: port %d already used by %s" % (key, port, seen[port])
            )
        else:
            seen[port] = key

    interval = get_key(data, "services.loscron.interval")
    if not isinstance(interval, int) or interval < 5:
        problems.append("services.loscron.interval: must be an integer >= 5")

    if get_key(data, "channels.whatsapp.enabled"):
        if not os.path.isdir(paths.WHATSAPP_VENV):
            problems.append(
                "channels.whatsapp: enabled but %s is missing — "
                "run `los install --channel whatsapp`" % paths.WHATSAPP_VENV
            )

    max_bytes = get_key(data, "logging.max_bytes")
    if not isinstance(max_bytes, int) or max_bytes < 65536:
        problems.append("logging.max_bytes: must be an integer >= 65536 (64 KiB)")

    keep = get_key(data, "logging.keep")
    if not isinstance(keep, int) or keep < 1:
        problems.append("logging.keep: must be an integer >= 1")

    return problems


# ── Load / save ─────────────────────────────────────────────────────────

def exists():
    return os.path.exists(paths.CONFIG_FILE)


def load(strict=True):
    """Read config.json, migrated and merged with the defaults.

    With strict=False a missing file yields the defaults instead of an
    error, which is what `los install` and `los doctor` want.
    """
    if not exists():
        if strict:
            raise ConfigError(
                "No configuration found at %s — run `los install` first."
                % paths.CONFIG_FILE
            )
        return defaults()
    try:
        with open(paths.CONFIG_FILE, "r") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (paths.CONFIG_FILE, exc))
    except OSError as exc:
        raise ConfigError("Cannot read %s: %s" % (paths.CONFIG_FILE, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s must contain a JSON object" % paths.CONFIG_FILE)
    return migrate(data)


def save(data):
    """Write config.json atomically, keeping one backup."""
    paths.ensure_dirs()
    data = dict(data)
    data["schema_version"] = SCHEMA_VERSION

    tmp = paths.CONFIG_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)

    if os.path.exists(paths.CONFIG_FILE):
        try:
            shutil.copy2(paths.CONFIG_FILE, paths.CONFIG_FILE + ".bak")
        except OSError:
            pass
    os.replace(tmp, paths.CONFIG_FILE)
    return paths.CONFIG_FILE


def ensure_identity(data):
    """Fill in the generated one-off values: install id and secret key."""
    changed = False
    if not data.get("install_id"):
        data["install_id"] = uuid.uuid4().hex
        changed = True
    if not get_key(data, "services.ssl_server.secret_key"):
        set_key(data, "services.ssl_server.secret_key", secrets.token_hex(32))
        changed = True
    return changed
