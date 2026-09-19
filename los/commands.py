"""The non-interactive `los` subcommands.

status / start / stop / restart / logs / doctor, plus the shared helpers
they use to describe a running system.
"""

import os
import shutil
import sys
import time

from . import config, paths, process, registry, supervisor, ui


# ── Inspection ──────────────────────────────────────────────────────────

def service_state(svc, cfg):
    """Work out what a service is doing right now, from the outside.

    Returns (state, pid, uptime_seconds, note).  Used by `los status`,
    which may run when no gateway is attached, so everything is derived
    from pid files and ports rather than from in-memory state.
    """
    pid = process.read_pid(svc.pid_path())
    alive = bool(pid and process.looks_like(pid, svc.script))
    port = svc.port(cfg)
    host = svc.setting(cfg, "host", "127.0.0.1")
    listening = bool(port and process.port_open(host, port))

    if not svc.is_enabled(cfg):
        # Disabled but still running: worth flagging.
        if alive:
            return ("stray", pid, _uptime(pid), "disabled but running")
        return ("disabled", None, 0, "")

    if alive:
        note = "" if (not port or listening) else "not answering"
        return ("running", pid, _uptime(pid), note)

    if listening:
        owner = process.port_owner(port)
        return ("foreign", owner, _uptime(owner) if owner else 0,
                "port %s held by another process" % port)

    return ("stopped", None, 0, "")


def _uptime(pid):
    if not pid:
        return 0
    started = process.process_start_time(pid)
    return time.time() - started if started else 0


_STATE_STYLE = {
    "running": ("green",),
    "stopped": ("grey",),
    "disabled": ("grey",),
    "stray": ("yellow",),
    "foreign": ("yellow",),
    "failed": ("bright_red",),
}


def cmd_status(args):
    cfg = config.load(strict=False)
    services = registry.resolve(args.services)

    gateway_pid = process.read_pid(paths.GATEWAY_PID)
    gateway_up = bool(gateway_pid and process.pid_alive(gateway_pid))

    rows, styles = [], []
    for svc in services:
        state, pid, up, note = service_state(svc, cfg)
        port = svc.port(cfg)
        rows.append([
            svc.name,
            state,
            str(pid) if pid else "—",
            supervisor.human_uptime(up),
            str(port) if port else "—",
            note or "",
        ])
        styles.append(_STATE_STYLE.get(state, ()))

    if gateway_up:
        ui.ok("gateway running (pid %d)" % gateway_pid)
    else:
        ui.info("gateway not attached (services listed by pid file)")
    print()
    ui.table(["SERVICE", "STATE", "PID", "UPTIME", "PORT", "NOTE"], rows, styles)

    problems = config.validate(cfg)
    if problems:
        print()
        ui.warn("configuration issues:")
        for problem in problems:
            print("   %s" % problem)


# ── start / stop / restart ──────────────────────────────────────────────

def cmd_start(args):
    """Start services without staying attached.

    Useful for bringing one service back after a fix; `los gateway` is
    still the way to run the whole system.
    """
    cfg = config.load()
    services = registry.resolve(args.services)
    gw = supervisor.Gateway(cfg, services=services)

    started = failed = skipped = 0
    for child in gw.children:
        if child.state == "disabled":
            ui.info("%s is disabled in config.json" % child.service.title)
            skipped += 1
            continue
        state, pid, _, _ = service_state(child.service, cfg)
        if state == "running":
            ui.info("%s already running (pid %d)" % (child.service.title, pid))
            skipped += 1
            continue
        if gw.start_child(child):
            started += 1
            # Detach: the child outlives this process, supervised only by
            # its pid file until a gateway picks it up.
            child.popen = None
        else:
            failed += 1

    if failed:
        ui.error("%d service(s) failed to start" % failed)
        return 1
    if started:
        ui.ok("started %d service(s)" % started)
    elif skipped:
        ui.info("nothing to do")
    return 0


def cmd_stop(args):
    cfg = config.load(strict=False)
    services = registry.resolve(args.services)

    # Stopping everything means the gateway goes too.
    if not args.services or "all" in args.services:
        gateway_pid = process.read_pid(paths.GATEWAY_PID)
        if gateway_pid and process.pid_alive(gateway_pid):
            ui.step("stopping gateway (pid %d)" % gateway_pid)
            process.terminate(gateway_pid,
                              timeout=int(config.get_key(
                                  cfg, "gateway.stop_timeout", 10) or 10))
            process.remove_pid(paths.GATEWAY_PID)
            process.remove_pid(paths.GATEWAY_LOCK)
            ui.ok("gateway stopped")
            return 0

    stopped = 0
    for svc in reversed(services):
        pid = process.read_pid(svc.pid_path())
        if pid and process.looks_like(pid, svc.script):
            ui.step("stopping %s (pid %d)" % (svc.title, pid))
            timeout = int(config.get_key(cfg, "gateway.stop_timeout", 10) or 10)
            process.terminate(pid, timeout=timeout, script=svc.script)
            process.remove_pid(svc.pid_path())
            stopped += 1
        else:
            process.remove_pid(svc.pid_path())

    if stopped:
        ui.ok("stopped %d service(s)" % stopped)
    else:
        ui.info("nothing was running")
    return 0


def cmd_restart(args):
    rc = cmd_stop(args)
    time.sleep(1)
    return cmd_start(args) or rc


# ── logs ────────────────────────────────────────────────────────────────

def cmd_logs(args):
    cfg = config.load(strict=False)

    # aicall.py isn't a supervised service (it's spawned per-call by the web
    # app, the WhatsApp channel and loscron), but its log lives in the same
    # log/ directory now and people reasonably expect `los logs aicall` and
    # `los logs` (no args) to include it.
    requested = list(args.services)
    want_aicall = "aicall" in requested or not requested or "all" in requested
    service_names = [n for n in requested if n != "aicall"]
    if requested and not service_names:
        # Everything requested was "aicall" — no supervised services wanted.
        services = []
    else:
        services = registry.resolve(service_names)

    files = []
    show_all = not requested or "all" in requested
    if show_all:
        for name in ("aicall", "gateway"):
            path = paths.log_file(name)
            if os.path.exists(path):
                files.append((name, path))
    elif want_aicall:
        path = paths.log_file("aicall")
        if os.path.exists(path):
            files.append(("aicall", path))

    for svc in services:
        path = svc.log_path()
        if os.path.exists(path):
            files.append((svc.name, path))

    if not files:
        ui.info("no log files yet in %s" % paths.LOG_DIR)
        return 0

    if args.follow:
        return _follow(files, args.lines)

    for name, path in files:
        if len(files) > 1:
            ui.heading("%s  (%s)" % (name, path))
        _tail(path, args.lines)
    return 0


def _tail(path, lines):
    """Print the last N lines of a file without reading all of it."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        text = data.decode("utf-8", "replace").splitlines()
        for line in text[-lines:]:
            print(line)
    except OSError as exc:
        ui.error("cannot read %s: %s" % (path, exc))


def _follow(files, lines):
    """tail -f across one or more log files."""
    handles = {}
    for name, path in files:
        _tail(path, lines) if len(files) == 1 else None
        try:
            fh = open(path, "r", errors="replace")
            fh.seek(0, os.SEEK_END)
            handles[name] = fh
        except OSError:
            continue
    if not handles:
        return 1

    prefixed = len(handles) > 1
    ui.info("following %d log(s) — Ctrl-C to stop" % len(handles))
    try:
        while True:
            idle = True
            for name, fh in handles.items():
                for line in fh:
                    idle = False
                    if prefixed:
                        print("%s %s" % (ui.color("%-11s" % name, "cyan"),
                                         line.rstrip()))
                    else:
                        print(line.rstrip())
            if idle:
                time.sleep(0.4)
    except KeyboardInterrupt:
        print()
    finally:
        for fh in handles.values():
            fh.close()
    return 0


# ── doctor ──────────────────────────────────────────────────────────────

class Check(object):
    """One diagnostic result."""

    def __init__(self, label, ok, detail="", fix=""):
        self.label = label
        self.ok = ok
        self.detail = detail
        self.fix = fix


def run_checks(cfg=None):
    """Every read-only check `los doctor` performs.

    Shared with `los install`, which runs the same checks before doing
    any work, so 'why won't it start' and 'will it install' agree.
    """
    checks = []
    cfg = cfg if cfg is not None else config.load(strict=False)

    # Platform
    checks.append(Check(
        "linux", sys.platform.startswith("linux"),
        sys.platform,
        "LOS runs on Linux; on Windows or macOS use a Linux VM."))

    # Python
    ok_py = sys.version_info >= (3, 8)
    checks.append(Check(
        "python >= 3.8", ok_py,
        "%d.%d.%d" % sys.version_info[:3]))

    # /los must exist; being able to write to it matters for `los install`
    # and for creating new users, not for running an installed system, so a
    # read-only root is reported but not treated as a failure here.
    base_exists = os.path.isdir(paths.BASE_DIR)
    checks.append(Check(
        "%s exists" % paths.BASE_DIR, base_exists,
        paths.BASE_DIR if base_exists else "missing",
        "sudo mkdir -p %s && sudo chown %s %s"
        % (paths.BASE_DIR, _whoami(), paths.BASE_DIR)))

    if base_exists and not os.access(paths.BASE_DIR, os.W_OK):
        # Writable enough to run; not writable enough to add a user.
        checks.append(Check(
            "%s writable" % paths.BASE_DIR, True,
            "read-only for %s — fine to run, needed to add users"
            % _whoami()))

    # The software directory must be writable to update in place.
    sys_ok = os.access(paths.SYS_DIR, os.W_OK)
    checks.append(Check(
        "%s writable" % paths.SYS_DIR, sys_ok,
        paths.SYS_DIR if sys_ok else "not writable by %s" % _whoami(),
        "sudo chown -R %s %s" % (_whoami(), paths.SYS_DIR)))

    # Config
    if config.exists():
        try:
            config.load()
            checks.append(Check("config.json", True, paths.CONFIG_FILE))
        except config.ConfigError as exc:
            checks.append(Check("config.json", False, str(exc),
                                "los config validate"))
    else:
        checks.append(Check("config.json", False, "not created yet",
                            "los install"))

    # Runtime directories
    for label, path in (("run/", paths.RUN_DIR), ("log/", paths.LOG_DIR),
                        ("var/", paths.VAR_DIR)):
        exists = os.path.isdir(path)
        checks.append(Check(label, exists, path,
                            "" if exists else "los install"))

    # External tools
    for tool, needed in (("git", True), ("openssl", True)):
        found = shutil.which(tool)
        checks.append(Check(tool, bool(found) or not needed,
                            found or "not found",
                            "install %s" % tool))

    # TLS material for the web app
    if config.get_key(cfg, "services.ssl_server.enabled"):
        have_cert = os.path.exists(paths.SSL_CERT) and os.path.exists(paths.SSL_KEY)
        checks.append(Check("tls certificate", have_cert,
                            paths.SSL_CERT if have_cert else "missing",
                            "los install  (generates a self-signed cert)"))

    # Interpreters and ports for each enabled service
    for svc in registry.SERVICES:
        if not svc.is_enabled(cfg):
            continue
        script = os.path.join(svc.cwd, svc.script)
        checks.append(Check("%s script" % svc.name, os.path.exists(script),
                            script))
        python = svc.python_bin(cfg)
        checks.append(Check("%s python" % svc.name, os.path.exists(python),
                            python,
                            "los install  (rebuilds the virtualenv)"))
        port = svc.port(cfg)
        if port:
            state, _, _, _ = service_state(svc, cfg)
            free = process.port_free(svc.setting(cfg, "host", "127.0.0.1"), port)
            if free:
                checks.append(Check("%s port %s" % (svc.name, port), True,
                                    "free"))
            else:
                # A port held by this very service is fine — it just means
                # the service is up, whether or not the gateway started it.
                owner = process.port_owner(port)
                ours = state in ("running", "foreign") and \
                    bool(owner) and process.looks_like(owner, svc.script)
                checks.append(Check(
                    "%s port %s" % (svc.name, port),
                    ours,
                    "in use by %s (pid %s)"
                    % (svc.name if ours else "another process", owner),
                    "" if ours else
                    "stop it, or change the port with `los config`"))

    # LOS user
    user = cfg.get("user") or ""
    user_ok = bool(user) and os.path.isdir(paths.user_dir(user))
    checks.append(Check("los user", user_ok,
                        paths.user_dir(user) if user else "not set",
                        "%s/los-user-manage create <name>" % paths.BIN_DIR))

    return checks


def _whoami():
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "your-user")


def cmd_doctor(args):
    ui.heading("LOS diagnostics")
    checks = run_checks()
    failed = 0
    for check in checks:
        if check.ok:
            print("  %s %-22s %s" % (ui.color("✓", "green"), check.label,
                                     ui.color(check.detail, "grey")))
        else:
            failed += 1
            print("  %s %-22s %s" % (ui.color("✗", "bright_red"), check.label,
                                     check.detail))
            if check.fix:
                print("      %s %s" % (ui.color("fix:", "grey"), check.fix))

    print()
    if failed:
        ui.error("%d of %d checks failed" % (failed, len(checks)))
        return 1
    ui.ok("all %d checks passed" % len(checks))
    return 0

    return 0
