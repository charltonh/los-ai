"""Command-line entry point for `los`."""

import argparse
import os
import sys

from . import __version__, config, paths, process, registry, ui


def _service_help():
    names = [s.name for s in registry.SERVICES]
    return ("service names (%s), or a group: core, channels, all"
            % ", ".join(names))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="los",
        description="LOS — Layered Organization System.",
        epilog="Run `los gateway` to start the system, `los config` to "
               "configure it, and `los status` to see what is running.",
    )
    parser.add_argument("-V", "--version", action="version",
                        version="los %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # gateway
    p = sub.add_parser("gateway", help="run every configured service")
    p.add_argument("-d", "--daemon", action="store_true",
                   help="detach and run in the background")
    p.add_argument("-f", "--force", action="store_true",
                   help="stop anything already running first")

    # start / stop / restart
    for name, helptext in (
        ("start", "start services and detach"),
        ("stop", "stop services"),
        ("restart", "restart services"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("services", nargs="*", metavar="service",
                       help=_service_help())

    # status
    p = sub.add_parser("status", help="show what is running")
    p.add_argument("services", nargs="*", metavar="service",
                   help=_service_help())

    # logs
    p = sub.add_parser("logs", help="show service logs")
    p.add_argument("services", nargs="*", metavar="service",
                   help=_service_help())
    p.add_argument("-f", "--follow", action="store_true", help="keep watching")
    p.add_argument("-n", "--lines", type=int, default=40,
                   help="lines to show (default 40)")

    # config
    p = sub.add_parser("config", aliases=["configure"],
                       help="view and change the configuration")
    csub = p.add_subparsers(dest="action", metavar="<action>")
    g = csub.add_parser("get", help="print one setting")
    g.add_argument("key")
    s = csub.add_parser("set", help="change one setting")
    s.add_argument("key")
    s.add_argument("value")
    u = csub.add_parser("unset", help="remove one setting")
    u.add_argument("key")
    l = csub.add_parser("list", help="print every setting")
    l.add_argument("--json", action="store_true", help="print raw JSON")
    csub.add_parser("validate", help="check the configuration")
    csub.add_parser("edit", help="open config.json in $EDITOR")
    csub.add_parser("path", help="print the path to config.json")

    # doctor
    sub.add_parser("doctor", help="check this installation")

    # install
    p = sub.add_parser("install", help="set up or repair this installation")
    p.add_argument("--channel", action="append", default=[], metavar="NAME",
                   help="also install an optional channel (e.g. whatsapp)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="accept defaults without prompting")
    p.add_argument("--repair", action="store_true",
                   help="re-run setup over an existing installation")

    # update
    p = sub.add_parser("update", help="fetch and apply the latest release")
    p.add_argument("--check", action="store_true",
                   help="report whether an update exists, then exit")
    p.add_argument("--to", metavar="VERSION", help="install a specific tag")
    p.add_argument("-y", "--yes", action="store_true",
                   help="do not ask for confirmation")
    p.add_argument("--force", action="store_true",
                   help="overwrite local changes without asking")

    sub.add_parser("rollback", help="return to the previous version")

    return parser



# ── gateway ─────────────────────────────────────────────────────────────

def cmd_gateway(args):
    """Run every configured service in the foreground."""
    from . import supervisor

    cfg = config.load()
    problems = config.validate(cfg)
    if problems:
        ui.error("configuration problems:")
        for problem in problems:
            print("   %s" % problem)
        print()
        ui.info("run `los config` to fix them")
        return 1

    paths.ensure_dirs()

    # One gateway at a time.
    lock = process.Lock()
    owner = lock.owner()
    if owner:
        ui.error("a gateway is already running (pid %d)" % owner)
        ui.info("use `los status`, or `los stop` to shut it down")
        return 1

    gw = supervisor.Gateway(cfg)

    # Deal with anything left behind by a previous run.
    leftovers = gw.adopt_or_clear()
    if leftovers:
        ui.warn("these services are already running outside the gateway:")
        for svc, pid in leftovers:
            print("   %-12s pid %d" % (svc.name, pid))
        if args.force:
            for svc, pid in leftovers:
                ui.step("stopping %s (pid %d)" % (svc.title, pid))
                process.terminate(pid, script=svc.script)
                process.remove_pid(svc.pid_path())
        else:
            print()
            ui.info("run `los gateway --force` to stop them and take over, "
                    "or `los stop` first")
            return 1

    if args.daemon:
        return _daemonize(gw)

    if not lock.acquire():
        ui.error("could not acquire %s" % lock.path)
        return 1
    process.write_pid(paths.GATEWAY_PID)
    try:
        return gw.run()
    finally:
        process.remove_pid(paths.GATEWAY_PID)
        lock.release()


def _daemonize(gw):
    """Fork into the background, logging to log/gateway.log."""
    from . import logrotate

    log_path = paths.log_file("gateway")

    # Rotate before we start writing, same as any other service's log.
    rotate, max_bytes, keep = logrotate.settings(gw.cfg)
    logrotate.numbered(log_path, max_bytes=max_bytes, keep=keep, rotate=rotate)

    try:
        pid = os.fork()
    except OSError as exc:
        ui.error("cannot fork: %s" % exc)
        return 1
    if pid > 0:
        ui.ok("gateway starting in the background")
        ui.info("logs: %s" % log_path)
        return 0

    # Child: detach from the terminal.
    os.setsid()
    with open(os.devnull, "r") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    out = open(log_path, "a", buffering=1)
    os.dup2(out.fileno(), sys.stdout.fileno())
    os.dup2(out.fileno(), sys.stderr.fileno())

    # The gateway's own log outlives any single service and is written to
    # via the redirected stdout/stderr above, so it needs the same
    # copy-truncate rotation as a running service's log, checked
    # periodically from within the supervision loop.
    gw.own_log_path = log_path

    lock = process.Lock()
    lock.acquire()
    process.write_pid(paths.GATEWAY_PID)
    try:
        rc = gw.run()
    finally:
        process.remove_pid(paths.GATEWAY_PID)
        lock.release()
    os._exit(rc)


# ── Dispatch ────────────────────────────────────────────────────────────

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        print()
        _quick_status()
        return 0

    from . import commands

    handlers = {
        "gateway": cmd_gateway,
        "start": commands.cmd_start,
        "stop": commands.cmd_stop,
        "restart": commands.cmd_restart,
        "status": commands.cmd_status,
        "logs": commands.cmd_logs,
        "doctor": commands.cmd_doctor,
    }

    try:
        if args.command in ("config", "configure"):
            from . import configure
            return configure.run(args)
        if args.command == "install":
            from . import install
            return install.run(args)
        if args.command == "update":
            from . import update
            return update.run(args)
        if args.command == "rollback":
            from . import update
            return update.rollback(args)

        handler = handlers.get(args.command)
        if handler is None:
            parser.error("unknown command %r" % args.command)
        return handler(args)

    except config.ConfigError as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0


def _quick_status():
    """A one-line hint under `los` with no arguments."""
    if not config.exists():
        ui.info("not installed yet — run `los install`")
        return
    pid = process.read_pid(paths.GATEWAY_PID)
    if pid and process.pid_alive(pid):
        ui.ok("gateway running (pid %d) — `los status` for detail" % pid)
    else:
        ui.info("gateway not running — `los gateway` to start")


if __name__ == "__main__":
    sys.exit(main())
