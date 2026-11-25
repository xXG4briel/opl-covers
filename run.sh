#!/usr/bin/env bash
# Launches OPL Covers. On first run it creates the venv (.venv) and installs requirements.txt.
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$DIR"
VENV="$DIR/.venv"

if [ ! -x "$VENV/bin/python" ]; then
    PY=""
    for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter, venv, ensurepip" >/dev/null 2>&1; then
            PY="$c"; break
        fi
    done
    if [ -z "$PY" ]; then
        echo "Could not find a Python 3 with tkinter and venv."
        echo "On Ubuntu/Mint: sudo apt install python3-tk python3-venv"
        exit 1
    fi
    echo "Creating virtual environment with $("$PY" --version)..."
    "$PY" -m venv "$VENV"
    rm -f "$VENV/.deps-ok"
fi

# Reinstall dependencies whenever requirements.txt changes.
if [ ! -f "$VENV/.deps-ok" ] || [ requirements.txt -nt "$VENV/.deps-ok" ]; then
    echo "Installing dependencies..."
    "$VENV/bin/python" -m pip install -q --upgrade pip
    "$VENV/bin/python" -m pip install -q -r requirements.txt
    touch "$VENV/.deps-ok"
fi

exec "$VENV/bin/python" "$DIR/opl_covers.py" "$@"
