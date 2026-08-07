#!/usr/bin/env bash
# DOOM CLI launcher for Linux / macOS (run.bat is the Windows equivalent).
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
PIP=.venv/bin/pip

# A broken/partial venv (e.g. created without ensurepip so pip is missing)
# must be recreated, not reused.
if [ ! -x "$PY" ] || [ ! -x "$PIP" ]; then
    if [ -d .venv ]; then
        echo "Removing incomplete venv..."
        rm -rf .venv
    fi
    echo "Creating venv..."
    if ! python3 -m venv .venv; then
        echo "python3-venv is missing. On Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    fi
    "$PY" -m pip install --upgrade pip
fi

# Idempotent: fast no-op when everything is already installed.
"$PY" -m pip install -r requirements.txt

# System deps the engine/music need (Ubuntu package names). Warn, don't fail —
# the game still runs, just silently. ldconfig is Linux-only; macOS has an
# OpenAL framework built in, so that check is skipped there.
if command -v ldconfig >/dev/null 2>&1 \
        && ! ldconfig -p 2>/dev/null | grep -q "libopenal.so"; then
    echo "warning: OpenAL not found — sound effects won't play." >&2
    echo "         On Ubuntu: sudo apt install libopenal1" >&2
fi
if ! command -v fluidsynth >/dev/null 2>&1; then
    echo "warning: fluidsynth not found — music won't play." >&2
    echo "         On Ubuntu: sudo apt install fluidsynth fluid-soundfont-gm" >&2
    echo "         On macOS: brew install fluidsynth fluid-soundfont" >&2
fi

exec "$PY" -m doomcli.main "$@"
