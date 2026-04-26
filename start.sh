#!/usr/bin/env bash
# Music CN Tagger — Linux / macOS launcher
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "Music CN Tagger"
echo "URL: http://localhost:5174"
echo
exec "$PY" -X utf8 app.py
