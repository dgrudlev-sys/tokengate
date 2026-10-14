#!/usr/bin/env sh
# tokengate one-line installer for macOS / Linux. Run inside your project folder:
#   curl -fsSL https://raw.githubusercontent.com/dgrudlev-sys/tokengate/main/scripts/install.sh | sh
set -e
PY=$(command -v python3 || command -v python) || { echo "Python 3.10+ is required"; exit 1; }
"$PY" -m pip install --upgrade "tokengate[mcp] @ git+https://github.com/dgrudlev-sys/tokengate.git"
"$PY" -m tokengate.cli init .
"$PY" -m tokengate.cli doctor --offline
