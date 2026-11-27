#!/usr/bin/env bash
# Mac / Linux / Raspberry Pi launcher.
set -euo pipefail
cd "$(dirname "$0")"
trap 'echo; echo "Stopped."; exit 0' INT TERM

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ is required. Mac: 'brew install python'. Debian/Raspberry Pi OS: 'sudo apt install python3 python3-venv'."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up for the first time..."
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

# Run setup on first launch, or if it was quit before choosing a server.
if [ ! -f .env ] || ! grep -q '^GUILD_IDS=[0-9]' .env; then
  .venv/bin/python -m admin_bot setup
fi

while true; do
  echo "Starting bot. Press Ctrl+C to stop."
  set +e
  .venv/bin/python -m admin_bot
  code=$?
  set -e
  [ "$code" -eq 2 ] && exit 2   # config error: don't loop
  echo "Bot stopped (exit $code). Restarting in 10 seconds... (Ctrl+C to quit)"
  sleep 10
done
