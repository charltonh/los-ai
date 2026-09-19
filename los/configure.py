"""`los config` — view and change the configuration.

Two faces on one file: a numbered interactive menu for people, and
get/set/list/validate for scripts and the installer.  Both read and write
/los/sys/config.json, which stays hand-editable.
"""

import json
import os
import subprocess

from . import config, paths, registry, ui

_MISSING = object()


# ── Non-interactive actions ─────────────────────────────────────────────

def _print_value(value):
    if isinstance(value, bool):
        print("true" if value else "false")
    elif value is None:
        print("")
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2))
    else:
        print(value)


def action_get(args):
    cfg = config.load()
    value = config.get_key(cfg, args.key, _MISSING)
    if value is _MISSING:
        ui.error("no such setting: %s" % args.key)
        return 1
    _print_value(value)
    return 0


def action_set(args):
    cfg = config.load()
    if config.get_key(cfg, args.key, _MISSING) is _MISSING:
        ui.warn("%s is not a known setting; adding it anyway" % args.key)
    value = config.coerce(args.value)
    config.set_key(cfg, args.key, value)

    problems = config.validate(cfg)
    if problems:
        ui.warn("the configuration now has problems:")
        for problem in problems:
            print("   %s" % problem)

    config.save(cfg)
    ui.ok("%s = %s" % (args.key, json.dumps(value)))
    _restart_hint(args.key)
    return 0


def action_unset(args):
    cfg = config.load()
    if not config.unset_key(cfg, args.key):
        ui.error("no such setting: %s" % args.key)
        return 1
    config.save(cfg)
    ui.ok("removed %s" % args.key)
    return 0


def action_list(args):
    cfg = config.load()
    if getattr(args, "json", False):
        print(json.dumps(cfg, indent=2))
        return 0
    for key, value in config.flatten(cfg):
        if key.endswith("secret_key") and value:
            value = "<set>"
        print("%-42s %s" % (key, json.dumps(value)))
    return 0


def action_validate(args):
    cfg = config.load()
    problems = config.validate(cfg)
    if problems:
        ui.error("%d problem(s) in %s" % (len(problems), paths.CONFIG_FILE))
        for problem in problems:
            print("   %s" % problem)
        return 1
    ui.ok("%s is valid" % paths.CONFIG_FILE)
    return 0


def action_edit(args):
    """Open config.json in $EDITOR, and refuse to keep an invalid result."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    if not config.exists():
        config.save(config.defaults())

    with open(paths.CONFIG_FILE, "r") as fh:
        before = fh.read()

    try:
        subprocess.call([editor, paths.CONFIG_FILE])
    except OSError as exc:
        ui.error("cannot run %s: %s" % (editor, exc))
        return 1

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        ui.error(str(exc))
        ui.warn("restoring the previous contents")
        with open(paths.CONFIG_FILE, "w") as fh:
            fh.write(before)
        return 1

    problems = config.validate(cfg)
    if problems:
        ui.warn("saved, but with problems:")
        for problem in problems:
            print("   %s" % problem)
        return 1
    ui.ok("saved")
    return 0


def action_path(args):
    print(paths.CONFIG_FILE)
    return 0




# ── Interactive menu ────────────────────────────────────────────────────

def _summary(cfg):
    """Short descriptions shown beside each menu entry."""
    core = []
    for svc in registry.SERVICES:
        if svc.group != "core":
            continue
        mark = svc.name if svc.is_enabled(cfg) else ui.color(svc.name, "grey")
        core.append(mark)

    channels = []
    for name in sorted(cfg.get("channels", {})):
        on = config.get_key(cfg, "channels.%s.enabled" % name)
        channels.append("%s [%s]" % (name, "enabled" if on else "disabled"))

    ollama = config.get_key(cfg, "ai.ollama.enabled")
    managed = config.get_key(cfg, "ai.ollama.managed")
    ai = "ollama [%s]" % ("managed" if (ollama and managed)
                          else "external" if ollama else "off")

    return {
        "core": ", ".join(core),
        "channels": ", ".join(channels) or "none",
        "ai": ai,
        "update": "%s, auto-check %s" % (
            config.get_key(cfg, "update.channel"),
            "on" if config.get_key(cfg, "update.auto_check") else "off"),
        "paths": "%s, %s" % (cfg.get("user") or "no user", paths.BASE_DIR),
        "logging": (
            "rotate at %s MB, keep %s"
            % (config.get_key(cfg, "logging.max_bytes", 0) // (1024 * 1024),
               config.get_key(cfg, "logging.keep"))
            if config.get_key(cfg, "logging.rotate")
            else ui.color("rotation off", "grey")
        ),
    }


def run_menu(cfg):
    """The top-level loop.  Returns True when the config was saved."""
    dirty = False
    while True:
        info = _summary(cfg)
        ui.heading("LOS configuration  —  %s" % paths.CONFIG_FILE)
        print("  1) Core services      %s" % info["core"])
        print("  2) Channels           %s" % info["channels"])
        print("  3) AI providers       %s" % info["ai"])
        print("  4) Updates            %s" % info["update"])
        print("  5) User and paths     %s" % info["paths"])
        print("  6) Logging            %s" % info["logging"])
        print()
        print("  %s save and quit    %s write    %s discard and quit"
              % (ui.color("q)", "bold"), ui.color("w)", "bold"),
                 ui.color("x)", "bold")))
        print()

        choice = ui.ask("Choose", "q").strip().lower()

        if choice == "1":
            dirty = _menu_core(cfg) or dirty
        elif choice == "2":
            dirty = _menu_channels(cfg) or dirty
        elif choice == "3":
            dirty = _menu_ai(cfg) or dirty
        elif choice == "4":
            dirty = _menu_update(cfg) or dirty
        elif choice == "5":
            dirty = _menu_user(cfg) or dirty
        elif choice == "6":
            dirty = _menu_logging(cfg) or dirty
        elif choice == "w":
            if _save(cfg):
                dirty = False
        elif choice == "q":
            if dirty and not _save(cfg):
                continue
            return True
        elif choice == "x":
            if dirty and not ui.ask_yes_no("Discard your changes?", False):
                continue
            ui.info("no changes written")
            return False
        else:
            ui.warn("Choose 1-6, w, q or x.")


def _save(cfg):
    problems = config.validate(cfg)
    if problems:
        ui.warn("configuration problems:")
        for problem in problems:
            print("   %s" % problem)
        if not ui.ask_yes_no("Save anyway?", False):
            return False
    config.ensure_identity(cfg)
    config.save(cfg)
    ui.ok("saved %s" % paths.CONFIG_FILE)
    return True


def _menu_core(cfg):
    """Enable/disable core services and set their host and port."""
    changed = False
    services = [s for s in registry.SERVICES if s.group == "core"]
    while True:
        ui.heading("Core services")
        for index, svc in enumerate(services, 1):
            enabled = svc.is_enabled(cfg)
            port = svc.port(cfg)
            state = ui.color("on ", "green") if enabled else ui.color("off", "grey")
            print("  %d) %-12s %s  %s" % (
                index, svc.name, state,
                ui.color("port %s" % port, "grey") if port else ""))
        print("  b) back")
        print()

        choice = ui.ask("Choose", "b").strip().lower()
        if choice in ("b", ""):
            return changed
        if not choice.isdigit() or not (1 <= int(choice) <= len(services)):
            ui.warn("Choose 1-%d or b." % len(services))
            continue

        svc = services[int(choice) - 1]
        changed = _edit_service(cfg, svc) or changed


def _edit_service(cfg, svc):
    """Edit one service's settings."""
    prefix = svc.config_prefix
    changed = False
    ui.heading(svc.title)

    enabled = ui.ask_yes_no("Enable %s?" % svc.name, svc.is_enabled(cfg))
    if enabled != svc.is_enabled(cfg):
        config.set_key(cfg, prefix + ".enabled", enabled)
        changed = True

    if not enabled:
        return changed

    if config.get_key(cfg, prefix + ".host") is not None:
        host = ui.ask("Listen address", config.get_key(cfg, prefix + ".host"))
        if host != config.get_key(cfg, prefix + ".host"):
            config.set_key(cfg, prefix + ".host", host)
            changed = True

    if config.get_key(cfg, prefix + ".port") is not None:
        port = ui.ask_int("Port", config.get_key(cfg, prefix + ".port"),
                          minimum=1, maximum=65535)
        if port != config.get_key(cfg, prefix + ".port"):
            config.set_key(cfg, prefix + ".port", port)
            changed = True

    if svc.name == "loscron":
        interval = ui.ask_int("Seconds between checks",
                              config.get_key(cfg, prefix + ".interval"),
                              minimum=5)
        if interval != config.get_key(cfg, prefix + ".interval"):
            config.set_key(cfg, prefix + ".interval", interval)
            changed = True

    if svc.name == "ssl_server":
        debug = ui.ask_yes_no("Flask debug mode (development only)?",
                              bool(config.get_key(cfg, prefix + ".debug")))
        if debug != bool(config.get_key(cfg, prefix + ".debug")):
            config.set_key(cfg, prefix + ".debug", debug)
            changed = True


# ── Channels ────────────────────────────────────────────────────────────

def _menu_channels(cfg):
    """Channels are optional and open-ended; more can be added later."""
    changed = False
    while True:
        names = sorted(cfg.get("channels", {}))
        ui.heading("Channels")
        print(ui.color("  Optional ways to talk to LOS from outside the "
                       "web app.", "grey"))
        print()
        for index, name in enumerate(names, 1):
            on = config.get_key(cfg, "channels.%s.enabled" % name)
            state = ui.color("on ", "green") if on else ui.color("off", "grey")
            print("  %d) %-12s %s" % (index, name, state))
        print("  b) back")
        print()

        choice = ui.ask("Choose", "b").strip().lower()
        if choice in ("b", ""):
            return changed
        if not choice.isdigit() or not (1 <= int(choice) <= len(names)):
            ui.warn("Choose 1-%d or b." % len(names))
            continue

        name = names[int(choice) - 1]
        if name == "whatsapp":
            changed = _edit_whatsapp(cfg) or changed
        else:
            changed = _edit_generic_channel(cfg, name) or changed


def _edit_generic_channel(cfg, name):
    """Enable/disable and port for a channel we have no special UI for."""
    prefix = "channels.%s" % name
    changed = False
    ui.heading(name)
    enabled = ui.ask_yes_no("Enable %s?" % name,
                            bool(config.get_key(cfg, prefix + ".enabled")))
    if enabled != bool(config.get_key(cfg, prefix + ".enabled")):
        config.set_key(cfg, prefix + ".enabled", enabled)
        changed = True
    if enabled and config.get_key(cfg, prefix + ".port") is not None:
        port = ui.ask_int("Port", config.get_key(cfg, prefix + ".port"),
                          minimum=1, maximum=65535)
        if port != config.get_key(cfg, prefix + ".port"):
            config.set_key(cfg, prefix + ".port", port)
            changed = True
    return changed


def _edit_whatsapp(cfg):
    """The full WhatsApp setup walk-through.

    Enabling the channel needs more than a flag: a virtualenv, a browser
    session, phone numbers and a matching label in the user's .config.
    Each of those is checked here so 'optional' really does mean optional.
    """
    prefix = "channels.whatsapp"
    changed = False
    ui.heading("WhatsApp channel")
    print(ui.color(
        "  Talks to WhatsApp Web through a real browser session.  The first\n"
        "  run shows a QR code to link your phone.", "grey"))
    print()

    was = bool(config.get_key(cfg, prefix + ".enabled"))
    enabled = ui.ask_yes_no("Enable the WhatsApp channel?", was)
    if enabled != was:
        config.set_key(cfg, prefix + ".enabled", enabled)
        changed = True

    if not enabled:
        if was:
            ui.info("WhatsApp will not start with the next gateway")
        return changed

    # The channel needs its own virtualenv (selenium and a browser driver).
    if not os.path.isdir(paths.WHATSAPP_VENV):
        ui.warn("the WhatsApp virtualenv is missing: %s" % paths.WHATSAPP_VENV)
        ui.info("run `los install --channel whatsapp` to create it")
    else:
        ui.ok("virtualenv present")

    port = ui.ask_int("Port", config.get_key(cfg, prefix + ".port"),
                      minimum=1, maximum=65535)
    if port != config.get_key(cfg, prefix + ".port"):
        config.set_key(cfg, prefix + ".port", port)
        changed = True

    headless = ui.ask_yes_no(
        "Run the browser headless? (answer no to scan the QR code)",
        bool(config.get_key(cfg, prefix + ".headless")))
    if headless != bool(config.get_key(cfg, prefix + ".headless")):
        config.set_key(cfg, prefix + ".headless", headless)
        changed = True

    print()
    print(ui.color("  Phone numbers, digits only, including country code.",
                   "grey"))
    for key, prompt in (
        ("origin_num", "The number LOS sends from"),
        ("owner_num", "Your own number (used as the default recipient)"),
    ):
        current = config.get_key(cfg, "%s.%s" % (prefix, key)) or ""
        value = ui.ask(prompt, current, allow_empty=True)
        value = "".join(ch for ch in str(value) if ch.isdigit())
        if value != current:
            config.set_key(cfg, "%s.%s" % (prefix, key), value)
            changed = True

    label = ui.ask("aicall label for incoming messages",
                   config.get_key(cfg, prefix + ".aicall_label"))
    if label != config.get_key(cfg, prefix + ".aicall_label"):
        config.set_key(cfg, prefix + ".aicall_label", label)
        changed = True

    _check_whatsapp_label(cfg, label)
    return changed


def _check_whatsapp_label(cfg, label):
    """Warn when the user's .config has no section for this label.

    Incoming messages are dispatched to `aicall <label>`, so a missing
    label means every message fails with a confusing error.
    """
    user = cfg.get("user")
    if not user:
        return
    user_config = os.path.join(paths.user_dir(user), ".config")
    if not os.path.exists(user_config):
        ui.warn("no %s yet — create it before using the channel" % user_config)
        return
    try:
        with open(user_config, "r") as fh:
            text = fh.read()
    except OSError:
        return

    if ("%s:" % label) in text:
        ui.ok("label '%s' found in %s" % (label, user_config))
        return

    ui.warn("label '%s' is not defined in %s" % (label, user_config))
    print(ui.color(
        "  Incoming messages are handled by `aicall %s`, so that label\n"
        "  needs a section giving it an aiconfig, memory and prompt.\n"
        "  See the 'whatsapp_incoming' example in the documentation."
        % label, "grey"))


# ── AI, updates, user ───────────────────────────────────────────────────

def _menu_ai(cfg):
    """Ollama is the only provider the gateway knows about directly;
    everything else is configured per entity in the user's .config."""
    changed = False
    ui.heading("AI providers")
    print(ui.color(
        "  Per-entity models are configured in /los/<user>/.config.\n"
        "  This section only covers what the gateway itself touches.",
        "grey"))
    print()

    enabled = ui.ask_yes_no("Use Ollama?",
                            bool(config.get_key(cfg, "ai.ollama.enabled")))
    if enabled != bool(config.get_key(cfg, "ai.ollama.enabled")):
        config.set_key(cfg, "ai.ollama.enabled", enabled)
        changed = True

    if enabled:
        url = ui.ask("Ollama URL", config.get_key(cfg, "ai.ollama.url"))
        if url != config.get_key(cfg, "ai.ollama.url"):
            config.set_key(cfg, "ai.ollama.url", url)
            changed = True

        managed = ui.ask_yes_no(
            "Should the gateway start and stop Ollama for you?",
            bool(config.get_key(cfg, "ai.ollama.managed")))
        if managed != bool(config.get_key(cfg, "ai.ollama.managed")):
            config.set_key(cfg, "ai.ollama.managed", managed)
            changed = True
        if managed:
            ui.info("not yet implemented — start `ollama serve` yourself "
                    "for now")

    return changed


def _menu_update(cfg):
    changed = False
    ui.heading("Updates")

    channel = ui.ask_choice("Release channel", ["stable", "dev"],
                            config.get_key(cfg, "update.channel"))
    if channel != config.get_key(cfg, "update.channel"):
        config.set_key(cfg, "update.channel", channel)
        changed = True

    repo = ui.ask("GitHub repository", config.get_key(cfg, "update.repo"))
    if repo != config.get_key(cfg, "update.repo"):
        config.set_key(cfg, "update.repo", repo)
        changed = True

    auto = ui.ask_yes_no("Check for updates when the gateway starts?",
                         bool(config.get_key(cfg, "update.auto_check")))
    if auto != bool(config.get_key(cfg, "update.auto_check")):
        config.set_key(cfg, "update.auto_check", auto)
        changed = True

    return changed


def _menu_user(cfg):
    changed = False
    ui.heading("User and paths")
    print("  LOS root      %s" % ui.color(paths.BASE_DIR, "grey"))
    print("  Software      %s" % ui.color(paths.SYS_DIR, "grey"))
    print("  Logs          %s" % ui.color(paths.LOG_DIR, "grey"))
    print("  State         %s" % ui.color(paths.VAR_DIR, "grey"))
    print()

    candidates = _known_users()
    if candidates:
        print("  Existing entities: %s" % ", ".join(candidates))

    user = ui.ask("Primary LOS user", cfg.get("user") or "")
    if user != cfg.get("user"):
        if not os.path.isdir(paths.user_dir(user)):
            ui.warn("%s does not exist" % paths.user_dir(user))
            ui.info("create it with `%s/los-user-manage create %s`"
                    % (paths.BIN_DIR, user))
            if not ui.ask_yes_no("Use it anyway?", False):
                return changed
        cfg["user"] = user
        changed = True

    return changed


def _menu_logging(cfg):
    """Log rotation, shared by every log under log/ and by aicall.log."""
    changed = False
    ui.heading("Logging")
    print(ui.color(
        "  Applies to every service log (gateway, ssl_server, action_mcp,\n"
        "  memory_mcp, loscron, whatsapp) and to aicall.log.  One setting\n"
        "  keeps them all behaving the same way.", "grey"))
    print()

    rotate = ui.ask_yes_no("Rotate logs when they grow too large?",
                           bool(config.get_key(cfg, "logging.rotate")))
    if rotate != bool(config.get_key(cfg, "logging.rotate")):
        config.set_key(cfg, "logging.rotate", rotate)
        changed = True

    if rotate:
        current_mb = config.get_key(cfg, "logging.max_bytes", 52428800) // (1024 * 1024)
        max_mb = ui.ask_int("Rotate at how many MB?", current_mb, minimum=1)
        max_bytes = max_mb * 1024 * 1024
        if max_bytes != config.get_key(cfg, "logging.max_bytes"):
            config.set_key(cfg, "logging.max_bytes", max_bytes)
            changed = True

        keep = ui.ask_int("How many rotated backups to keep?",
                          config.get_key(cfg, "logging.keep"), minimum=1)
        if keep != config.get_key(cfg, "logging.keep"):
            config.set_key(cfg, "logging.keep", keep)
            changed = True
    else:
        ui.info("rotation is off — log files will grow without limit")

    return changed


def _known_users():
    """Directories under /los that look like user entities."""
    found = []
    try:
        for name in sorted(os.listdir(paths.BASE_DIR)):
            if name == "sys" or name.startswith("."):
                continue
            path = os.path.join(paths.BASE_DIR, name)
            if os.path.isdir(path) and \
                    os.path.exists(os.path.join(path, "identity")):
                found.append(name)
    except OSError:
        pass
    return found


# ── Entry point ─────────────────────────────────────────────────────────

_ACTIONS = {
    "get": action_get,
    "set": action_set,
    "unset": action_unset,
    "list": action_list,
    "validate": action_validate,
    "edit": action_edit,
    "path": action_path,
}


def run(args):
    action = getattr(args, "action", None)
    if action:
        handler = _ACTIONS.get(action)
        if handler is None:
            ui.error("unknown action %r" % action)
            return 1
        return handler(args)

    # No sub-action: the interactive editor.
    if not config.exists():
        ui.warn("no configuration yet at %s" % paths.CONFIG_FILE)
        if not ui.ask_yes_no("Create one now?", True):
            ui.info("run `los install` to set up this machine")
            return 1
        cfg = config.defaults()
        config.ensure_identity(cfg)
    else:
        cfg = config.load()

    run_menu(cfg)
    return 0


def _restart_hint(key):
    """Tell the user when a change needs a restart to take effect."""
    for svc in registry.SERVICES:
        if key.startswith(svc.config_prefix):
            ui.info("run `los restart %s` to apply" % svc.name)
            return
