"""LOS — Layered Organization System.

The `los` command: a single entry point that runs and manages the whole
system.  Deliberately stdlib-only, so it can bootstrap an installation
before any virtualenv exists.
"""

__version__ = "1.0.2"

# Upstream repository used by `los update`.
REPO = "charltonh/los-ai"
REPO_URL = "https://github.com/charltonh/los-ai.git"
