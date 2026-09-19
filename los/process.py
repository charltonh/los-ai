"""Process and port utilities shared by the gateway and the CLI.

Kept separate from the supervisor so that `los status` and `los doctor`
can inspect a running system without importing the supervisor loop.
"""

import errno
import os
import signal
import socket
import time

from . import paths


# ── PID files ───────────────────────────────────────────────────────────

def read_pid(path):
    """Return the pid recorded in path, or None if absent/unreadable."""
    try:
        with open(path, "r") as fh:
            text = fh.read().strip()
    except (OSError, IOError):
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def write_pid(path, pid=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("%d\n" % (pid if pid is not None else os.getpid()))
    os.replace(tmp, path)


def remove_pid(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True   # exists, owned by someone else
        return False
    return True


def process_cmdline(pid):
    """The command line of a pid, from /proc — '' when unavailable."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except (OSError, IOError):
        return ""
    return " ".join(p.decode("utf-8", "replace") for p in raw.split(b"\0") if p)


def process_start_time(pid):
    """Epoch seconds when the process started, or None."""
    try:
        st = os.stat("/proc/%d" % pid)
    except OSError:
        return None
    return st.st_ctime


def looks_like(pid, script):
    """True when pid's command line mentions script.

    Checked before adopting or killing anything recorded in a stale pid
    file, so we never signal a pid that has been recycled.
    """
    if not pid_alive(pid):
        return False
    return script in process_cmdline(pid)


# ── Signals ─────────────────────────────────────────────────────────────

def terminate(pid, timeout=10, script=None):
    """SIGTERM, wait, then SIGKILL.  Returns True if the process is gone."""
    if not pid_alive(pid):
        return True
    if script and not looks_like(pid, script):
        return True     # pid was recycled; not ours to kill
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not pid_alive(pid)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.3)
    return not pid_alive(pid)


# ── Ports ───────────────────────────────────────────────────────────────

def port_open(host, port, timeout=0.5):
    """True when something is accepting connections on host:port."""
    target = "127.0.0.1" if host in ("0.0.0.0", "", None) else host
    try:
        with socket.create_connection((target, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def port_free(host, port):
    """True when we could bind host:port right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host if host not in ("", None) else "0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def port_owner(port):
    """The pid listening on a TCP port, or None.

    Reads /proc rather than shelling out to lsof/ss, which may not be
    installed.  Best-effort: returns None if anything is unexpected.
    """
    inodes = set()
    for proto in ("tcp", "tcp6"):
        try:
            with open("/proc/net/%s" % proto, "r") as fh:
                next(fh, None)
                for line in fh:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != "0A":  # 0A = LISTEN
                        continue
                    try:
                        local_port = int(fields[1].split(":")[1], 16)
                    except (IndexError, ValueError):
                        continue
                    if local_port == int(port):
                        inodes.add(fields[9])
        except (OSError, IOError):
            continue
    if not inodes:
        return None

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % entry
        try:
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    return int(entry)
        except OSError:
            continue
    return None


def wait_for_port(host, port, timeout=30, is_alive=None, interval=0.25):
    """Poll until host:port accepts connections.

    is_alive, when given, is called each pass; if it returns False the
    wait aborts early — a child that has already died will never listen.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_alive is not None and not is_alive():
            return False
        if port_open(host, port):
            return True
        time.sleep(interval)
    return False


# ── Single-instance lock ────────────────────────────────────────────────

class Lock(object):
    """A pid-file lock for the gateway, so two cannot run at once."""

    def __init__(self, path=None):
        self.path = path or paths.GATEWAY_LOCK
        self.acquired = False

    def owner(self):
        pid = read_pid(self.path)
        if pid and pid_alive(pid):
            return pid
        return None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if self.owner():
            return False
        # Stale lock (or none): take it.
        write_pid(self.path)
        self.acquired = True
        return True

    def release(self):
        if self.acquired:
            remove_pid(self.path)
            self.acquired = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False
