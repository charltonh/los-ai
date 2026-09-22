"""Filesystem layout for a LOS installation.

Everything the system writes at runtime lives under /los/sys in its own
directory, so the rest of the git working tree is code only and can be
hard-reset by `los update` without touching state.

    /los/sys/            git working tree (code)
    /los/sys/config.json configuration
    /los/sys/run/        pid files
    /los/sys/log/        service logs
    /los/sys/var/        sessions, ids, spools
    /los/<user>/         per-user agenda (never touched by update)

Each location can be overridden with an environment variable, which makes
it possible to run a second instance for testing.
"""

import os

# The directory containing the `los` package, i.e. /los/sys
SYS_DIR = os.environ.get(
    "LOS_SYS",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

# The LOS root, i.e. /los — parent of sys/ and of the user entities.
BASE_DIR = os.environ.get("LOS_BASE", os.path.dirname(SYS_DIR))

CONFIG_FILE = os.environ.get("LOS_CONFIG", os.path.join(SYS_DIR, "config.json"))
RUN_DIR = os.environ.get("LOS_RUN", os.path.join(SYS_DIR, "run"))
LOG_DIR = os.environ.get("LOS_LOG", os.path.join(SYS_DIR, "log"))
VAR_DIR = os.environ.get("LOS_VAR", os.path.join(SYS_DIR, "var"))
BIN_DIR = os.path.join(SYS_DIR, "bin")

# Templates.  `los install` and `los-user-manage create` copy them into a new
# agenda: skeleton/ for every new entity, and newuser/ as well when the
# *account* is created — a starter .config plus the ai/ directory of
# aiconfigs, prompts and memory.  Sub-entities get skeleton/ only.
SKELETON_DIR = os.path.join(SYS_DIR, "skeleton")
NEWUSER_DIR = os.path.join(SYS_DIR, "newuser")

# Runtime files
GATEWAY_PID = os.path.join(RUN_DIR, "gateway.pid")
GATEWAY_LOCK = os.path.join(RUN_DIR, "gateway.lock")
STATE_FILE = os.path.join(VAR_DIR, "state.json")

# Virtualenvs, rebuilt from requirements.txt by `los install`.
SSL_VENV = os.path.join(SYS_DIR, "ssl_venv")
WHATSAPP_VENV = os.path.join(SYS_DIR, "whatsapp_mcp", "whatsapp_venv")

# Where the web app keeps its TLS material.
SSL_CERT_DIR = os.path.join(SYS_DIR, "ssl_server", "ssl")
SSL_CERT = os.path.join(SSL_CERT_DIR, "cert.pem")
SSL_KEY = os.path.join(SSL_CERT_DIR, "key.pem")

RUNTIME_DIRS = (RUN_DIR, LOG_DIR, VAR_DIR)


def ensure_dirs():
    """Create the runtime directories if they are missing."""
    for d in RUNTIME_DIRS:
        os.makedirs(d, exist_ok=True)


def pid_file(name):
    return os.path.join(RUN_DIR, "%s.pid" % name)


def log_file(name):
    return os.path.join(LOG_DIR, "%s.log" % name)


def user_dir(username):
    return os.path.join(BASE_DIR, username)
