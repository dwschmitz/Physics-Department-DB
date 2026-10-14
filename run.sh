#!/bin/sh
# Set up (first time only) and start the web interface. Mac / Linux.
# Usage: ./run.sh      (can be launched from any folder)
set -e
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Python 3 is required: https://www.python.org/downloads/"; exit 1; }

[ -d .venv ] || { echo "Creating virtual environment..."; "$PY" -m venv .venv; }
if ! cmp -s requirements.txt .venv/requirements.installed; then
    echo "Installing dependencies..."
    .venv/bin/python -m pip install --quiet -r requirements.txt
    cp requirements.txt .venv/requirements.installed
fi

.venv/bin/python webapp.py ensure-admin
echo "Starting at http://127.0.0.1:5000  (press Ctrl+C to stop)"
exec .venv/bin/python webapp.py run
