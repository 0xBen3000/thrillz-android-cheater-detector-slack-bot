#!/usr/bin/env python3
"""
Thrillz Android Bot Detector
Polls Balance_History for new level-ups and flags anomalies.
Sends Slack alerts on first detection.
"""

import json
import os
import sys
import time
import signal
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ─── CONFIG ──────────────────────────────────────────────────────────────────

API_URL = "https://thrillz-api-production.up.railway.app/query"
API_KEY = "thrillz-prod-2026"
DATABASE = "thrillz-dev-android"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL = "#android-anomalies"

POLL_INTERVAL = 30          # seconds between polls
MIN_LEVEL = 5               # only check level-ups above this
VELOCITY_THRESHOLD = 100    # games in 24h to flag
XP_RATIO_THRESHOLD = 3.0    # xp_per_game / 100 — flag if above this
NORMAL_XP_PER_GAME = 100    # baseline XP per game

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_detector.log")

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bot_detector")

# ─── STATE ───────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_id": 0, "flagged_users": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── DB QUERY ────────────────────────────────────────────────────────────────

def query_db(sql):
    payload = json.dumps({"sql": sql, "database": DATABASE}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "X-Api-Key": API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                log.error(f"SQL error: {data['error']}")
                return None
            return data.get("rows", [])
    except Exception as e:
        log.error(f"DB query failed: {e}")
        return None


# ─── SLACK ───────────────────────────────────────────────────────────────────

def send_slack(text, blocks=None):
    if not SLACK_WEBHOOK_URL:
        log.warning(f"No SLACK_WEBHOOK_URL set. Message: {text}")
        return False

    payload = {"text": text, "channel": SLACK_CHANNEL}
    if blocks:
        payload["blocks"] = blocks

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.error(f"Slack send failed: {e}")
        return False


def format_slack_alert(user_id, level, anomalies, ctx=None):
    """Build a Slack Block Kit message for an anomaly alert."""
    flags = []
    for a in anomalies:
        if a["type"] == "xp_coherence":
            flags.append(
                f":warning: *XP Coherence* — {a['real_games']} games played, "
                f"{a['xp_per_game']} XP/game (normal ~100). "
                f"Ratio: *{a['ratio']}x*"
            )
        elif a["type"] == "velocity":
            flags.append(
                f":rocket: *Velocity* — *{a['games_24h']} games* in last 24h "
                f"(threshold: {VELOCITY_THRESHOLD})"
            )

    flag_text = "\n".join(flags)

    # User context block
    ctx_text = ""
    if ctx:
        blocked_str = ":no_entry: Blocked" if ctx["blocked"] else ":white_check_mark: Not Blocked"
        ctx_text = (
            f"\n\n"
            f"*Total Games:* {ctx['total_games']:,}  |  "
            f"*Avg/day:* {ctx['avg_games_per_day']}\n"
            f"*Total Ads:* {ctx['total_ads']:,}  |  "
            f"*Avg eCPM (last 10):* ${ctx['avg_ecpm']:.2f}\n"
            f"*Ads Revenue:* ${ctx['ads_revenue']:.2f}  |  "
            f"*Gems:* {ctx['gems']:,}\n"
            f"*Total Withdrawn:* ${ctx['total_withdrawn']:.2f}  |  "
            f"{blocked_str}"
        )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Bot Alert — Level {level}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*User:* `{user_id}`\n*Name:* {ctx['name'] if ctx else 'Unknown'}\n*Level:* {level}\n\n{flag_text}{ctx_text}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Thrillz Android Bot Detector • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                }
            ],
        },
    ]

    plain = f"Bot Alert: User {user_id[:12]}... reached level {level}. {'; '.join(a['type'] for a in anomalies)}"
    return plain, blocks


# ─── USER CONTEXT ────────────────────────────────────────────────────────────

def get_user_context(user_id):
    """Fetch enriched user data for the Slack alert."""
    # User profile
    rows = query_db(
        f"SELECT level, exp, gamesPlayed, date_created, "
        f"pseudo, email, "
        f"numberOfInterstitialWatched, ads_usd_generated_total, "
        f"gems, totalWithdrawAmount, "
        f"blocked, softBlock "
        f'FROM directus_users WHERE id = "{user_id}"'
    )
    if not rows:
        return None

    user = rows[0]

    # Parse gamesPlayed JSON
    gp = user.get("gamesPlayed")
    if isinstance(gp, str):
        try:
            gp = json.loads(gp) if gp else []
        except json.JSONDecodeError:
            gp = []
    elif gp is None:
        gp = []
    total_games = sum(g.get("numberOfGame", 0) for g in gp) if gp else 0

    # Days since creation
    created = user.get("date_created", "")
    days = 1
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            days = max((datetime.now(timezone.utc) - dt).days, 1)
        except (ValueError, TypeError):
            pass

    # Average eCPM on last 10 ads
    ecpm_rows = query_db(
        f"SELECT AVG(revenue) * 1000 as avg_ecpm FROM "
        f"(SELECT revenue FROM AdsWatched WHERE user = \"{user_id}\" "
        f"ORDER BY id DESC LIMIT 10) t"
    )
    avg_ecpm = 0
    if ecpm_rows and ecpm_rows[0].get("avg_ecpm"):
        try:
            avg_ecpm = float(ecpm_rows[0]["avg_ecpm"])
        except (TypeError, ValueError):
            pass

    blocked = user.get("blocked", 0) or 0
    soft_block = user.get("softBlock", 0) or 0
    is_blocked = int(blocked) > 0 or int(soft_block) > 0

    name = user.get("pseudo") or user.get("email") or "Unknown"

    return {
        "name": name,
        "total_games": total_games,
        "avg_games_per_day": round(total_games / days, 1),
        "total_ads": user.get("numberOfInterstitialWatched", 0) or 0,
        "ads_revenue": float(user.get("ads_usd_generated_total", 0) or 0),
        "avg_ecpm": round(avg_ecpm, 2),
        "gems": int(user.get("gems", 0) or 0),
        "total_withdrawn": float(user.get("totalWithdrawAmount", 0) or 0),
        "blocked": is_blocked,
        "days_active": days,
    }


# ─── CHECKS ──────────────────────────────────────────────────────────────────

def check_xp_coherence(user_id):
    """Check 1: Does the user's XP make sense relative to games played?"""
    rows = query_db(
        f'SELECT level, exp, gamesPlayed FROM directus_users WHERE id = "{user_id}"'
    )
    if not rows:
        return None

    user = rows[0]
    level = user.get("level", 0)
    exp = user.get("exp", 0)

    # Parse gamesPlayed JSON
    gp = user.get("gamesPlayed")
    if isinstance(gp, str):
        try:
            gp = json.loads(gp) if gp else []
        except json.JSONDecodeError:
            gp = []
    elif gp is None:
        gp = []

    total_games = sum(g.get("numberOfGame", 0) for g in gp) if gp else 0

    if total_games == 0:
        # No games but has XP > level 5 threshold = suspicious
        if exp > 1500:
            return {
                "type": "xp_coherence",
                "real_games": 0,
                "xp_per_game": exp,
                "ratio": round(exp / NORMAL_XP_PER_GAME, 1),
            }
        return None

    xp_per_game = exp / total_games
    ratio = xp_per_game / NORMAL_XP_PER_GAME

    if ratio > XP_RATIO_THRESHOLD:
        return {
            "type": "xp_coherence",
            "real_games": total_games,
            "xp_per_game": round(xp_per_game),
            "ratio": round(ratio, 1),
        }
    return None


def check_velocity(user_id, lvlup_time=None):
    """Check 2: Did the user play >100 games in the 24 hours before the level-up?"""
    if lvlup_time:
        # Use the level-up timestamp as reference point
        rows = query_db(
            f"SELECT COUNT(*) as cnt FROM Scores "
            f'WHERE user_id = "{user_id}" '
            f'AND date_created BETWEEN DATE_SUB("{lvlup_time}", INTERVAL 24 HOUR) AND "{lvlup_time}"'
        )
    else:
        rows = query_db(
            f"SELECT COUNT(*) as cnt FROM Scores "
            f'WHERE user_id = "{user_id}" '
            f"AND date_created >= NOW() - INTERVAL 24 HOUR"
        )
    if not rows:
        return None

    games_24h = rows[0].get("cnt", 0)
    if games_24h > VELOCITY_THRESHOLD:
        return {"type": "velocity", "games_24h": games_24h}
    return None


# ─── MAIN LOOP ───────────────────────────────────────────────────────────────

running = True


def shutdown(sig, frame):
    global running
    log.info("Shutting down...")
    running = False


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


def poll_once(state):
    """Check for new level-ups since last_id and analyze them."""
    last_id = state["last_id"]

    # Fetch new level-up events (gems currency = 1 per actual level-up)
    rows = query_db(
        f"SELECT h.id, h.user_id, h.date_created, u.level "
        f"FROM Balance_History h "
        f"JOIN directus_users u ON h.user_id = u.id "
        f'WHERE h.type = "icon_level" AND h.currency = "gems" '
        f"AND h.id > {last_id} AND u.level > {MIN_LEVEL} "
        f"ORDER BY h.id ASC LIMIT 50"
    )

    if rows is None:
        return  # query error, retry next cycle

    if not rows:
        return  # no new events

    log.info(f"Processing {len(rows)} new level-up events")

    for row in rows:
        user_id = row.get("user_id")
        event_id = row.get("id", 0)
        level = row.get("level", 0)
        lvlup_time = row.get("date_created")

        # Update last_id
        if event_id > state["last_id"]:
            state["last_id"] = event_id

        if not user_id:
            continue

        # Skip already flagged users
        if user_id in state["flagged_users"]:
            continue

        # Run checks
        anomalies = []

        xp_result = check_xp_coherence(user_id)
        if xp_result:
            anomalies.append(xp_result)

        vel_result = check_velocity(user_id, lvlup_time)
        if vel_result:
            anomalies.append(vel_result)

        if anomalies:
            log.warning(
                f"ANOMALY: user={user_id[:12]}... level={level} "
                f"flags={[a['type'] for a in anomalies]}"
            )

            # Fetch user context for enriched alert
            ctx = get_user_context(user_id)

            # Send Slack alert
            plain, blocks = format_slack_alert(user_id, level, anomalies, ctx)
            send_slack(plain, blocks)

            # Mark as flagged
            state["flagged_users"][user_id] = {
                "first_detected": datetime.now(timezone.utc).isoformat(),
                "level_at_detection": level,
                "anomalies": [a["type"] for a in anomalies],
            }

    save_state(state)


def main():
    log.info("=== Thrillz Bot Detector started ===")
    log.info(f"Polling every {POLL_INTERVAL}s | Min level: {MIN_LEVEL}")
    log.info(f"Velocity threshold: {VELOCITY_THRESHOLD} games/24h")
    log.info(f"XP coherence threshold: {XP_RATIO_THRESHOLD}x normal")
    log.info(f"Slack webhook: {'configured' if SLACK_WEBHOOK_URL else 'NOT SET'}")

    state = load_state()
    log.info(f"Resuming from Balance_History id={state['last_id']}, {len(state['flagged_users'])} users already flagged")

    while running:
        try:
            poll_once(state)
        except Exception as e:
            log.error(f"Poll error: {e}", exc_info=True)

        # Sleep in small increments so we can catch SIGTERM
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    save_state(state)
    log.info("Bot Detector stopped.")


if __name__ == "__main__":
    main()
