#!/bin/bash
set -e

CONFIG_DIR="${HOME}/.tunarr"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "No config found. Writing default config to ${CONFIG_FILE}..."
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" << 'EOF'
server:
  host: "0.0.0.0"
  port: 8000
  log_level: "info"
  log_file: "~/.tunarr/logs/scheduler.log"
database:
  url: "sqlite+aiosqlite:///~/.tunarr/scheduler.db"
jellyfin:
  url: "http://jellyfin:8096"
  api_key: "YOUR_JELLYFIN_API_KEY"
  user_id: "YOUR_JELLYFIN_USER_ID"
  sync_interval_minutes: 15
tunarr:
  url: "http://tunarr:8000"
plugins:
  directories:
    - "~/.tunarr/plugins"
  disabled: []
auth:
  password_hash: ""
  session_secret: "YOUR_SESSION_SECRET"
channels: []
EOF
    echo ""
    echo "============================================================"
    echo " Default config written to ${CONFIG_FILE}"
    echo ""
    echo " Open the web UI and complete initial setup:"
    echo "   - admin password"
    echo "   - Jellyfin URL, API key, and user ID"
    echo "   - Tunarr URL"
    echo ""
    echo " The scheduler will keep running so setup is available through Traefik."
    echo "============================================================"
fi

echo "Config found. Starting scheduler..."
exec python -m tunarr_autoscheduler.main
