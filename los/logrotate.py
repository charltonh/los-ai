"""Shared log rotation, used by the gateway and by aicall.py.

One policy applies to every log file LOS writes — the gateway's own
service logs under log/, and aicall.log — so there is a single place
that decides "is this log too big yet" and a single set of config keys
(logging.rotate / logging.max_bytes / logging.keep) that controls all of
them.

Two rotation styles are supported because the two callers had different
schemes before this module existed, and changing aicall's on-disk naming
would break anyone already tailing dated files:

    numbered(path)         path.1, path.2, ... path.N (gateway/service logs)
    dated(path, prefix)    prefix.YYYYMMDD.log, _2 suffix on collision
                           (aicall.log's historical scheme)
"""

import glob
import os
import shutil
from datetime import datetime


def settings(cfg):
    """(rotate: bool, max_bytes: int, keep: int) from a loaded config dict."""
    try:
        from . import config
        rotate = config.get_key(cfg, "logging.rotate", True)
        max_bytes = config.get_key(cfg, "logging.max_bytes", 52428800)
        keep = config.get_key(cfg, "logging.keep", 3)
    except Exception:
        rotate, max_bytes, keep = True, 52428800, 3
    try:
        max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        max_bytes = 52428800
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 3
    return bool(rotate), max_bytes, max(1, keep)


def numbered(path, max_bytes=52428800, keep=3, rotate=True):
    """Rotate path -> path.1 -> path.2 ... dropping anything past `keep`.

    Used for the gateway's own service logs (ssl_server.log, gateway.log,
    etc), which are appended to directly by the supervisor and don't go
    through Python's logging module.
    """
    if not rotate:
        return False
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return False

        # Drop the oldest kept backup, then shift path.(keep-1) -> path.keep,
        # ..., path.1 -> path.2, and finally path -> path.1.
        oldest = "%s.%d" % (path, keep)
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(keep - 1, 0, -1):
            older = "%s.%d" % (path, index)
            if os.path.exists(older):
                os.replace(older, "%s.%d" % (path, index + 1))
        os.replace(path, path + ".1")
        return True
    except OSError:
        return False


def numbered_live(path, max_bytes=52428800, keep=3, rotate=True):
    """Copy-and-truncate rotation for a file another process still has open.

    `numbered()` renames the file, which is fine right before a process
    starts (nothing has it open yet) but does nothing useful once a
    long-running service is already writing to it through its own file
    descriptor — the process keeps appending to the renamed file forever.
    This copies the bytes out to path.1 (shifting older numbered backups
    down first) and then truncates path in place, which every writer
    continues to see correctly.
    """
    if not rotate:
        return False
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return False

        oldest = "%s.%d" % (path, keep)
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(keep - 1, 0, -1):
            older = "%s.%d" % (path, index)
            if os.path.exists(older):
                os.replace(older, "%s.%d" % (path, index + 1))

        shutil.copy2(path, path + ".1")
        with open(path, "r+b") as fh:
            fh.truncate(0)
        return True
    except OSError:
        return False


def dated(path, max_bytes=52428800, keep=3, rotate=True):
    """Rotate path -> <stem>.YYYYMMDD.log (aicall.log's historical scheme).

    Older dated backups beyond `keep` are pruned, oldest first.  `keep`
    counts rotated files, not counting the live log.
    """
    if not rotate:
        return False
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < max_bytes:
            return False

        log_dir = os.path.dirname(path) or "."
        base = os.path.basename(path)
        stem = base[:-4] if base.endswith(".log") else base

        date_str = datetime.now().strftime("%Y%m%d")
        rotated = os.path.join(log_dir, "%s.%s.log" % (stem, date_str))
        suffix = 2
        while os.path.exists(rotated):
            rotated = os.path.join(log_dir, "%s.%s_%d.log"
                                   % (stem, date_str, suffix))
            suffix += 1
        os.rename(path, rotated)

        _prune_dated(log_dir, stem, keep)
        return True
    except OSError:
        return False


def _prune_dated(log_dir, stem, keep):
    """Keep only the `keep` most recent dated backups of stem.*.log."""
    try:
        pattern = os.path.join(glob.escape(log_dir), "%s.*.log" % stem)
        backups = [p for p in glob.glob(pattern)
                  if os.path.basename(p) != "%s.log" % stem]
        backups.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in backups[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass
