"""The gateway: start, supervise and stop every LOS service.

`los gateway` runs this in the foreground.  It brings services up in
dependency order, waits for each to answer on its port before starting
the ones that need it, restarts anything that dies unexpectedly (with
backoff), and shuts everything down cleanly on Ctrl-C.

There is no systemd or OpenRC dependency: the supervisor is the init
system for LOS, which keeps it identical on every distro.
"""

import os
import signal
import subprocess
import threading
import time

from . import config, logrotate, paths, process, registry, report, ui


class Child(object):
    """Runtime state for one supervised service."""

    def __init__(self, service):
        self.service = service
        self.popen = None
        self.log_fh = None
        self.started_at = None
        self.restarts = 0
        self.restart_times = []
        self.next_try = 0
        self.state = "stopped"     # stopped|starting|running|failed|disabled
        self.last_error = ""

    @property
    def name(self):
        return self.service.name

    @property
    def pid(self):
        return self.popen.pid if self.popen else None

    def alive(self):
        return self.popen is not None and self.popen.poll() is None

    def uptime(self):
        if not self.started_at or not self.alive():
            return 0
        return time.time() - self.started_at


def human_uptime(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


class Gateway(object):
    """Supervises the configured set of services."""

    def __init__(self, cfg, services=None, verbose=True):
        self.cfg = cfg
        self.verbose = verbose
        chosen = services if services is not None else registry.SERVICES
        self.children = []
        for svc in chosen:
            child = Child(svc)
            if not svc.is_enabled(cfg):
                child.state = "disabled"
            self.children.append(child)
        self.by_name = dict((c.name, c) for c in self.children)
        self.started_at = None
        self.stopping = False
        self._last_beat = 0
        self._last_own_log_check = 0
        # Set by _daemonize() in daemon mode; the gateway's own log (stdout/
        # stderr redirected to a file) needs the same rotation as a
        # service's log, checked periodically from the supervision loop.
        self.own_log_path = None

    # ── Settings ────────────────────────────────────────────────────
    def _opt(self, key, default):
        value = config.get_key(self.cfg, "gateway." + key, default)
        return default if value is None else value

    # ── Logging ─────────────────────────────────────────────────────
    def _rotate(self, path):
        """Size-based rotation, checked before each (re)start.

        Uses the shared logging.* settings (rotate/max_bytes/keep) so
        every service log behaves the same way as aicall.log.
        """
        rotate, max_bytes, keep = logrotate.settings(self.cfg)
        logrotate.numbered(path, max_bytes=max_bytes, keep=keep, rotate=rotate)

    # ── Start ───────────────────────────────────────────────────────
    def start_child(self, child, quiet=False):
        """Spawn one service and wait for it to become healthy."""
        svc = child.service
        if child.alive():
            return True

        script = os.path.join(svc.cwd, svc.script)
        if not os.path.exists(script):
            child.state = "failed"
            child.last_error = "missing %s" % script
            ui.error("%s: %s" % (svc.title, child.last_error))
            return False

        # Refuse to start on top of something already holding the port.
        port = svc.port(self.cfg)
        if port:
            host = svc.setting(self.cfg, "host", "127.0.0.1")
            if process.port_open(host, port):
                owner = process.port_owner(port)
                child.state = "failed"
                child.last_error = "port %s in use%s" % (
                    port, " by pid %d" % owner if owner else "")
                ui.error("%s: %s" % (svc.title, child.last_error))
                return False

        log_path = svc.log_path()
        self._rotate(log_path)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        try:
            child.log_fh = open(log_path, "a", buffering=1)
        except OSError as exc:
            child.state = "failed"
            child.last_error = "cannot open log: %s" % exc
            ui.error("%s: %s" % (svc.title, child.last_error))
            return False

        child.log_fh.write(
            "\n=== %s started by los gateway at %s ===\n"
            % (svc.name, time.strftime("%Y-%m-%d %H:%M:%S"))
        )

        child.state = "starting"
        if not quiet:
            ui.step("starting %s%s" % (
                svc.title, " (port %s)" % port if port else ""))

        try:
            child.popen = subprocess.Popen(
                svc.argv(self.cfg),
                cwd=svc.cwd,
                env=svc.environ(self.cfg),
                stdout=child.log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            child.state = "failed"
            child.last_error = str(exc)
            ui.error("%s: %s" % (svc.title, exc))
            return False

        process.write_pid(svc.pid_path(), child.popen.pid)
        child.started_at = time.time()

        if not self._await_health(child, quiet=quiet):
            return False

        child.state = "running"
        if not quiet:
            ui.ok("%s ready (pid %d)" % (svc.title, child.popen.pid))
        return True

    def _await_health(self, child, quiet=False):
        """Wait for a service to answer, or to prove it has died."""
        svc = child.service
        port = svc.port(self.cfg)
        timeout = int(self._opt("start_timeout", 45))

        if not port:
            # No port to probe: a short grace period catches instant exits.
            time.sleep(0.6)
            if not child.alive():
                self._note_death(child)
                return False
            return True

        host = svc.setting(self.cfg, "host", "127.0.0.1")
        ready = process.wait_for_port(
            host, port, timeout=timeout, is_alive=child.alive)
        if ready:
            return True

        if not child.alive():
            self._note_death(child)
        else:
            child.state = "failed"
            child.last_error = "no response on port %s after %ds" % (port, timeout)
            ui.error("%s: %s (see %s)"
                     % (svc.title, child.last_error, svc.log_path()))
        return False

    def _note_death(self, child):
        code = child.popen.returncode if child.popen else None
        child.state = "failed"
        child.last_error = "exited with code %s" % code
        ui.error("%s: %s — see %s"
                 % (child.service.title, child.last_error,
                    child.service.log_path()))


    # ── Orchestration ───────────────────────────────────────────────
    def _deps_ready(self, child):
        """True when every dependency is running (or absent/disabled)."""
        for dep in child.service.depends_on:
            other = self.by_name.get(dep)
            if other is None or other.state == "disabled":
                continue
            if other.state != "running":
                return False
        return True

    def start_all(self, quiet=False):
        """Start every enabled service in dependency order.

        A failing optional service is reported but does not stop the
        rest; a failing required service aborts the sequence.
        """
        failures = []
        for child in self.children:
            if child.state == "disabled":
                continue
            if not self._deps_ready(child):
                child.state = "failed"
                child.last_error = "dependency not running"
                if child.service.optional:
                    ui.warn("%s skipped: %s"
                            % (child.service.title, child.last_error))
                    continue
                ui.error("%s skipped: %s"
                         % (child.service.title, child.last_error))
                failures.append(child)
                break
            if not self.start_child(child, quiet=quiet):
                if child.service.optional:
                    ui.warn("%s did not start; continuing without it"
                            % child.service.title)
                    continue
                failures.append(child)
                break
        return failures

    def stop_child(self, child, quiet=False):
        svc = child.service
        timeout = int(self._opt("stop_timeout", 10))

        if child.alive():
            if not quiet:
                ui.step("stopping %s (pid %d)" % (svc.title, child.popen.pid))
            try:
                # The child has its own session, so signal the whole group
                # to catch anything it spawned (e.g. a browser driver).
                os.killpg(os.getpgid(child.popen.pid), signal.SIGTERM)
            except OSError:
                try:
                    child.popen.terminate()
                except OSError:
                    pass
            try:
                child.popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                ui.warn("%s did not stop in %ds; killing" % (svc.title, timeout))
                try:
                    os.killpg(os.getpgid(child.popen.pid), signal.SIGKILL)
                except OSError:
                    child.popen.kill()
                try:
                    child.popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        else:
            # Not ours, but a pid file may point at a survivor of a crash.
            stale = process.read_pid(svc.pid_path())
            if stale and process.looks_like(stale, svc.script):
                if not quiet:
                    ui.step("stopping orphaned %s (pid %d)" % (svc.title, stale))
                process.terminate(stale, timeout=timeout, script=svc.script)

        if child.log_fh:
            try:
                child.log_fh.close()
            except OSError:
                pass
            child.log_fh = None
        process.remove_pid(svc.pid_path())
        child.popen = None
        child.started_at = None
        if child.state != "disabled":
            child.state = "stopped"
        return True

    def stop_all(self, quiet=False):
        """Stop in reverse dependency order."""
        self.stopping = True
        for child in reversed(self.children):
            if child.state == "disabled" and not child.alive():
                continue
            self.stop_child(child, quiet=quiet)
        return True

    # ── Adoption of leftovers ───────────────────────────────────────
    def adopt_or_clear(self):
        """Deal with pid files left by a previous run.

        Returns a list of (service, pid) still running, which the caller
        can offer to stop.  Stale files are simply removed.
        """
        leftovers = []
        for child in self.children:
            svc = child.service
            pid = process.read_pid(svc.pid_path())
            if pid is None:
                continue
            if process.looks_like(pid, svc.script):
                leftovers.append((svc, pid))
            else:
                process.remove_pid(svc.pid_path())
        return leftovers

    # ── Restart policy ──────────────────────────────────────────────
    def _may_restart(self, child):
        """Rate-limit restarts so a broken service cannot spin forever."""
        if not self._opt("restart_on_failure", True):
            return False
        window = int(self._opt("restart_window", 600))
        limit = int(self._opt("max_restarts", 5))
        now = time.time()
        child.restart_times = [t for t in child.restart_times if now - t < window]
        return len(child.restart_times) < limit

    def _backoff(self, child):
        """1, 2, 4, 8 … capped at 60 seconds."""
        return min(60, 2 ** max(0, len(child.restart_times) - 1))

    def _handle_exit(self, child):
        """A supervised child has gone away unexpectedly."""
        code = child.popen.returncode if child.popen else None
        svc = child.service
        process.remove_pid(svc.pid_path())
        if child.log_fh:
            try:
                child.log_fh.close()
            except OSError:
                pass
            child.log_fh = None
        child.popen = None
        child.started_at = None

        ui.warn("%s exited (code %s)" % (svc.title, code))

        if not self._may_restart(child):
            child.state = "failed"
            child.last_error = "exited with code %s; restart limit reached" % code
            ui.error("%s will not be restarted — fix it and run "
                     "`los restart %s`" % (svc.title, svc.name))
            return

        child.restart_times.append(time.time())
        child.restarts += 1
        delay = self._backoff(child)
        child.next_try = time.time() + delay
        child.state = "waiting"
        ui.info("restarting %s in %ds (attempt %d)"
                % (svc.title, delay, child.restarts))

    def poll_once(self):
        """One pass of the supervision loop."""
        for child in self.children:
            if child.state in ("disabled", "failed"):
                continue

            if child.state == "waiting":
                if time.time() >= child.next_try and self._deps_ready(child):
                    self.start_child(child)
                continue

            if child.popen is not None and child.popen.poll() is not None:
                self._handle_exit(child)
                continue

            if child.alive():
                self._rotate_live(child)

        self._rotate_own_log()

    def _rotate_own_log(self):
        """Rotate the gateway's own log file when running as a daemon."""
        if not self.own_log_path:
            return
        now = time.time()
        if now - self._last_own_log_check < 30:
            return
        self._last_own_log_check = now

        rotate, max_bytes, keep = logrotate.settings(self.cfg)
        if not rotate:
            return
        # stdout/stderr are dup2'd onto this file's fd directly (no Python
        # file object of ours to reopen), so copy-truncate is required here
        # too — a rename would leave writes going into the renamed file.
        logrotate.numbered_live(self.own_log_path, max_bytes=max_bytes,
                                keep=keep, rotate=True)

    def _rotate_live(self, child):
        """Rotate a running service's log without disturbing its writer.

        The child holds its log file open in append mode for its whole
        life, so a plain rename (as used before a service starts) would
        leave it writing into the renamed file forever.  Checked
        periodically rather than every second, since stat()-ing every
        log file every second is needless overhead.
        """
        now = time.time()
        if now - getattr(child, "_last_rotate_check", 0) < 30:
            return
        child._last_rotate_check = now

        rotate, max_bytes, keep = logrotate.settings(self.cfg)
        if not rotate:
            return
        path = child.service.log_path()
        if logrotate.numbered_live(path, max_bytes=max_bytes, keep=keep,
                                   rotate=True):
            try:
                child.log_fh.write(
                    "=== rotated at %s ===\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
                child.log_fh.flush()
            except (OSError, ValueError):
                pass

    def running_count(self):
        return len([c for c in self.children if c.alive()])

    # ── Main loop ───────────────────────────────────────────────────
    def run(self):
        """Start everything, then supervise until signalled.

        Returns the process exit status for `los gateway`.
        """
        paths.ensure_dirs()
        self.started_at = time.time()
        self._last_beat = time.time()

        stop_requested = {"flag": False}

        def _on_signal(signum, frame):
            if not stop_requested["flag"]:
                stop_requested["flag"] = True
                print()
                ui.info("shutting down (signal %d)" % signum)

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        try:
            signal.signal(signal.SIGHUP, _on_signal)
        except (AttributeError, ValueError):
            pass

        # Keep mcp_config.json in step with the configured ports, so the
        # tool discovery aicall does can never point at the wrong place.
        try:
            from . import update as _update
            _update._write_mcp_config(self.cfg)
        except Exception:
            pass

        failures = self.start_all()
        if failures:
            ui.error("gateway could not start: %s"
                     % ", ".join(c.service.title for c in failures))
            self.stop_all(quiet=True)
            return 1

        report.startup(self.cfg)
        self._check_for_update()
        self._announce()

        beat_every = report.interval(self.cfg)
        try:
            while not stop_requested["flag"]:
                time.sleep(1)
                self.poll_once()
                now = time.time()
                if beat_every and now - self._last_beat >= beat_every:
                    self._last_beat = now
                    report.heartbeat(self.cfg, now - self.started_at)
        except KeyboardInterrupt:
            print()
            ui.info("shutting down")

        uptime = time.time() - self.started_at
        report.shutdown(self.cfg, uptime)
        self.stop_all()
        ui.ok("gateway stopped (up %s)" % human_uptime(uptime))
        return 0

    def _check_for_update(self):
        """Mention a newer release, without ever blocking startup."""
        if not config.get_key(self.cfg, "update.auto_check", True):
            return

        def _worker():
            try:
                from . import update as _update
                from . import __version__
                tag, _notes = _update.latest_release(self.cfg)
                if tag and _update._newer(tag, __version__):
                    ui.info("LOS %s is available (you have %s) — "
                            "run `los update`" % (tag, __version__))
            except Exception:
                pass    # offline, rate-limited, no releases yet: all fine

        try:
            threading.Thread(target=_worker, name="los-update-check",
                             daemon=True).start()
        except Exception:
            pass

    def _announce(self):
        """The banner printed once everything is up."""
        web = self.by_name.get("ssl_server")
        print()
        ui.hr()
        ui.ok("LOS gateway running — %d service%s"
              % (self.running_count(), "" if self.running_count() == 1 else "s"))
        if web and web.state == "running":
            port = web.service.port(self.cfg)
            host = web.service.setting(self.cfg, "host", "0.0.0.0")
            shown = "localhost" if host in ("0.0.0.0", "") else host
            print("   %s https://%s:%s"
                  % (ui.color("web:", "grey"), shown, port))
        print("   %s %s" % (ui.color("logs:", "grey"), paths.LOG_DIR))
        print("   %s Ctrl-C to stop" % ui.color("stop:", "grey"))
        ui.hr()
        print()
