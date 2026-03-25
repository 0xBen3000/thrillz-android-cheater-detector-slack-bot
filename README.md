# Thrillz Android Cheater Detector

Daemon that monitors Thrillz Android level-ups in real-time and sends Slack alerts when anomalies are detected.

## Detection Checks

| Check | What it does | Threshold |
|---|---|---|
| **XP Coherence** | Compares XP to total games played (from `gamesPlayed` JSON). Normal is ~100 XP/game. | Flags if ratio > 3x normal |
| **Velocity** | Counts games in the 24h window before each level-up (from `Scores` table). | Flags if > 100 games/24h |

## How it works

1. Polls `Balance_History` every 30s for new `icon_level` events (level > 5)
2. For each level-up, runs both checks against the user
3. On first anomaly detection per user, sends a Slack Block Kit alert to `#android-anomalies`
4. Persists state in `state.json` (last processed ID + flagged users)

## Setup

### 1. Create a Slack Webhook

1. Go to [Slack Apps](https://api.slack.com/apps) > Create New App > From scratch
2. **Incoming Webhooks** > Activate > Add New Webhook to Workspace
3. Select `#android-anomalies` > Copy the webhook URL

### 2. Run directly

```bash
SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." python3 bot_detector.py
```

### 3. Install as macOS service (persistent)

```bash
chmod +x install.sh
./install.sh https://hooks.slack.com/services/...
```

This creates a `launchd` service that:
- Starts automatically on login
- Restarts on crash
- Logs to `bot_detector.log`

### Service management

```bash
# View logs
tail -f bot_detector.log

# Check status
launchctl list | grep bot-detector

# Stop
launchctl unload ~/Library/LaunchAgents/com.thrillz.bot-detector.plist

# Restart
launchctl unload ~/Library/LaunchAgents/com.thrillz.bot-detector.plist
launchctl load ~/Library/LaunchAgents/com.thrillz.bot-detector.plist
```

## Configuration

All config is at the top of `bot_detector.py`:

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL` | 30 | Seconds between polls |
| `MIN_LEVEL` | 5 | Ignore level-ups below this |
| `VELOCITY_THRESHOLD` | 100 | Games/24h to trigger flag |
| `XP_RATIO_THRESHOLD` | 3.0 | XP/game ratio multiplier to trigger flag |

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Yes | Slack incoming webhook URL |
| `THRILLZ_API_KEY` | No | API key (defaults to built-in) |
