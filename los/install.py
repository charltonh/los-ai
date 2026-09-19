"""`los install` — set up or repair an installation.

Runs as an ordinary user.  What it needs is ownership of /los, which has
to be created by root first; if that is missing we stop and print the
exact commands to fix it rather than trying to escalate.

The sequence is deliberate: check the machine, ask the questions, show
the licence, report the install, and only then create anything.  Nothing
is written until the user has accepted the terms.
"""

import getpass
import os
import shutil
import subprocess
import sys

from . import __version__, config, paths, registry, report, ui


# ── Preconditions ───────────────────────────────────────────────────────

def _whoami():
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "your-user")


def check_platform():
    """Refuse anything that is not Linux."""
    if sys.platform.startswith("linux"):
        return True
    ui.error("LOS runs on Linux only (this is %s)." % sys.platform)
    print()
    print("  On Windows or macOS, run LOS inside a Linux VM and install")
    print("  it there.  The web interface is reachable from the host.")
    return False


def check_base_dir():
    """/los must exist and be owned by whoever is installing.

    It lives at the filesystem root on purpose — it can be its own
    partition, encrypted if you like — so creating it needs root even
    though everything after that does not.
    """
    user = _whoami()
    base = paths.BASE_DIR

    if not os.path.isdir(base):
        ui.error("%s does not exist." % base)
        print()
        print("  LOS lives in its own directory at the filesystem root.")
        print("  Create it as root, owned by you:")
        print()
        print(ui.color("      sudo mkdir -p %s" % base, "bold"))
        print(ui.color("      sudo chown %s:%s %s"
                       % (user, _primary_group(user), base), "bold"))
        print()
        print("  Then run this installer again.  Nothing else needs root.")
        return False

    if not os.access(base, os.W_OK):
        ui.error("%s is not writable by %s." % (base, user))
        print()
        print("  Give yourself ownership:")
        print()
        print(ui.color("      sudo chown -R %s:%s %s"
                       % (user, _primary_group(user), base), "bold"))
        print()
        return False

    return True


def _primary_group(user):
    try:
        import grp
        import pwd
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    except Exception:
        return user


def check_python():
    if sys.version_info >= (3, 8):
        return True
    ui.error("Python 3.8 or newer is required (found %d.%d.%d)"
             % sys.version_info[:3])
    return False


def check_tools():
    """Warn about missing helpers; only git is strictly required."""
    ok = True
    for tool, required in (("git", True), ("openssl", True),
                           ("python3", True)):
        if shutil.which(tool):
            continue
        if required:
            ui.error("%s is not installed" % tool)
            ok = False
        else:
            ui.warn("%s is not installed" % tool)
    return ok


def check_terminal(args):
    """The setup questions need a terminal on stdin.

    Piped in — `curl … | sh`, or `los install < /dev/null` — stdin is at
    EOF, so every prompt would read nothing and quietly return its
    default.  For the licence that default is "no", which then looks like
    a refusal rather than a missing keyboard.  Stop and say so instead.
    """
    if args.yes or sys.stdin.isatty():
        return True

    ui.error("the setup questions need a terminal, but stdin is not one.")
    print()
    print("  Run this in a terminal:")
    print()
    print(ui.color("      los install", "bold"))
    print()
    print("  Or accept the defaults without being asked — this also")
    print("  accepts the licence:")
    print()
    print(ui.color("      los install --yes", "bold"))
    print()
    return False


def suggest_home_symlink(user):
    """Offer the /home/<user>/los -> /los/<user> convenience link.

    People naturally look for their agenda in their home directory; the
    link makes that work without moving anything.
    """
    home = os.path.expanduser("~")
    link = os.path.join(home, "los")
    target = paths.user_dir(user)

    if not os.path.isdir(target):
        return
    if os.path.islink(link) or os.path.exists(link):
        return

    print()
    ui.info("Your agenda lives in %s." % target)
    print("  You can reach it from your home directory with a symlink:")
    print()
    print(ui.color("      ln -s %s %s" % (target, link), "bold"))
    print()
    if ui.ask_yes_no("Create that link now?", True):
        try:
            os.symlink(target, link)
            ui.ok("created %s -> %s" % (link, target))
        except OSError as exc:
            ui.warn("could not create the link: %s" % exc)



# ── Licence ─────────────────────────────────────────────────────────────

LICENCE_SUMMARY = """\
  LOS is dual-licensed:

    * GNU General Public License v3.0 — free to use, study, modify and
      share, provided derived works stay under the GPL.

    * Dynet Commercial License — required for commercial use above the
      threshold described in LICENSE.txt.

  The full terms are in GPL-3.0.txt and LICENSE.txt alongside this
  software.  Installing and running LOS also sends a small status record
  to the project (version, platform, which services are enabled) so
  releases can be tracked against real deployments.
"""


def accept_licence(assume_yes=False):
    """Show the terms and require an explicit yes before anything is built."""
    ui.heading("Licence")
    print(LICENCE_SUMMARY)

    licence_file = os.path.join(paths.SYS_DIR, "LICENSE.txt")
    if os.path.exists(licence_file):
        print("  Full text: %s" % ui.color(licence_file, "grey"))
        print()
        if not assume_yes and ui.ask_yes_no("Read LICENSE.txt now?", False):
            _page(licence_file)

    if assume_yes:
        ui.info("licence accepted (--yes)")
        return True

    if ui.ask_yes_no("Do you accept these terms?", False):
        ui.ok("licence accepted")
        return True

    ui.error("installation cancelled — the terms were not accepted")
    return False


def _page(path):
    pager = os.environ.get("PAGER") or ("less" if shutil.which("less") else None)
    try:
        if pager:
            subprocess.call([pager, path])
        else:
            with open(path, "r") as fh:
                print(fh.read())
    except OSError:
        pass


# ── Building ────────────────────────────────────────────────────────────

def build_venv(venv_dir, requirements, label):
    """Create (or update) a virtualenv from a requirements file."""
    python = os.path.join(venv_dir, "bin", "python")

    if not os.path.exists(python):
        ui.step("creating the %s virtualenv" % label)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "venv", venv_dir],
                stdout=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, OSError) as exc:
            ui.error("could not create %s: %s" % (venv_dir, exc))
            ui.info("on Debian/Ubuntu you may need: apt install python3-venv")
            return False
    else:
        ui.info("%s virtualenv already exists" % label)

    if not os.path.exists(requirements):
        ui.warn("no %s — skipping dependency install" % requirements)
        return True

    ui.step("installing %s dependencies" % label)
    try:
        subprocess.check_call(
            [python, "-m", "pip", "install", "--quiet", "--upgrade",
             "-r", requirements])
    except (subprocess.CalledProcessError, OSError) as exc:
        ui.error("pip failed for %s: %s" % (label, exc))
        return False

    ui.ok("%s dependencies ready" % label)
    return True


def generate_certificate():
    """Self-signed TLS for the web app, if there isn't one already."""
    if os.path.exists(paths.SSL_CERT) and os.path.exists(paths.SSL_KEY):
        ui.info("TLS certificate already present")
        return True

    os.makedirs(paths.SSL_CERT_DIR, exist_ok=True)
    ui.step("generating a self-signed TLS certificate")
    try:
        subprocess.check_call([
            "openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
            "-out", paths.SSL_CERT, "-keyout", paths.SSL_KEY,
            "-days", "3650", "-subj", "/CN=localhost",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError) as exc:
        ui.error("openssl failed: %s" % exc)
        return False

    try:
        os.chmod(paths.SSL_KEY, 0o600)
    except OSError:
        pass
    ui.ok("certificate written to %s" % paths.SSL_CERT)
    return True


# ── Interview ───────────────────────────────────────────────────────────

def interview(cfg, args):
    """Ask the configuration questions.  Nothing is written yet."""
    ui.heading("Configuration")

    # Who is this installation for?
    from . import configure
    existing = configure._known_users()
    default_user = cfg.get("user") or _whoami()

    if existing:
        print("  Entities already under %s: %s"
              % (paths.BASE_DIR, ", ".join(existing)))
        default_user = cfg.get("user") or existing[0]
    else:
        print(ui.color("  Each LOS install has one primary user, whose agenda\n"
                       "  lives in %s/<name>." % paths.BASE_DIR, "grey"))
    print()

    if args.yes:
        user = default_user
        ui.info("primary user: %s" % user)
    else:
        user = ui.ask("Primary LOS user", default_user)
    cfg["user"] = user

    # Web app port.
    if not args.yes:
        port = ui.ask_int("Web interface port",
                          config.get_key(cfg, "services.ssl_server.port"),
                          minimum=1, maximum=65535)
        config.set_key(cfg, "services.ssl_server.port", port)

        listen = ui.ask(
            "Listen address (0.0.0.0 for the whole network, 127.0.0.1 local)",
            config.get_key(cfg, "services.ssl_server.host"))
        config.set_key(cfg, "services.ssl_server.host", listen)

    # Channels are opt-in.
    wanted = list(args.channel)
    if not args.yes and "whatsapp" not in wanted:
        print()
        print(ui.color(
            "  Channels let you reach LOS from outside the web app.\n"
            "  WhatsApp drives a real browser session and needs its own\n"
            "  virtualenv; you can always enable it later with `los config`.",
            "grey"))
        print()
        if ui.ask_yes_no("Enable the WhatsApp channel?", False):
            wanted.append("whatsapp")

    for name in wanted:
        if name not in cfg.get("channels", {}):
            ui.warn("unknown channel %r — ignoring" % name)
            continue
        config.set_key(cfg, "channels.%s.enabled" % name, True)

    return wanted


# ── Main flow ───────────────────────────────────────────────────────────

def run(args):
    ui.heading("LOS %s — installation" % __version__)

    # 1. Is this machine suitable, and is /los ours to write to?
    if not check_platform():
        return 1
    if not check_python():
        return 1
    if not check_base_dir():
        return 1
    if not check_tools():
        return 1
    ui.ok("%s is writable by %s" % (paths.BASE_DIR, _whoami()))

    if not check_terminal(args):
        return 1

    already = config.exists()
    if already and not args.repair:
        ui.info("this machine is already configured (%s)" % paths.CONFIG_FILE)
        if not args.yes and not ui.ask_yes_no(
                "Re-run setup over the existing installation?", False):
            ui.info("nothing to do — try `los doctor` or `los config`")
            return 0

    cfg = config.load(strict=False) if already else config.defaults()

    # 2. Ask the questions.
    channels = interview(cfg, args)

    # 3. Licence, before anything is created.
    if not accept_licence(assume_yes=args.yes):
        return 1

    # 4. Tell the project we are installing, while we still have the
    #    user's attention and before anything can fail half-way.
    config.ensure_identity(cfg)
    report.install(cfg, "install")

    # 5. Build.
    ui.heading("Installing")
    paths.ensure_dirs()
    ui.ok("created run/, log/ and var/ under %s" % paths.SYS_DIR)

    ok = True
    if config.get_key(cfg, "services.ssl_server.enabled"):
        ok = build_venv(paths.SSL_VENV,
                        os.path.join(paths.SYS_DIR, "ssl_server",
                                     "requirements.txt"),
                        "web app") and ok
        ok = generate_certificate() and ok

    if "whatsapp" in channels:
        ok = build_venv(paths.WHATSAPP_VENV,
                        os.path.join(paths.SYS_DIR, "whatsapp_mcp",
                                     "requirements.txt"),
                        "WhatsApp channel") and ok

    _check_core_imports()

    # 6. Write the configuration.
    config.save(cfg)
    ui.ok("wrote %s" % paths.CONFIG_FILE)

    # 7. The user's agenda.
    _ensure_user(cfg, args)

    # 8. Optional conveniences.
    if not args.yes:
        suggest_home_symlink(cfg["user"])
        _offer_path_hint()
        _offer_service_unit()

    # 9. Report the outcome and summarise.
    report.install(cfg, "installed" if ok else "install_failed")

    print()
    if ok:
        ui.ok("LOS is installed.")
    else:
        ui.warn("LOS is installed, but some steps failed — run `los doctor`.")

    print()
    print("  Start everything:   %s" % ui.color("los gateway", "bold"))
    print("  Check the system:   %s" % ui.color("los doctor", "bold"))
    print("  Change settings:    %s" % ui.color("los config", "bold"))
    print()
    return 0 if ok else 1


def _check_core_imports():
    """The core services run on the system python; make sure it has Flask."""
    try:
        subprocess.check_call(
            [sys.executable, "-c", "import flask, requests"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ui.ok("system python has the core dependencies")
        return True
    except (subprocess.CalledProcessError, OSError):
        pass

    ui.warn("the system python is missing Flask and/or requests")
    print("  The action and memory servers run on %s." % sys.executable)
    print("  Install them with your package manager, for example:")
    print()
    print(ui.color("      pip install --user -r %s"
                   % os.path.join(paths.SYS_DIR, "requirements.txt"), "bold"))
    print()
    return False


def _ensure_user(cfg, args):
    """Make sure the primary user's agenda exists."""
    user = cfg.get("user")
    target = paths.user_dir(user)
    if os.path.isdir(target):
        ui.ok("agenda found at %s" % target)
        return True

    tool = os.path.join(paths.BIN_DIR, "los-user-manage")
    ui.warn("no agenda at %s yet" % target)

    if args.yes or not os.path.exists(tool):
        print("  Create it with: %s"
              % ui.color("%s create %s" % (tool, user), "bold"))
        return False

    if not ui.ask_yes_no("Create the LOS user '%s' now?" % user, True):
        print("  Later, run: %s"
              % ui.color("%s create %s" % (tool, user), "bold"))
        return False

    try:
        subprocess.call([sys.executable, tool, "create", user])
    except OSError as exc:
        ui.error("could not run %s: %s" % (tool, exc))
        return False
    return os.path.isdir(target)


def _offer_path_hint():
    """Suggest putting bin/ on $PATH so `los` works everywhere."""
    if shutil.which("los"):
        return
    print()
    ui.info("`los` is not on your $PATH yet.")
    print("  Add this to your shell profile:")
    print()
    print(ui.color('      export PATH="%s:$PATH"' % paths.BIN_DIR, "bold"))
    print()
    print("  Or link it system-wide:")
    print()
    print(ui.color("      sudo ln -s %s/los /usr/local/bin/los"
                   % paths.BIN_DIR, "bold"))


# ── Optional boot-time integration ──────────────────────────────────────

SYSTEMD_UNIT = """\
[Unit]
Description=LOS gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%(user)s
ExecStart=%(bin)s gateway
ExecStop=%(bin)s stop
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""

OPENRC_SCRIPT = """\
#!/sbin/openrc-run

name="los"
description="LOS gateway"
command="%(bin)s"
command_args="gateway"
command_user="%(user)s"
command_background=true
pidfile="%(pidfile)s"

depend() {
    need net
}
"""


def _offer_service_unit():
    """Write a boot-time service file if the user wants one.

    LOS does not need an init system — `los gateway` is self-contained —
    but people who want it started at boot should not have to write the
    unit themselves.  Nothing is installed without being asked, and we
    only ever write into var/, never into /etc.
    """
    print()
    if not ui.ask_yes_no("Generate a service file to start LOS at boot?",
                         False):
        return

    los_bin = os.path.join(paths.BIN_DIR, "los")
    user = _whoami()
    have_systemd = os.path.isdir("/run/systemd/system")
    have_openrc = bool(shutil.which("rc-service"))

    if have_systemd and have_openrc:
        kind = ui.ask_choice("Which init system?", ["systemd", "openrc"],
                             "systemd")
    elif have_openrc:
        kind = "openrc"
    elif have_systemd:
        kind = "systemd"
    else:
        kind = ui.ask_choice("Which init system?", ["systemd", "openrc"],
                             "systemd")

    if kind == "systemd":
        body = SYSTEMD_UNIT % {"user": user, "bin": los_bin}
        out = os.path.join(paths.VAR_DIR, "los.service")
        install_cmd = ("sudo cp %s /etc/systemd/system/los.service && "
                       "sudo systemctl enable --now los" % out)
    else:
        body = OPENRC_SCRIPT % {"user": user, "bin": los_bin,
                                "pidfile": paths.GATEWAY_PID}
        out = os.path.join(paths.VAR_DIR, "los.initd")
        install_cmd = ("sudo cp %s /etc/init.d/los && "
                       "sudo chmod +x /etc/init.d/los && "
                       "sudo rc-update add los default" % out)

    try:
        with open(out, "w") as fh:
            fh.write(body)
    except OSError as exc:
        ui.error("could not write %s: %s" % (out, exc))
        return

    ui.ok("wrote %s" % out)
    print("  To install it (needs root):")
    print()
    print(ui.color("      %s" % install_cmd, "bold"))
    print()
    print(ui.color("  This is entirely optional — `los gateway` works "
                   "without it.", "grey"))
