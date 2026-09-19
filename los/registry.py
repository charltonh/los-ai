"""The service registry: what `los gateway` knows how to run.

Every managed process is declared here once.  The gateway, `los status`,
`los doctor` and the installer all read this same table, so adding a new
service (or a new channel) means adding one entry and nothing else.

Services are started in listed order and stopped in reverse, with each
one's health checked before its dependents are launched.
"""

import os
import shutil
import sys

from . import config, paths


class Service(object):
    """One managed process."""

    def __init__(self, name, title, cwd, script, config_prefix,
                 python=None, args=(), env=None, health_port_key=None,
                 health_path=None, depends_on=(), optional=False,
                 group="core"):
        self.name = name
        self.title = title
        self.cwd = cwd
        self.script = script
        self.config_prefix = config_prefix
        self._python = python
        self.args = list(args)
        self.env_map = env or {}
        self.health_port_key = health_port_key
        self.health_path = health_path
        self.depends_on = list(depends_on)
        self.optional = optional
        self.group = group

    # ── Config helpers ──────────────────────────────────────────────
    def setting(self, cfg, key, default=None):
        return config.get_key(cfg, "%s.%s" % (self.config_prefix, key), default)

    def is_enabled(self, cfg):
        return bool(self.setting(cfg, "enabled", False))

    def port(self, cfg):
        if not self.health_port_key:
            return None
        return config.get_key(cfg, self.health_port_key)

    # ── Process construction ────────────────────────────────────────
    def python_bin(self, cfg):
        """The interpreter for this service.

        A venv python is used when it exists; otherwise fall back to the
        interpreter running `los`, so a partially-installed system still
        gives a useful error from the service rather than from us.
        """
        if callable(self._python):
            candidate = self._python(cfg)
        else:
            candidate = self._python
        if candidate and os.path.exists(candidate):
            return candidate
        return sys.executable or shutil.which("python3") or "python3"

    def argv(self, cfg):
        return [self.python_bin(cfg), self.script] + self.args

    def environ(self, cfg):
        """Environment for the child: inherited, plus the LOS_* settings
        that let the service read its configuration."""
        env = os.environ.copy()
        env["LOS_MANAGED"] = "1"
        env["LOS_BASE"] = paths.BASE_DIR
        env["LOS_SYS"] = paths.SYS_DIR
        env["LOS_RUN"] = paths.RUN_DIR
        env["LOS_LOG"] = paths.LOG_DIR
        env["LOS_VAR"] = paths.VAR_DIR
        env["LOS_USER"] = cfg.get("user", "")
        env["PYTHONUNBUFFERED"] = "1"
        for var, key in self.env_map.items():
            value = config.get_key(cfg, key)
            if value is None:
                continue
            if isinstance(value, bool):
                value = "1" if value else "0"
            env[var] = str(value)
        return env

    def log_path(self):
        return paths.log_file(self.name)

    def pid_path(self):
        return paths.pid_file(self.name)

    def __repr__(self):
        return "<Service %s>" % self.name


# ── The services ────────────────────────────────────────────────────────
#
# Order matters: backends first, then channels, then the web app, so the
# interface only comes up once the things it talks to are ready.

def _ssl_python(cfg):
    return os.path.join(paths.SSL_VENV, "bin", "python")


def _whatsapp_python(cfg):
    return os.path.join(paths.WHATSAPP_VENV, "bin", "python")


SERVICES = [
    Service(
        name="memory_mcp",
        title="Memory server",
        cwd=os.path.join(paths.SYS_DIR, "memory_mcp"),
        script="memory_server.py",
        config_prefix="services.memory_mcp",
        health_port_key="services.memory_mcp.port",
        health_path="/",
        env={
            "LOS_MEMORY_HOST": "services.memory_mcp.host",
            "LOS_MEMORY_PORT": "services.memory_mcp.port",
        },
    ),
    Service(
        name="action_mcp",
        title="Action MCP server",
        cwd=os.path.join(paths.SYS_DIR, "aicall_mcp"),
        script="action_server.py",
        config_prefix="services.action_mcp",
        health_port_key="services.action_mcp.port",
        health_path="/",
        depends_on=["memory_mcp"],
        env={
            "LOS_ACTION_HOST": "services.action_mcp.host",
            "LOS_ACTION_PORT": "services.action_mcp.port",
            "LOS_MEMORY_PORT": "services.memory_mcp.port",
            "LOS_WHATSAPP_PORT": "channels.whatsapp.port",
        },
    ),
    Service(
        name="whatsapp",
        title="WhatsApp channel",
        cwd=os.path.join(paths.SYS_DIR, "whatsapp_mcp"),
        script="whatsapp_server.py",
        config_prefix="channels.whatsapp",
        python=_whatsapp_python,
        health_port_key="channels.whatsapp.port",
        health_path="/",
        optional=True,
        group="channel",
        env={
            "LOS_WHATSAPP_HOST": "channels.whatsapp.host",
            "LOS_WHATSAPP_PORT": "channels.whatsapp.port",
            "LOS_WHATSAPP_SESSION_ID": "channels.whatsapp.session_id",
            "LOS_ACTION_PORT": "services.action_mcp.port",
            "LOS_MEMORY_PORT": "services.memory_mcp.port",
            # whatsapp_automation.py already reads WHATSAPP_HEADLESS.
            "WHATSAPP_HEADLESS": "channels.whatsapp.headless",
        },
    ),
    Service(
        name="loscron",
        title="Cron daemon",
        cwd=os.path.join(paths.SYS_DIR, "loscron"),
        script="loscron.py",
        config_prefix="services.loscron",
        env={
            "LOS_CRON_INTERVAL": "services.loscron.interval",
        },
    ),
    Service(
        name="ssl_server",
        title="Web app",
        cwd=os.path.join(paths.SYS_DIR, "ssl_server"),
        script="app.py",
        config_prefix="services.ssl_server",
        python=_ssl_python,
        health_port_key="services.ssl_server.port",
        depends_on=["action_mcp"],
        env={
            "LOS_WEB_HOST": "services.ssl_server.host",
            "LOS_WEB_PORT": "services.ssl_server.port",
            "LOS_WEB_SECRET_KEY": "services.ssl_server.secret_key",
            "LOS_WEB_DEBUG": "services.ssl_server.debug",
            "LOS_ACTION_PORT": "services.action_mcp.port",
            "LOS_MEMORY_PORT": "services.memory_mcp.port",
        },
    ),
]

BY_NAME = dict((s.name, s) for s in SERVICES)


def get(name):
    return BY_NAME.get(name)


def enabled_services(cfg):
    """The services that should be running, in dependency order."""
    return [s for s in SERVICES if s.is_enabled(cfg)]


def resolve(names, cfg=None):
    """Turn CLI service names into Service objects.

    Accepts service names and the group aliases 'core', 'channel' and
    'all'.  Raises KeyError with a helpful message on an unknown name.
    """
    if not names:
        return list(SERVICES)
    picked = []
    for name in names:
        if name == "all":
            picked.extend(SERVICES)
        elif name in ("core", "channel", "channels"):
            group = "channel" if name.startswith("channel") else "core"
            picked.extend([s for s in SERVICES if s.group == group])
        elif name in BY_NAME:
            picked.append(BY_NAME[name])
        else:
            raise KeyError(
                "unknown service %r (known: %s)"
                % (name, ", ".join(sorted(BY_NAME)))
            )
    # De-duplicate while preserving registry order.
    return [s for s in SERVICES if s in picked]
