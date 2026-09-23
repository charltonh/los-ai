"""Installation reporting.

Sends a small status record to the LOS service so releases can be
tracked against real deployments.  Records carry an anonymous install id
and never include paths, entity names, phone numbers or prompt content.

One identity is included: this machine's hostname, sent as "hostname" and
accepted by the intake as such.  It is the only part of "who is this" that
cannot be worked out at the far end — the address a record arrives from is
read there from the connection, where nothing here can influence it, and a
reverse lookup of that address names the network a record came from, never
the machine it came from.

Everything here is best-effort and runs on a background thread: any
failure is swallowed, and records that cannot be delivered are queued in
var/ and folded into the next successful send.

The destination and the heartbeat interval are fixed in code below.  There
is deliberately nothing to configure and nothing in config.json: a text
file must not be able to redirect an installation's traffic somewhere
else, and there is no part of this worth tuning.
"""

import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__, paths

# Where records go, and how often the gateway checks in.  Fixed in code on
# purpose — see the module docstring.
_ENDPOINT = "https://los.dynet.com/api/check.php"
_INTERVAL = 86400

_QUEUE_FILE = os.path.join(paths.VAR_DIR, "rq.dat")
_QUEUE_MAX = 100
_TIMEOUT = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: records must not be bounced elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _distro():
    """A coarse OS label — the distro family, not the release string."""
    try:
        with open("/etc/os-release", "r") as fh:
            for line in fh:
                if line.startswith("ID="):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except (OSError, IOError):
        pass
    return platform.system().lower()


def _record(cfg, event, extra=None):
    """Build one status record."""
    data = {
        "id": cfg.get("install_id", ""),
        "v": __version__,
        "event": event,
        "ts": int(time.time()),
        "os": _distro(),
        "arch": platform.machine(),
        "py": "%d.%d" % sys.version_info[:2],
    }
    # The one field the far end cannot find for itself: see the docstring.
    try:
        data["hostname"] = socket.gethostname() or ""
    except Exception:
        pass
    if extra:
        data.update(extra)
    return data


def _summary(cfg):
    """Which pieces are switched on — names only, never values."""
    from . import registry
    enabled = []
    for svc in registry.SERVICES:
        if svc.is_enabled(cfg):
            enabled.append(svc.name)
    users = 0
    try:
        for name in os.listdir(paths.BASE_DIR):
            d = os.path.join(paths.BASE_DIR, name)
            if name != "sys" and os.path.isdir(d) and \
                    os.path.exists(os.path.join(d, "identity")):
                users += 1
    except OSError:
        pass
    return {"svc": enabled, "users": users}


# ── Queue ───────────────────────────────────────────────────────────────

def _queue_load():
    try:
        with open(_QUEUE_FILE, "r") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, IOError, ValueError):
        return []


def _queue_save(items):
    try:
        os.makedirs(paths.VAR_DIR, exist_ok=True)
        tmp = _QUEUE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(items[-_QUEUE_MAX:], fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _QUEUE_FILE)
    except (OSError, IOError):
        pass


def _queue_add(item):
    items = _queue_load()
    items.append(item)
    _queue_save(items)


# ── Delivery ────────────────────────────────────────────────────────────

def _post(endpoint, batch):
    """POST a batch; returns the decoded response dict, or None."""
    body = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "los/%s" % __version__,
        },
        method="POST",
    )
    # _OPENER refuses redirects, so a 30x can never bounce records to a
    # third party: it raises instead, which _deliver treats as a failure.
    with _OPENER.open(req, timeout=_TIMEOUT) as resp:
        if resp.status not in (200, 201, 202, 204):
            raise urllib.error.HTTPError(
                endpoint, resp.status, "unexpected status", resp.headers, None
            )
        payload = resp.read(65536)
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return {}


def _deliver(cfg, item):
    """Send item plus anything queued.  Returns the server response."""
    endpoint = _ENDPOINT
    if not endpoint.startswith("https://"):
        return None
    batch = _queue_load()
    batch.append(item)
    try:
        response = _post(endpoint, batch)
    except Exception:
        _queue_add(item)
        return None
    _queue_save([])
    return response


def send(cfg, event, extra=None, blocking=False):
    """Record an event.  Never raises, never blocks the caller by default."""
    try:
        item = _record(cfg, event, extra)
    except Exception:
        return None

    if blocking:
        try:
            return _deliver(cfg, item)
        except Exception:
            return None

    def _worker():
        try:
            _deliver(cfg, item)
        except Exception:
            pass

    try:
        thread = threading.Thread(target=_worker, name="los-report", daemon=True)
        thread.start()
    except Exception:
        pass
    return None


def startup(cfg):
    """Called when the gateway comes up."""
    try:
        return send(cfg, "start", _summary(cfg))
    except Exception:
        return None


def heartbeat(cfg, uptime):
    try:
        extra = _summary(cfg)
        extra["up"] = int(uptime)
        return send(cfg, "beat", extra)
    except Exception:
        return None


def shutdown(cfg, uptime):
    try:
        return send(cfg, "stop", {"up": int(uptime)}, blocking=True)
    except Exception:
        return None


def install(cfg, stage="install", blocking=True):
    try:
        return send(cfg, stage, _summary(cfg), blocking=blocking)
    except Exception:
        return None


def updated(cfg, old, new, ok=True):
    try:
        return send(cfg, "update",
                    {"from": old, "to": new, "ok": bool(ok)}, blocking=True)
    except Exception:
        return None


def interval():
    """How often the gateway checks in, in seconds."""
    return _INTERVAL
