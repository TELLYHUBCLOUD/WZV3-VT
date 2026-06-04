#!/usr/bin/env bash
set -e

source /usr/src/app/.venv/bin/activate
uv pip install --python /usr/src/app/.venv/bin/python --no-cache-dir -r requirements.txt
python3 update.py
python3 -m bot
