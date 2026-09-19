# LOS — Layered Organization System

> A framework for organizing your interests in a harness for AI.

With the advent of AI, the way people live, work, and organize is changing.
LOS maps your interests in a way that represents *you*, so AIs can act in your
interest — and ultimately become more than the sum of our agendas.

Say goodbye to things falling between the cracks.

---

## The idea

You are not one thing. You're made up of competing interests — work, family,
side projects, the person you want to become — each with its own priorities.
Most tools flatten all of that into a single to-do list and hope for the best.

LOS takes a different approach: each part of your life becomes an **entity** with
its own identity, goals, rules, calendar, and tasks. Entities can have
sub-entities, each with its own weights and priorities — a hierarchy that
mirrors how you actually think.

Then it puts AI to work *inside* that hierarchy. A task can be delegated to the
right entity, an event can land on the right calendar, and a model can act on
behalf of the right interest — without your constant attention.

## Why "Layered"?

Every entity is a layer. Your root entity is *you*. Below it sit the things you
care about, and below those, the things they care about. Weights and priorities
flow up and down the tree. The result is a system that holds more nuance than
any flat list ever could — and that AI can navigate on your behalf.

## What it does

- **Web interface** — a secure SSL app with a calendar, to-do list, agenda
  tree, and file editor. Designed for a 4K monitor, adaptable to 1080p.
- **AI calling** — a model-agnostic engine that runs prompts per entity, with
  per-entity configuration, memory, and tool access.
- **MCP action server** — exposes tools (calendar, to-do, cron, files,
  WhatsApp, memory, history) to any LLM or agent that speaks MCP.
- **WhatsApp interface** — talk to your system from a messaging app; send
  tasks, ask questions, and get answers back in plain language.
- **Cron daemon** — a lightweight scheduler that runs date, cron, and
  interval-based entries across every entity's agenda.
- **Memory server** — stores and recalls conversations and context so your
  AIs remember what matters.

The framework is **model-agnostic**: bring your own LLM, your own agent, your
own hardware. It's designed to be self-maintaining — it should run and sustain
itself without your constant input, and light enough for a Raspberry Pi, a
cloud instance, or anything in between.

## Architecture

LOS is designed to live on its own always-on Linux machine — bare metal, a VM,
or a cloud instance. On Windows or macOS, run it inside a Linux VM.

Everything lives under `/los`:

```
/los
├── sys/          # the LOS software (this repository)
└── <username>/   # your agenda — one user per LOS install
```

Hosting under `/los` is intentional: it can sit on its own partition, encrypted
if you like, separate from the rest of the OS.

### The anatomy of an entity

```
<entity>/
├── identity    # who or what this entity is
├── goals       # what it's trying to accomplish
├── omni        # always-on rules — a system prompt for this entity
├── entities    # its sub-entities, one per line (with optional weights)
├── prompt      # the prompt the AI runs for this entity
└── data/
    ├── calendar/  # events, filed by year/month-day
    ├── todo/      # to-do and done items (JSON lines)
    ├── cron/      # scheduled entries (date / cron / cycle)
    ├── ref/       # reference files, notes, anything relevant
    └── project/   # per-task notes, history, and attachments
```

The skeleton above is copied for every new entity, so the structure is
consistent all the way down the tree.

## Installation

> **Status:** LOS is in active development. Contributions are welcome.

Decide where LOS should live.

LOS is designed to live in its own directory at the root level: /los/.
This gives flexibility for it to have its own filesystem or even encrypted filesystem.
LOS doesn't need to be installed as root, but you may need to create this directory as
root and give the ownership of the directory to the user you will be installing as.

```bash
sudo mkdir -p /los
sudo chown $USER:$(id -gn) /los
```

You may also want to create a symlink from your home directory to /los:

```bash
cd ~
ln -s /los los
cd los
```

Now that you are in the place where LOS should be installed, you may run the install script:

```bash
curl -fsSL https://raw.githubusercontent.com/charltonh/los-ai/master/install.sh | sh
```

The installer checks the machine, asks a handful of questions, shows the
licence, and then builds everything. It is safe to run again later: if
`/los/sys` is already a checkout it is brought up to date with the
repository's default branch, overwriting the code that is there —
`config.json`, `run/`, `log/` and `var/` are left alone. (To move an
installed system between releases, use `los update`.) Prefer to do it by
hand?

```bash
git clone https://github.com/charltonh/los-ai.git /los/sys
/los/sys/bin/los install
```

Make sure you have the python requirements from /los/sys/requirements.txt
installed on your linux machine:
Flask>=3.1.0
requests>=2.32.0

Add `/los/sys/bin` to your `$PATH` (the installer offers to show you how) so
`los` works from anywhere.

Then you can start the system:

```bash
los gateway
```

## The `los` command

One program runs and manages the whole system.

| Command | What it does |
| --- | --- |
| `los gateway` | Start every configured service and supervise them |
| `los status` | What is running, on which port, for how long |
| `los config` | Interactive configuration (`los configure` also works) |
| `los logs [service] -f` | Follow the logs |
| `los start` / `stop` / `restart` | Act on one service or all of them |
| `los doctor` | Check the installation and explain what's wrong |
| `los update` | Fetch and apply the latest release |
| `los rollback` | Return to the previous version |
| `los install` | Set up or repair this machine |

`los gateway` runs in the foreground and stops cleanly on Ctrl-C. It starts
services in dependency order, waits for each to answer before starting the
ones that need it, and restarts anything that dies. There is no dependency on
systemd or OpenRC — but `los install` can generate a unit or init script for
either if you want LOS to start at boot.

## Services

| Service | Path | Port | What it does |
| --- | --- | --- | --- |
| Web app | `ssl_server/app.py` | 14001 | The primary interface — calendar, to-do, agenda tree, file editor |
| AI caller | `aicall/aicall.py` | — | Runs prompts per entity; model-agnostic, with memory and tools |
| Action server | `aicall_mcp/action_server.py` | 5100 | MCP tool server (calendar, to-do, cron, files, WhatsApp, memory) |
| Memory server | `memory_mcp/memory_server.py` | 5102 | Conversation and context recall |
| Cron daemon | `loscron/loscron.py` | — | Scheduled entries across all entities |
| WhatsApp channel | `whatsapp_mcp/whatsapp_server.py` | 5101 | *Optional* — messaging interface to the system |

Every port, host and toggle above lives in `/los/sys/config.json` and can be
changed with `los config`. Each service still runs standalone if you start it
by hand; the configuration only overrides its built-in defaults.

The AI manager can be configured per entity — so privacy-sensitive parts of
your life never touch a model you don't trust, while the rest run on whatever
suits them best.

### Channels

Channels are optional ways to reach LOS from outside the web app. WhatsApp is
the first; more can be added without touching the gateway. It is **disabled by
default** — enable it with `los config` (Channels → WhatsApp), which walks
through the virtualenv, the browser session, your phone numbers and the
matching label in your `.config`.

### Log rotation

Every log under `log/` — the gateway's own log, each service's log, and
`aicall.log` — shares one rotation policy, set in `los config` (Logging) or
directly in `config.json` under `logging`:

```json
"logging": {
    "rotate": true,
    "max_bytes": 52428800,
    "keep": 3
}
```

`rotate` defaults to on; turn it off to let a log grow without limit.
`max_bytes` (default 50 MB) is the size at which a log rotates, and `keep` is
how many rotated backups are retained. Service logs rotate to `name.log.1`,
`name.log.2`, and so on; `aicall.log` keeps its historical dated naming
(`aicall.YYYYMMDD.log`) since that's easier to correlate with a particular
day when you're digging through history. Rotation happens for a log that's
actively being written to as well as one that isn't — a running service
never has to be restarted for its log to rotate.

## Layout

The git working tree holds code only, so an update can never disturb your
data or configuration:

```
/los
├── sys/              # the LOS software (this repository)
│   ├── config.json   # system configuration — los config writes here
│   ├── run/          # pid files
│   ├── log/          # service logs and aicall.log, rotated automatically
│   └── var/          # sessions, ids and other runtime state
└── <username>/       # your agenda — one user per LOS install
```

## Theory and jargon

Think of a store-bought agenda: a calendar, a to-do list, maybe some scratch
pages. A *complete* agenda also has a defined identity and defined goals.
That's what the `identity` and `goals` files are for.

And every entity can have a set of rules that always apply — guidelines, a
system prompt, anything that should shape every decision. That's `omni`.

In practice, nobody keeps their agenda perfectly up to date. That's fine —
the system is built to run and sustain itself without your constant attention.
Jot notes into a scratch file (the `ref/` directory is for exactly that), and
let the AI keep the rest honest.

## The bigger picture

LOS is a framework — agnostic and ambivalent to the model or agent. Today it
manages your agenda. Tomorrow it can be the framework your AIs use to interact
with each other, with cryptocurrency smart contracts, and with corporate and
legal entities — your own AI lawyers working on your behalf, without your
interaction.

The social contract we were handed at birth is broken. This is a new way of
doing things — one where your interests are the architecture, not an
afterthought.

## License

Dual-licensed under the [GPL-3.0](GPL-3.0.txt) or the Dynet Commercial License.
Commercial use above a threshold requires a commercial license — see
[LICENSE.txt](LICENSE.txt) for details.

---

**LOS — Layered Organization System.** Built by Charlton Harrison, doing
business as Dynet.
