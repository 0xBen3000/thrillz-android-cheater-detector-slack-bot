#!/bin/bash
# Install Thrillz Bot Detector as a macOS launchd service
# Usage: ./install.sh <SLACK_WEBHOOK_URL>

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.thrillz.bot-detector"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

SLACK_WEBHOOK="${1:-}"

if [ -z "$SLACK_WEBHOOK" ]; then
    echo "Usage: ./install.sh <SLACK_WEBHOOK_URL>"
    echo ""
    echo "To get a webhook URL:"
    echo "  1. Go to https://api.slack.com/apps"
    echo "  2. Create app > From scratch > name it 'Bot Detector'"
    echo "  3. Incoming Webhooks > Activate > Add New Webhook"
    echo "  4. Select #android-anomalies channel"
    echo "  5. Copy the webhook URL"
    exit 1
fi

# Stop existing service if running
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# Create launchd plist
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${SCRIPT_DIR}/bot_detector.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SLACK_WEBHOOK_URL</key>
        <string>${SLACK_WEBHOOK}</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/stderr.log</string>
</dict>
</plist>
EOF

# Load and start
launchctl load "$PLIST_PATH"

echo "Bot Detector installed and started!"
echo "  Logs:    tail -f ${SCRIPT_DIR}/bot_detector.log"
echo "  Status:  launchctl list | grep bot-detector"
echo "  Stop:    launchctl unload ${PLIST_PATH}"
echo "  Restart: launchctl unload ${PLIST_PATH} && launchctl load ${PLIST_PATH}"
