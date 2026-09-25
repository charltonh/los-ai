"""`los update` — fetch and apply the latest release from GitHub.

The installation is a git clone, so updating is a tag checkout.  Because
config and runtime state live outside the tracked tree (config.json,
run/, log/, var/ are all ignored), the checkout only ever touches code
and can be rolled back cleanly.

If a service that was healthy before the update fails to come back, we
roll back automatically rather than leaving the system down.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from . import __version__, config, paths, process, registry, report, ui

_API = "https://api.github.com/repos/%s"
_TIMEOUT = 15
_STATE = os.path.join(paths.VAR_DIR, "previous.json")


class UpdateError(Exception):
    pass


# ── git helpers ─────────────────────────────────────────────────────────

def git(*args, **kw):
    """Run git in the software directory and return stdout.

    Output is stripped of surrounding whitespace by default, which is what
    every caller but the porcelain parser wants; pass strip=False for the
    verbatim bytes, where leading spaces are significant.
    """
    check = kw.pop("check", True)
    strip = kw.pop("strip", True)
    result = subprocess.run(
        ["git"] + list(args),
        cwd=paths.SYS_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        raise UpdateError("git %s failed: %s"
                          % (" ".join(args), result.stderr.strip()))
    return result.stdout.strip() if strip else result.stdout


def is_git_checkout():
    return os.path.isdir(os.path.join(paths.SYS_DIR, ".git"))


def current_ref():
    """A description of what is checked out right now."""
    try:
        tag = git("describe", "--tags", "--exact-match", check=False)
        if tag:
            return tag
    except UpdateError:
        pass
    try:
        return git("rev-parse", "--short", "HEAD")
    except UpdateError:
        return "unknown"


def changed_files():
    """The paths with uncommitted changes, for display.

    Parsed from the NUL-delimited porcelain form so paths containing
    spaces come through verbatim rather than quoted.  Untracked files are
    included: they are just as capable of getting in the checkout's way.
    """
    entries = git("status", "--porcelain", "-z", strip=False).split("\0")
    files = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        if not entry:
            i += 1
            continue
        status = entry[:2]
        files.append(entry[3:])
        # A rename or copy names the original file in the next entry; show
        # only where the file is now.
        i += 2 if ("R" in status or "C" in status) else 1
    return files


def ensure_remote(repo):
    """Make sure 'origin' points at the configured repository."""
    url = "https://github.com/%s.git" % repo
    existing = git("remote", "get-url", "origin", check=False)
    if not existing:
        git("remote", "add", "origin", url)
        ui.info("added remote origin -> %s" % url)
    return url


# ── Version discovery ───────────────────────────────────────────────────

def _api(path, repo):
    url = (_API % repo) + path
    request = urllib.request.Request(
        url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "los/%s" % __version__,
        })
    token = os.environ.get("LOS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def latest_release(cfg):
    """The newest release (stable) or tag (dev).

    Returns (tag, notes) — either may be None when nothing is published
    yet, which is a normal state for a young repository.
    """
    repo = config.get_key(cfg, "update.repo") or "charltonh/los-ai"
    channel = config.get_key(cfg, "update.channel") or "stable"

    try:
        if channel == "stable":
            data = _api("/releases/latest", repo)
            return data.get("tag_name"), data.get("body") or ""
        tags = _api("/tags", repo)
        if tags:
            return tags[0].get("name"), ""
        return None, ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, ""
        raise UpdateError("GitHub API returned %s" % exc.code)
    except urllib.error.URLError as exc:
        raise UpdateError("cannot reach GitHub: %s" % exc.reason)
    except (ValueError, KeyError) as exc:
        raise UpdateError("unexpected response from GitHub: %s" % exc)


def _normalise(tag):
    return (tag or "").lstrip("vV")


def _newer(candidate, current):
    """Compare dotted numeric versions; unknown formats compare unequal."""
    def parts(text):
        out = []
        for chunk in _normalise(text).replace("-", ".").split("."):
            out.append(int(chunk) if chunk.isdigit() else chunk)
        return out
    try:
        return parts(candidate) > parts(current)
    except TypeError:
        return _normalise(candidate) != _normalise(current)


# ── Which services were running ─────────────────────────────────────────

def _snapshot(cfg):
    """Record what is running so we can put it back afterwards."""
    from . import commands
    running = []
    for svc in registry.SERVICES:
        state, _, _, _ = commands.service_state(svc, cfg)
        if state == "running":
            running.append(svc.name)
    gateway = process.read_pid(paths.GATEWAY_PID)
    return {
        "services": running,
        "gateway": bool(gateway and process.pid_alive(gateway)),
    }


def _stop_everything(cfg, snapshot):
    if not snapshot["services"] and not snapshot["gateway"]:
        return
    ui.step("stopping services")

    if snapshot["gateway"]:
        pid = process.read_pid(paths.GATEWAY_PID)
        if pid:
            process.terminate(pid, timeout=20)
            process.remove_pid(paths.GATEWAY_PID)
            process.remove_pid(paths.GATEWAY_LOCK)

    for name in reversed(snapshot["services"]):
        svc = registry.get(name)
        pid = process.read_pid(svc.pid_path())
        if pid and process.looks_like(pid, svc.script):
            process.terminate(pid, script=svc.script)
            process.remove_pid(svc.pid_path())


def _start_again(cfg, snapshot):
    """Bring back whatever was running.  Returns the list that failed."""
    if not snapshot["services"] and not snapshot["gateway"]:
        return []

    from . import supervisor
    services = [registry.get(n) for n in snapshot["services"]]
    services = [s for s in services if s is not None]
    if not services:
        return []

    ui.step("restarting services")
    gw = supervisor.Gateway(cfg, services=services)
    failed = []
    for child in gw.children:
        if child.state == "disabled":
            continue
        if gw.start_child(child, quiet=True):
            child.popen = None      # detach; pid file carries the state
        else:
            failed.append(child.service.name)
    return failed


# ── Post-checkout work ──────────────────────────────────────────────────

def _post_checkout(cfg):
    """Rebuild venvs and migrate the config after new code lands."""
    from . import install

    ok = True
    if config.get_key(cfg, "services.ssl_server.enabled"):
        req = os.path.join(paths.SYS_DIR, "ssl_server", "requirements.txt")
        if os.path.exists(req):
            ok = install.build_venv(paths.SSL_VENV, req, "web app") and ok

    if config.get_key(cfg, "channels.whatsapp.enabled"):
        req = os.path.join(paths.SYS_DIR, "whatsapp_mcp", "requirements.txt")
        if os.path.exists(req):
            ok = install.build_venv(
                paths.WHATSAPP_VENV, req, "WhatsApp channel") and ok

    # Re-save the config so any schema migration is written to disk.
    try:
        migrated = config.load()
        config.save(migrated)
        ui.ok("configuration migrated")
    except config.ConfigError as exc:
        ui.warn("could not migrate the configuration: %s" % exc)
        ok = False

    _write_mcp_config(cfg)
    return ok


def _write_mcp_config(cfg):
    """Regenerate aicall_mcp/mcp_config.json from config.json.

    Keeping it generated means the ports in it can never drift from the
    ports the servers actually bind to.
    """
    target = os.path.join(paths.SYS_DIR, "aicall_mcp", "mcp_config.json")
    servers = [
        {"name": "aicall-actions",
         "url": "http://127.0.0.1:%s"
                % config.get_key(cfg, "services.action_mcp.port")},
        {"name": "memory-management",
         "url": "http://127.0.0.1:%s"
                % config.get_key(cfg, "services.memory_mcp.port")},
    ]
    if config.get_key(cfg, "channels.whatsapp.enabled"):
        servers.append(
            {"name": "whatsapp-messaging",
             "url": "http://127.0.0.1:%s"
                    % config.get_key(cfg, "channels.whatsapp.port")})
    try:
        with open(target, "w") as fh:
            json.dump({"servers": servers}, fh, indent=4)
            fh.write("\n")
    except OSError as exc:
        ui.warn("could not write %s: %s" % (target, exc))


# ── Rollback state ──────────────────────────────────────────────────────

def _remember(ref, version):
    try:
        os.makedirs(paths.VAR_DIR, exist_ok=True)
        with open(_STATE, "w") as fh:
            json.dump({"ref": ref, "version": version, "at": int(time.time())},
                      fh)
    except OSError:
        pass


def _recall():
    try:
        with open(_STATE, "r") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ── Main flow ───────────────────────────────────────────────────────────

def run(args):
    cfg = config.load(strict=False)

    if not is_git_checkout():
        ui.error("%s is not a git checkout — cannot update automatically."
                 % paths.SYS_DIR)
        ui.info("re-install with: git clone https://github.com/%s.git %s"
                % (config.get_key(cfg, "update.repo"), paths.SYS_DIR))
        return 1

    repo = config.get_key(cfg, "update.repo") or "charltonh/los-ai"

    # What is available?
    if args.to:
        target = args.to
        notes = ""
        ui.info("target version: %s (requested)" % target)
    else:
        ui.step("checking %s for a newer release" % repo)
        try:
            target, notes = latest_release(cfg)
        except UpdateError as exc:
            ui.error(str(exc))
            return 1

        if not target:
            ui.info("no releases published yet for %s" % repo)
            return 0

        if not _newer(target, __version__):
            ui.ok("LOS %s is up to date (latest is %s)"
                  % (__version__, target))
            return 0

        ui.ok("a newer version is available: %s (you have %s)"
              % (target, __version__))
        if notes:
            print()
            for line in notes.strip().splitlines()[:20]:
                print("   %s" % line)
            print()

    if args.check:
        return 1 if not args.to else 0

    # Safety gate.  Rather than refusing to run when the working tree has
    # been edited, name what changed and offer to set it aside.  The edits
    # go into a stash, so agreeing is not destructive and `git stash pop`
    # brings them back.  --force and --yes skip the question and take the
    # default.
    changes = changed_files()
    if changes:
        ui.warn("%s has local modifications:" % paths.SYS_DIR)
        for path in changes:
            print("   %s" % path)
        print()
        overwrite = args.force or args.yes
        if not overwrite:
            overwrite = ui.ask_yes_no("Overwrite these local changes?", True)
        if not overwrite:
            ui.info("cancelled — your local changes are untouched")
            return 0
        ui.step("setting local changes aside (recover with `git stash pop`)")
        git("stash", "push", "-u", "-m", "los update %s" % int(time.time()))

    if not args.yes and not args.to:
        if not ui.ask_yes_no("Update to %s now?" % target, True):
            ui.info("cancelled")
            return 0

    previous_ref = current_ref()
    previous_version = __version__
    _remember(previous_ref, previous_version)

    snapshot = _snapshot(cfg)
    _stop_everything(cfg, snapshot)

    # Fetch and check out.
    ensure_remote(repo)
    ui.step("fetching")
    try:
        git("fetch", "--tags", "--prune", "origin")
        ui.step("checking out %s" % target)
        git("checkout", "--quiet", target)
    except UpdateError as exc:
        ui.error(str(exc))
        ui.step("restoring %s" % previous_ref)
        git("checkout", "--quiet", previous_ref, check=False)
        _start_again(cfg, snapshot)
        report.updated(cfg, previous_version, target, ok=False)
        return 1

    ui.ok("checked out %s" % target)

    # Rebuild and migrate.
    _post_checkout(cfg)

    # Bring the system back, and roll back if it will not come up.
    cfg = config.load(strict=False)
    failed = _start_again(cfg, snapshot)

    if failed:
        ui.error("these services did not come back: %s" % ", ".join(failed))
        ui.warn("rolling back to %s" % previous_ref)
        _stop_everything(cfg, snapshot)
        git("checkout", "--quiet", previous_ref, check=False)
        _post_checkout(cfg)
        _start_again(cfg, snapshot)
        report.updated(cfg, previous_version, target, ok=False)
        ui.info("rolled back — check the logs in %s" % paths.LOG_DIR)
        return 1

    report.updated(cfg, previous_version, target, ok=True)

    print()
    ui.ok("updated %s -> %s" % (previous_version, target))
    if snapshot["gateway"]:
        ui.info("the gateway was running before the update — "
                "start it again with `los gateway`")
    return 0


def rollback(args):
    """Return to the version recorded by the last update."""
    cfg = config.load(strict=False)

    if not is_git_checkout():
        ui.error("%s is not a git checkout" % paths.SYS_DIR)
        return 1

    previous = _recall()
    if not previous:
        ui.error("no previous version recorded — nothing to roll back to")
        return 1

    ref = previous.get("ref")
    ui.info("rolling back to %s (from %s)" % (ref, current_ref()))
    if not ui.ask_yes_no("Continue?", True):
        return 0

    snapshot = _snapshot(cfg)
    _stop_everything(cfg, snapshot)

    try:
        git("checkout", "--quiet", ref)
    except UpdateError as exc:
        ui.error(str(exc))
        _start_again(cfg, snapshot)
        return 1

    _post_checkout(cfg)
    _start_again(cfg, config.load(strict=False) and snapshot)

    ui.ok("rolled back to %s" % ref)
    return 0
