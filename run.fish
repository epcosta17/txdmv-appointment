#!/usr/bin/env fish
# TxDMV Appointment Desk - arranque local en macOS/Linux.

cd (dirname (status --current-filename))

if not test -d .venv
    echo "Preparando el entorno por primera vez..."
    python3 -m venv .venv; or exit 1
    ./.venv/bin/python -m pip install --quiet --upgrade pip
    ./.venv/bin/python -m pip install --quiet -r requirements.txt; or exit 1
end

echo "Appointment Desk -> http://127.0.0.1:8765/"
exec ./.venv/bin/python app.py $argv
