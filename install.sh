#!/bin/sh
# LOS bootstrap installer.
#
#   curl -fsSL https://raw.githubusercontent.com/charltonh/los-ai/master/install.sh | sh
#
# This script does the bare minimum: check the machine, clone the
# repository into /los/sys, and hand over to `los install`, which does
# the real work.  Keeping the logic in one place means installing and
# repairing follow exactly the same path.
#
# It does NOT need to run as root.  It does need /los to exist and to be
# owned by you — see the message below if it isn't.

set -eu

REPO_URL="${LOS_REPO_URL:-https://github.com/charltonh/los-ai.git}"
BASE_DIR="${LOS_BASE:-/los}"
SYS_DIR="$BASE_DIR/sys"

bold=''; red=''; green=''; grey=''; reset=''
if [ -t 1 ]; then
    bold="$(printf '\033[1m')"
    red="$(printf '\033[91m')"
    green="$(printf '\033[92m')"
    grey="$(printf '\033[90m')"
    reset="$(printf '\033[0m')"
fi

say()  { printf '%s::%s %s\n' "$grey" "$reset" "$1"; }
ok()   { printf '%s✓%s %s\n' "$green" "$reset" "$1"; }
die()  { printf '%s✗%s %s\n' "$red" "$reset" "$1" >&2; exit 1; }

# ── 1. Linux only ───────────────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || die "LOS runs on Linux only (this is $(uname -s)).
  On Windows or macOS, run LOS inside a Linux VM."

# ── 2. Prerequisites ────────────────────────────────────────────────────
for tool in git python3; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not installed."
done

python3 - <<'PY' || die "Python 3.8 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY

# ── 3. /los must exist and be ours ──────────────────────────────────────
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"

if [ ! -d "$BASE_DIR" ]; then
    printf '%s✗%s %s does not exist.\n\n' "$red" "$reset" "$BASE_DIR" >&2
    cat >&2 <<EOF
  LOS lives in its own directory at the filesystem root, so it can sit on
  its own partition — encrypted if you like — separate from the rest of
  the OS.  Creating it needs root, but nothing after that does.

      ${bold}sudo mkdir -p $BASE_DIR${reset}
      ${bold}sudo chown $USER_NAME:$GROUP_NAME $BASE_DIR${reset}

  Then run this installer again.
EOF
    exit 1
fi

if [ ! -w "$BASE_DIR" ]; then
    printf '%s✗%s %s is not writable by %s.\n\n' \
        "$red" "$reset" "$BASE_DIR" "$USER_NAME" >&2
    printf '      %ssudo chown -R %s:%s %s%s\n\n' \
        "$bold" "$USER_NAME" "$GROUP_NAME" "$BASE_DIR" "$reset" >&2
    exit 1
fi

ok "$BASE_DIR is writable by $USER_NAME"

# ── 4. Fetch the software ───────────────────────────────────────────────
if [ -d "$SYS_DIR/.git" ]; then
    # Re-running the installer is an update: put the latest code in place,
    # overwriting whatever is already there.  `los update` is how an
    # installed system moves between releases, but it checks out release
    # tags, so it never reaches the tip of a branch — which is exactly
    # what a re-run of this script is for.
    #
    # config.json, run/, log/ and var/ are not tracked by git, so they
    # survive untouched.
    say "updating the existing checkout in $SYS_DIR"
    git -C "$SYS_DIR" fetch --tags --prune origin \
        || die "could not fetch from $REPO_URL."

    # `los update` leaves the checkout detached on a tag, so ask git for
    # the remote's default branch rather than assuming we are on one.
    branch="$(git -C "$SYS_DIR" symbolic-ref --quiet --short \
              refs/remotes/origin/HEAD 2>/dev/null || true)"
    branch="${branch#origin/}"
    if [ -z "$branch" ] || ! git -C "$SYS_DIR" rev-parse --verify --quiet \
            "origin/$branch" >/dev/null 2>&1; then
        if git -C "$SYS_DIR" rev-parse --verify --quiet origin/main \
                >/dev/null 2>&1; then
            branch="main"
        else
            branch="master"
        fi
    fi

    git -C "$SYS_DIR" checkout --quiet --force --detach "origin/$branch" \
        || die "could not update $SYS_DIR — resolve it there by hand."
    ok "code at origin/$branch"
elif [ -d "$SYS_DIR" ] && [ -n "$(ls -A "$SYS_DIR" 2>/dev/null)" ]; then
    die "$SYS_DIR already exists and is not a git checkout.
  Move it aside, or run '$SYS_DIR/bin/los install' directly."
else
    say "cloning $REPO_URL into $SYS_DIR"
    git clone --quiet "$REPO_URL" "$SYS_DIR"
    ok "cloned"
fi

# ── 5. Hand over to the real installer ──────────────────────────────────
[ -x "$SYS_DIR/bin/los" ] || chmod +x "$SYS_DIR/bin/los" 2>/dev/null || true

# The setup asks questions, so it needs a keyboard.  When this script is
# piped in (`curl … | sh`) stdin is the script itself, already read to the
# end, so every prompt in `los install` would hit EOF and quietly take its
# default — including the licence, which would then look refused.  Hand
# the new process the terminal instead.  Testing with `[ -r /dev/tty ]`
# would not do: access() only reads the device node's permissions, so it
# reports a terminal even in a session that has none.
echo
if [ -t 0 ]; then
    exec "$SYS_DIR/bin/los" install "$@"
fi

# Nothing to ask when the caller passed --yes, so a headless run is fine.
for arg in "$@"; do
    case "$arg" in
        -y|--yes) exec "$SYS_DIR/bin/los" install "$@" ;;
    esac
done

if (exec </dev/tty) 2>/dev/null; then
    say "reading your answers from the terminal"
    exec "$SYS_DIR/bin/los" install "$@" </dev/tty
fi

printf '%s✗%s there is no terminal to ask the setup questions on.\n\n' \
    "$red" "$reset" >&2
cat >&2 <<EOF
  Run the installer yourself, in a terminal:

      ${bold}$SYS_DIR/bin/los install${reset}

  Or accept every default without being asked, which also accepts the
  licence:

      ${bold}$SYS_DIR/bin/los install --yes${reset}
EOF
exit 1
