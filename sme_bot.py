import requests
from datetime import datetime, timezone, timedelta
import time
import json
import os
import html
import hashlib
import logging
import tempfile

# Configure logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')

# File to store sent announcement IDs
SENT_FILE = "sent_announcements.json"

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

# Telegram rate limit: max 20 messages per minute to same chat
TELEGRAM_DELAY = 3  # seconds between messages
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_MAX_MSG_LEN = 4096

# NSE fetch settings
NSE_MAX_RETRIES = 3


def load_sent_announcements():
    """Load the list of already sent announcement IDs"""
    if os.path.exists(SENT_FILE):
        try:
            with open(SENT_FILE, 'r') as f:
                data = json.load(f)
                today = datetime.now(IST).strftime('%Y-%m-%d')
                if data.get('date') == today:
                    return set(data.get('sent_ids', []))
                else:
                    logger.info("New day detected - resetting sent announcements")
                    return set()
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Could not load tracking file: %s", e)
            return set()
    return set()


def save_sent_announcements(sent_ids):
    """Save the list of sent announcement IDs atomically"""
    data = {
        'date': datetime.now(IST).strftime('%Y-%m-%d'),
        'sent_ids': list(sent_ids)
    }
    # Write to temp file then atomically replace to prevent corruption
    dir_name = os.path.dirname(os.path.abspath(SENT_FILE))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, SENT_FILE)
    except Exception:
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def send_telegram(msg):
    """Send message to Telegram with retry on rate limit"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID not set")
        return False

    # Enforce Telegram message length limit
    if len(msg) > TELEGRAM_MAX_MSG_LEN:
        msg = msg[:TELEGRAM_MAX_MSG_LEN - 20] + "\n... (truncated)"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                data={
                    "chat_id": CHAT_ID,
                    "text": msg,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                },
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                if result.get('ok'):
                    return True
                else:
                    logger.error("Telegram API error: %s", result.get('description'))
                    return False
            elif response.status_code == 429:
                retry_after = 5
                try:
                    retry_after = response.json().get('parameters', {}).get('retry_after', 5)
                except Exception:
                    pass
                wait_time = max(retry_after, 2 ** attempt)
                logger.warning("Rate limited (429). Retry %d/%d after %ds...", attempt, TELEGRAM_MAX_RETRIES, wait_time)
                time.sleep(wait_time)
                continue
            else:
                logger.error("Telegram HTTP %d", response.status_code)
                return False

        except requests.exceptions.RequestException as e:
            logger.error("Telegram request error: %s", e)
            if attempt < TELEGRAM_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return False

    logger.error("Failed after %d retries", TELEGRAM_MAX_RETRIES)
    return False


def fetch_nse_sme_announcements():
    """Fetch NSE SME announcements. Returns list on success, None on failure."""
    api_url = "https://www.nseindia.com/api/corporate-announcements?index=sme"

    for attempt in range(1, NSE_MAX_RETRIES + 1):
        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            logger.info("Fetching NSE SME announcements (attempt %d/%d)...", attempt, NSE_MAX_RETRIES)

            # Get cookies first
            logger.info("Getting NSE cookies...")
            session.get("https://www.nseindia.com", timeout=10)
            time.sleep(2)

            # Fetch SME announcements
            logger.info("Fetching from SME endpoint...")
            response = session.get(api_url, timeout=15)

            logger.info("NSE response status: %d", response.status_code)

            if response.status_code == 200:
                data = response.json()

                if isinstance(data, list):
                    announcements = data
                elif isinstance(data, dict) and 'data' in data:
                    announcements = data['data']
                else:
                    announcements = []

                logger.info("Fetched %d SME announcements", len(announcements))
                return announcements
            elif response.status_code >= 500:
                logger.warning("NSE server error %d, will retry...", response.status_code)
            else:
                logger.error("NSE returned %d: %s", response.status_code, response.text[:200])
                return None

        except requests.exceptions.Timeout:
            logger.warning("NSE request timed out (attempt %d/%d)", attempt, NSE_MAX_RETRIES)
        except requests.exceptions.RequestException as e:
            logger.warning("NSE request error (attempt %d/%d): %s", attempt, NSE_MAX_RETRIES, e)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("NSE returned invalid JSON: %s", e)
            return None
        finally:
            session.close()

        if attempt < NSE_MAX_RETRIES:
            wait = 2 ** attempt
            logger.info("Waiting %ds before retry...", wait)
            time.sleep(wait)

    logger.error("Failed to fetch from NSE after %d attempts", NSE_MAX_RETRIES)
    return None


def get_announcement_id(ann):
    """Get a unique ID for an announcement. Uses seq_id if available, falls back to content hash."""
    seq_id = ann.get('seq_id')
    if seq_id is not None:
        return str(seq_id)

    # Fallback: hash of key fields
    symbol = ann.get('symbol', '')
    date = ann.get('an_dt', '')
    desc = ann.get('desc', '')[:50]
    fallback = hashlib.md5(f"{symbol}|{date}|{desc}".encode()).hexdigest()[:12]
    logger.warning("No seq_id for %s, using fallback ID: %s", symbol, fallback)
    return f"fallback_{fallback}"


def format_announcement(ann, index=None):
    """Format announcement for Telegram with proper HTML escaping"""
    symbol = html.escape(ann.get('symbol', 'N/A'))
    company = html.escape(ann.get('sm_name', 'N/A'))
    subject = html.escape(ann.get('desc', 'N/A'))
    date = html.escape(ann.get('an_dt', 'N/A'))
    attachment = ann.get('attchmntFile', '')

    if len(subject) > 100:
        subject = subject[:97] + "..."

    header = "🔔 <b>NSE SME ANNOUNCEMENT</b>" if index is None else f"🔔 <b>NEW SME ANNOUNCEMENT #{index}</b>"

    msg = f"""{header}

📌 <b>{symbol}</b> (SME)
🏢 {company}
📋 {subject}
📅 {date}"""

    if attachment:
        safe_url = html.escape(attachment, quote=True)
        msg += f"\n🔗 <a href='{safe_url}'>View Document</a>"

    msg += "\n━━━━━━━━━━━━━━━━"

    return msg


def main():
    """Main function"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN and CHAT_ID environment variables are required")
        exit(1)

    now = datetime.now(IST)

    logger.info("=" * 60)
    logger.info("NSE SME ANNOUNCEMENT BOT - %s", now.strftime('%d-%m-%Y %H:%M:%S IST'))
    logger.info("=" * 60)

    # Load previously sent announcements
    sent_ids = load_sent_announcements()
    is_first_run_today = len(sent_ids) == 0

    if is_first_run_today:
        logger.info("First run of the day - will send all SME announcements")
    else:
        logger.info("Incremental run - %d announcements already sent today", len(sent_ids))

    # Fetch SME announcements (returns None on failure, [] on empty)
    all_sme_announcements = fetch_nse_sme_announcements()

    if all_sme_announcements is None:
        error_msg = f"""❌ <b>NSE SME Bot Error</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST

Failed to fetch SME announcements from NSE.
Will retry on next run."""

        send_telegram(error_msg)
        logger.error("Failed to fetch SME announcements")
        return

    if not all_sme_announcements:
        logger.info("NSE returned 0 announcements (legitimate empty response)")
        if is_first_run_today:
            status_msg = f"""ℹ️ <b>NSE SME Bot Status</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST
📊 No SME announcements on NSE today"""

            send_telegram(status_msg)
        return

    logger.info("Total SME announcements from NSE: %d", len(all_sme_announcements))

    # Filter for new announcements only
    new_announcements = []
    for ann in all_sme_announcements:
        ann_id = get_announcement_id(ann)
        if ann_id not in sent_ids:
            new_announcements.append(ann)

    logger.info("Summary: total=%d, already_sent=%d, new=%d",
                len(all_sme_announcements), len(sent_ids), len(new_announcements))

    if not new_announcements:
        logger.info("No new SME announcements to send")

        if is_first_run_today:
            status_msg = f"""ℹ️ <b>NSE SME Bot Status</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST
📊 Total SME announcements: {len(all_sme_announcements)}
✅ No new announcements today"""

            send_telegram(status_msg)
        return

    # Send header
    logger.info("=" * 60)
    logger.info("SENDING TO TELEGRAM")
    logger.info("=" * 60)

    run_type = "FIRST RUN - ALL SME ANNOUNCEMENTS" if is_first_run_today else "INCREMENTAL SME UPDATE"

    header = f"""📢 <b>NSE {run_type}</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST
🏭 SME Platform
📊 New announcements: {len(new_announcements)}

Sending..."""

    send_telegram(header)
    time.sleep(TELEGRAM_DELAY)

    # Send new announcements
    successfully_sent = []

    for i, ann in enumerate(new_announcements, 1):
        logger.info("Sending %d/%d...", i, len(new_announcements))

        msg = format_announcement(ann, i)

        if send_telegram(msg):
            ann_id = get_announcement_id(ann)
            sent_ids.add(ann_id)
            successfully_sent.append(ann_id)
            logger.info("Sent %d/%d", i, len(new_announcements))
        else:
            logger.error("Failed to send %d/%d", i, len(new_announcements))

        # Save after each successful send for crash resilience
        if successfully_sent and i % 5 == 0:
            save_sent_announcements(sent_ids)

        time.sleep(TELEGRAM_DELAY)

    # Final save
    save_sent_announcements(sent_ids)

    # Summary
    summary = f"""✅ <b>COMPLETE</b>

📤 New SME announcements sent: {len(successfully_sent)}
📊 Total SME sent today: {len(sent_ids)}
⏰ {now.strftime('%H:%M:%S')} IST"""

    send_telegram(summary)

    logger.info("=" * 60)
    logger.info("DONE - Sent %d SME announcements", len(successfully_sent))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
