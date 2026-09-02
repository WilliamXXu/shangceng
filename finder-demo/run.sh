#!/usr/bin/env bash
# Entrance for the NiceGUI Finder demo: bootstraps the venv if needed, then
# serves http://localhost:8765 (opening it in your browser on macOS).
set -euo pipefail

cd "$(dirname "$0")"

PORT=8765
APP="app.py"
VENV=".venv"

# Stop a previous instance of this demo if one is still bound to the port.
if [ -n "$(lsof -ti :"$PORT" 2>/dev/null)" ]; then
  echo "Port $PORT is busy — stopping the previous finder-demo instance…"
  lsof -ti :"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi

# Create the virtualenv on first run.
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv…"
  python3 -m venv "$VENV"
fi

# Install/refresh dependencies (a no-op when they are already current).
"$VENV/bin/pip" install -q -r requirements.txt

# Open the app in the browser once the server answers (macOS only).
(
  for _ in $(seq 1 30); do
    if curl -s -o /dev/null "http://localhost:$PORT"; then
      open "http://localhost:$PORT" 2>/dev/null || true
      break
    fi
    sleep 0.5
  done
) &

echo "Starting finder-demo on http://localhost:$PORT …"
exec "$VENV/bin/python" "$APP"
