import asyncio
import logging
import time
import traceback
from threading import Thread

import config
import pykeybasebot.types.chat1 as chat1
import redis
import requests
from pykeybasebot import Bot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
last_sent_alert = time.time()
last_check = int(time.time())

# Initialize Redis
redis_client = redis.Redis(
    host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=True
)


def parse_issue(issue_data: dict) -> dict:
    """Convert Redis issue data from string values to appropriate types."""
    return {
        "id": issue_data["id"],
        "resolved": bool(int(issue_data["resolved"])),
        "message": issue_data["message"],
        "started_at": int(issue_data["started_at"]),
        "last_alert": int(issue_data["last_alert"]),
        "alert_number": int(issue_data["alert_number"]),
    }


def fetch_issues() -> list:
    """Fetch all issues from Redis and convert values to appropriate types."""
    keys = redis_client.keys("issue:*")
    issues = []
    for key in keys:
        issue_data = redis_client.hgetall(key)
        if issue_data:
            issues.append(parse_issue(issue_data))
    return issues


def update_issue(issue_id: str, last_alert: int, alert_number: int) -> None:
    """Updates the last alert time and alert count for a specific issue in Redis."""
    redis_client.hset(
        f"issue:{issue_id}",
        mapping={"last_alert": last_alert, "alert_number": alert_number},
    )


def delete_issue(issue_id: str) -> None:
    """Deletes a specific issue from Redis."""
    redis_client.delete(f"issue:{issue_id}")


def update_health_status() -> None:
    """Update last check timestamp in Redis."""
    redis_client.set("health:alert_service", int(time.time()))


def how_long(ts: int) -> str:
    """Calculate and format a human-readable duration since the given timestamp."""
    duration = int(time.time() - ts)
    intervals = [(24 * 60 * 60, "day", "days"), (60 * 60, "hour", "hours")]
    for seconds, singular, plural in intervals:
        if duration >= seconds:
            count = duration // seconds
            unit = singular if count == 1 else plural
            return f"since {count} {unit} ago"
    return "since a few minutes ago"


class KeybaseBot:
    """Singleton wrapper for the Keybase bot instance."""

    _instance = None

    @staticmethod
    def get_instance():
        if KeybaseBot._instance is None:
            KeybaseBot._instance = Bot(
                username=config.KEYBASE_BOT_USERNAME,
                paperkey=config.KEYBASE_BOT_KEY,
                handler=None,
            )
        return KeybaseBot._instance


def send_alerts(message: str) -> bool:
    """Sends an alert via Keybase and Telegram."""
    global last_sent_alert
    keybase_sent = send_keybase_alert(message)
    telegram_sent = send_telegram_alert(message)
    if keybase_sent or telegram_sent:
        last_sent_alert = time.time()
    return keybase_sent or telegram_sent


def send_keybase_alert(message: str) -> bool:
    """Sends an alert via Keybase."""
    try:
        bot = KeybaseBot.get_instance()
        channel = chat1.ChatChannel(**config.KEYBASE_BOT_CHANNEL)
        asyncio.run(bot.chat.send(channel, message))
        return True
    except Exception as e:
        logging.error(f"Keybase error: {e}")
        logging.error(traceback.format_exc())
        return False


def send_telegram_alert(message: str) -> bool:
    """Sends an alert via Telegram."""
    try:
        request_data = {"chat_id": config.TELEGRAM_BOT_CHANNEL, "text": message}
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_KEY}/sendMessage"
        response = requests.post(
            url, json=request_data, headers={"Content-Type": "application/json"}
        )
        if response.status_code == 200:
            return True
        else:
            logging.error(f"Telegram API error: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Telegram error: {e}")
        return False


def handle_resolved_issue(issue: dict) -> None:
    """Handle resolved issues by sending a message and deleting them."""
    if send_alerts(issue["message"]):
        delete_issue(issue["id"])


def handle_first_alert_issue(issue: dict):
    """Handle new issues by sending a message."""
    if send_alerts(issue["message"]):
        update_issue(issue["id"], int(time.time()), issue["alert_number"] + 1)


def handle_unresolved_issue(issue: dict) -> None:
    """Handle unresolved issues by sending a message."""
    current_timestamp = int(time.time())
    next_interval = min(
        config.MIN_MSG_INTERVAL * 2 ** (issue["alert_number"] - 1),
        config.MAX_MSG_INTERVAL,
    )
    next_alert = issue["last_alert"] + next_interval
    if next_alert <= current_timestamp:
        message = f"{issue['message']}\n{how_long(issue['started_at'])}"
        if send_alerts(message):
            update_issue(issue["id"], current_timestamp, issue["alert_number"] + 1)


def handle_issue(issue: dict) -> None:
    """Check and process an issue."""
    if issue["resolved"]:
        handle_resolved_issue(issue)
    elif issue["last_alert"] == 0:
        handle_first_alert_issue(issue)
    else:
        handle_unresolved_issue(issue)


def main() -> None:
    """Main function to check and process all issues."""
    while True:
        try:
            issues = fetch_issues()
            for issue in issues:
                handle_issue(issue)
            if not issues and time.time() - last_sent_alert > 24 * 60 * 60:
                send_alerts("There wasn't any issue in the past 24 hours")
            update_health_status()
        except Exception as e:
            logging.error(f"Error in alert_service: {e}")
        time.sleep(config.CHECK_INTERVAL)


if __name__ == "__main__":
    logging.info("Starting Alert Service...")
    alert_thread = Thread(target=main)
    alert_thread.start()
