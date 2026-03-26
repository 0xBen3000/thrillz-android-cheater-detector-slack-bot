#!/usr/bin/env python3
"""
Thrillz Android Bot Detector
Polls Balance_History for new level-ups and flags anomalies.
Sends Slack alerts on first detection.
"""

import json
import os
import re
import sys
import time
import signal
import logging
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ─── CONFIG ──────────────────────────────────────────────────────────────────

API_URL = "https://thrillz-api-production.up.railway.app/query"
API_KEY = "thrillz-prod-2026"
DATABASE = "thrillz-dev-android"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL = "#android-anomalies"
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0AP6EG682D")  # #android-anomalies

POLL_INTERVAL = 30          # seconds between polls
MIN_LEVEL = 5               # only check level-ups above this
VELOCITY_THRESHOLD = 100    # games in 24h to flag
XP_RATIO_THRESHOLD = 3.0    # xp_per_game / 100 — flag if above this
NORMAL_XP_PER_GAME = 100    # baseline XP per game
BACKFILL_DAYS = 7           # days of history to backfill on fresh start

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

def rebuild_flagged_from_slack():
    """Read recent Slack messages to rebuild flagged_users set (survives redeploys)."""
    if not SLACK_BOT_TOKEN:
        log.info("No SLACK_BOT_TOKEN set — cannot rebuild from Slack, using backfill instead")
        return {}

    flagged = {}
    cursor = None
    try:
        while True:
            url = (
                f"https://slack.com/api/conversations.history"
                f"?channel={SLACK_CHANNEL_ID}&limit=200"
            )
            if cursor:
                url += f"&cursor={cursor}"

            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            if not data.get("ok"):
                log.warning(f"Slack API error: {data.get('error')}")
                break

            for msg in data.get("messages", []):
                text = msg.get("text", "")
                # Extract user IDs from alert messages (format: *User:* `uuid`)
                if "Bot Alert" in text or "bot_alert" in text.lower():
                    match = re.search(r'`([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`', text)
                    if match:
                        uid = match.group(1)
                        if uid not in flagged:
                            flagged[uid] = {
                                "first_detected": "rebuilt_from_slack",
                                "level_at_detection": 0,
                                "anomalies": ["rebuilt"],
                            }

            # Paginate
            if data.get("has_more") and data.get("response_metadata", {}).get("next_cursor"):
                cursor = data["response_metadata"]["next_cursor"]
            else:
                break

    except Exception as e:
        log.warning(f"Failed to rebuild from Slack: {e}")

    log.info(f"Rebuilt {len(flagged)} flagged users from Slack history")
    return flagged


def backfill_flagged_silent():
    """Dry-run recent level-ups to populate flagged_users without sending Slack alerts."""
    log.info(f"Backfilling last {BACKFILL_DAYS} days of level-ups (silent mode)...")
    flagged = {}

    rows = query_db(
        f"SELECT h.id, h.user_id, h.date_created, "
        f"u.level, u.exp, u.gamesPlayed "
        f"FROM Balance_History h "
        f"JOIN directus_users u ON h.user_id = u.id "
        f'WHERE h.type = "icon_level" AND h.currency = "gems" '
        f"AND h.date_created >= NOW() - INTERVAL {BACKFILL_DAYS} DAY "
        f"AND u.level > {MIN_LEVEL} "
        f"ORDER BY h.id ASC LIMIT 500"
    )

    if not rows:
        log.info("No recent level-ups to backfill")
        return flagged, 0

    max_id = 0
    seen_users = {}
    for row in rows:
        event_id = row.get("id", 0)
        if event_id > max_id:
            max_id = event_id

        uid = row.get("user_id")
        if not uid or uid in seen_users:
            continue

        # XP coherence check
        xp_result = check_xp_coherence_inline(row)
        if xp_result:
            seen_users[uid] = True
            flagged[uid] = {
                "first_detected": "backfill_silent",
                "level_at_detection": row.get("level", 0),
                "anomalies": ["xp_coherence"],
            }

    # Batch velocity check on unique users
    to_check = []
    for row in rows:
        uid = row.get("user_id")
        if uid and uid not in flagged and uid not in [i["user_id"] for i in to_check]:
            to_check.append({
                "user_id": uid,
                "level": row.get("level", 0),
                "lvlup_time": row.get("date_created"),
                "anomalies": [],
            })

    if to_check:
        vel_results = check_velocity_batch(to_check)
        for uid, vel in vel_results.items():
            flagged[uid] = {
                "first_detected": "backfill_silent",
                "level_at_detection": 0,
                "anomalies": ["velocity"],
            }

    log.info(f"Backfill: {len(flagged)} users would have been flagged (silent, no Slack)")
    return flagged, max_id


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            log.info(f"Loaded state: last_id={state['last_id']}, {len(state.get('flagged_users', {}))} flagged")
            return state

    log.info("No state file — rebuilding flagged_users...")

    # Strategy 1: Try to rebuild from Slack messages (best — real source of truth)
    flagged = rebuild_flagged_from_slack()

    # Strategy 2: Backfill from DB (silent dry-run)
    backfill_flagged, max_id = backfill_flagged_silent()

    # Merge both sources
    for uid, info in backfill_flagged.items():
        if uid not in flagged:
            flagged[uid] = info

    # Start from current max_id so we don't reprocess
    if not max_id:
        max_rows = query_db(
            "SELECT MAX(id) as max_id FROM Balance_History "
            "WHERE type = 'icon_level'"
        )
        if max_rows and max_rows[0].get("max_id"):
            max_id = int(max_rows[0]["max_id"])

    log.info(f"Starting fresh: last_id={max_id}, {len(flagged)} users pre-flagged")
    return {"last_id": max_id, "flagged_users": flagged}


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

def parse_games_played(gp):
    """Parse gamesPlayed JSON field into total game count."""
    if isinstance(gp, str):
        try:
            gp = json.loads(gp) if gp else []
        except json.JSONDecodeError:
            gp = []
    elif gp is None:
        gp = []
    return sum(g.get("numberOfGame", 0) for g in gp) if gp else 0


def check_xp_coherence_inline(row):
    """Check 1: XP coherence — runs in-memory from poll data, NO extra query."""
    exp = row.get("exp", 0) or 0
    total_games = parse_games_played(row.get("gamesPlayed"))

    if total_games == 0:
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


def check_velocity_batch(items):
    """Check 2: Velocity — ONE batched query for all users instead of N queries."""
    if not items:
        return {}

    # Find the earliest lvlup_time to set the window
    # We query games per user in the 24h before their respective level-up
    # Strategy: one query with all user_ids, broad 48h window, then filter in Python
    user_ids = [item["user_id"] for item in items]
    quoted_ids = ", ".join(f'"{uid}"' for uid in user_ids)

    # Use a broad window (last 48h from now) to capture all relevant scores
    rows = query_db(
        f"SELECT user_id, date_created FROM Scores "
        f"WHERE user_id IN ({quoted_ids}) "
        f"AND date_created >= NOW() - INTERVAL 48 HOUR"
    )

    if not rows:
        return {}

    # Build a lookup: user_id -> lvlup_time
    lvlup_times = {}
    for item in items:
        uid = item["user_id"]
        lt = item.get("lvlup_time", "")
        if lt:
            try:
                lvlup_times[uid] = datetime.fromisoformat(lt.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                lvlup_times[uid] = datetime.now(timezone.utc)
        else:
            lvlup_times[uid] = datetime.now(timezone.utc)

    # Count games in the 24h window before each user's level-up
    counts = defaultdict(int)
    for score in rows:
        uid = score.get("user_id")
        if uid not in lvlup_times:
            continue
        score_time_str = score.get("date_created", "")
        try:
            score_time = datetime.fromisoformat(score_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        lvl_time = lvlup_times[uid]
        if lvl_time - timedelta(hours=24) <= score_time <= lvl_time:
            counts[uid] += 1

    # Build results
    results = {}
    for uid, cnt in counts.items():
        if cnt > VELOCITY_THRESHOLD:
            results[uid] = {"type": "velocity", "games_24h": cnt}

    return results


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

    # Fetch new level-up events WITH user data (XP coherence in-memory, no extra query)
    rows = query_db(
        f"SELECT h.id, h.user_id, h.date_created, "
        f"u.level, u.exp, u.gamesPlayed "
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

    # Filter to users we haven't flagged yet — DEDUPLICATE within the batch
    to_check = {}  # user_id -> item (keep latest event per user)
    for row in rows:
        event_id = row.get("id", 0)
        if event_id > state["last_id"]:
            state["last_id"] = event_id

        user_id = row.get("user_id")
        if not user_id or user_id in state["flagged_users"]:
            continue
        if user_id in to_check:
            continue  # already queued in this batch

        # XP coherence check — done in-memory, no extra query
        anomalies = []
        xp_result = check_xp_coherence_inline(row)
        if xp_result:
            anomalies.append(xp_result)

        to_check[user_id] = {
            "user_id": user_id,
            "level": row.get("level", 0),
            "lvlup_time": row.get("date_created"),
            "anomalies": anomalies,
        }
    to_check = list(to_check.values())

    # Batch velocity check — ONE query for all users instead of N individual queries
    if to_check:
        velocity_results = check_velocity_batch(to_check)

        for item in to_check:
            user_id = item["user_id"]
            vel = velocity_results.get(user_id)
            if vel:
                item["anomalies"].append(vel)

            if item["anomalies"]:
                log.warning(
                    f"ANOMALY: user={user_id[:12]}... level={item['level']} "
                    f"flags={[a['type'] for a in item['anomalies']]}"
                )

                # Fetch user context — retry once on failure
                ctx = get_user_context(user_id)
                if ctx is None:
                    log.warning(f"Context fetch failed for {user_id[:12]}..., retrying...")
                    time.sleep(2)
                    ctx = get_user_context(user_id)
                if ctx is None:
                    log.error(f"Context fetch failed twice for {user_id[:12]}..., sending without context")

                # Send Slack alert
                plain, blocks = format_slack_alert(user_id, item["level"], item["anomalies"], ctx)
                if not send_slack(plain, blocks):
                    log.error(f"Slack send failed for {user_id[:12]}..., will NOT mark as flagged")
                    continue  # Don't mark as flagged — retry next cycle

                # Mark as flagged
                state["flagged_users"][user_id] = {
                    "first_detected": datetime.now(timezone.utc).isoformat(),
                    "level_at_detection": item["level"],
                    "anomalies": [a["type"] for a in item["anomalies"]],
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
