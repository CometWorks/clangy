#!/bin/bash
# Copies this working copy to the bot's host, installs the dependencies and restarts the service if it runs.
set -euo pipefail
HOST="${1:-clangy@nas}"
cd "$(dirname "$0")/.."
rsync -a --delete \
    --exclude .git/ --exclude .venv/ --exclude .idea/ --exclude __pycache__/ --exclude .pytest_cache/ \
    --exclude /data/ --exclude /config.toml \
    ./ "$HOST:clangy/"
ssh "$HOST" 'set -e
cd ~/clangy
~/.local/bin/uv sync --frozen --python /usr/bin/python3 --quiet
systemctl --user daemon-reload
systemctl --user enable --quiet ~/clangy/deploy/clangy.service
systemctl --user try-restart clangy.service
systemctl --user --no-pager status clangy.service | head -5 || true'
