import requests
from datetime import datetime
import time
import json
import os

BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')

# File to store sent announcement IDs
SENT_FILE = "sent_announcements.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

# Telegram rate limit: max 20 messages per minute to same chat
TELEGRAM_DELAY = 3  # seconds between messages
TELEGRAM_MAX_RETRIES = 3


def load_sent_announcements():
    """Load the list of already sent announcement IDs"""
    if os.path.exists(SENT_FILE):
        try:
            with open(SENT_FILE, 'r') as f:
                data = json.load(f)
                # Check if it's today's data
                if data.get('date') == datetime.now().strftime('%Y-%m-%d'):
                    return set(data.get('sent_ids', []))
                else:
                    # New day, reset the list
                    print("New day detected - resetting sent announcements")
                    return set()
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load tracking file: {e}")
            return set()
    return set()


def save_sent_announcements(sent_ids):
    """Save the list of sent announcement IDs"""
    data = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'sent_ids': list(sent_ids)
    }
    with open(SENT_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def send_telegram(msg):
    """Send message to Telegram with retry on rate limit"""
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: BOT_TOKEN or CHAT_ID not set")
        return False

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
                    print(f"Failed: {result.get('description')}")
                    return False
            elif response.status_code == 429:
                # Rate limited - extract retry_after or use exponential backoff
                retry_after = 5
                try:
                    retry_after = response.json().get('parameters', {}).get('retry_after', 5)
                except Exception:
                    pass
                wait_time = max(retry_after, 2 ** attempt)
                print(f"Rate limited (429). Retry {attempt}/{TELEGRAM_MAX_RETRIES} after {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"HTTP {response.status_code}")
                return False

        except Exception as e:
            print(f"Error: {e}")
            if attempt < TELEGRAM_MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return False

    print(f"Failed after {TELEGRAM_MAX_RETRIES} retries")
    return False


def fetch_nse_sme_announcements():
    """Fetch NSE SME announcements directly using index=sme"""
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        print("\nFetching NSE SME announcements...")

        # Get cookies
        print("1. Getting NSE cookies...")
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(2)

        # Fetch SME announcements using index=sme
        print("2. Fetching from SME endpoint...")
        url = "https://www.nseindia.com/api/corporate-announcements?index=sme"
        response = session.get(url, timeout=15)

        print(f"   Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()

            # Handle different response formats
            if isinstance(data, list):
                announcements = data
            elif isinstance(data, dict) and 'data' in data:
                announcements = data['data']
            else:
                announcements = []

            print(f"Fetched {len(announcements)} SME announcements")
            return announcements
        else:
            print(f"Failed to fetch announcements: {response.text[:200]}")
            return []

    except Exception as e:
        print(f"Error: {e}")
        return []


def format_announcement(ann, index=None):
    """Format announcement for Telegram"""
    symbol = ann.get('symbol', 'N/A')
    company = ann.get('sm_name', 'N/A')
    subject = ann.get('desc', 'N/A')
    date = ann.get('an_dt', 'N/A')
    attachment = ann.get('attchmntFile', '')

    # Shorten subject if too long
    if len(subject) > 100:
        subject = subject[:97] + "..."

    header = f"🔔 <b>NSE SME ANNOUNCEMENT</b>" if index is None else f"🔔 <b>NEW SME ANNOUNCEMENT #{index}</b>"

    msg = f"""{header}

📌 <b>{symbol}</b> (SME)
🏢 {company}
📋 {subject}
📅 {date}"""

    if attachment:
        msg += f"\n🔗 <a href='{attachment}'>View Document</a>"

    msg += "\n━━━━━━━━━━━━━━━━"

    return msg


def main():
    """Main function"""
    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: BOT_TOKEN and CHAT_ID environment variables are required")
        exit(1)

    now = datetime.now()

    print("\n" + "="*60)
    print(f"NSE SME ANNOUNCEMENT BOT - {now.strftime('%d-%m-%Y %H:%M:%S')}")
    print("="*60)

    # Load previously sent announcements
    sent_ids = load_sent_announcements()
    is_first_run_today = len(sent_ids) == 0

    if is_first_run_today:
        print("First run of the day - will send all SME announcements")
    else:
        print(f"Incremental run - {len(sent_ids)} announcements already sent today")

    # Fetch SME announcements
    all_sme_announcements = fetch_nse_sme_announcements()

    if not all_sme_announcements:
        error_msg = f"""❌ <b>NSE SME Bot Error</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')}

Failed to fetch SME announcements from NSE.
Will retry on next run."""

        send_telegram(error_msg)
        print("\nNo SME announcements fetched")
        return

    print(f"\nTotal SME announcements from NSE: {len(all_sme_announcements)}")

    # Filter for new announcements only
    new_announcements = []
    for ann in all_sme_announcements:
        # Use seq_id as unique identifier, convert to string for consistent comparison
        ann_id = ann.get('seq_id')
        if ann_id is not None:
            ann_id = str(ann_id)
            if ann_id not in sent_ids:
                new_announcements.append(ann)

    print(f"\nSummary:")
    print(f"   Total SME announcements: {len(all_sme_announcements)}")
    print(f"   Already sent today: {len(sent_ids)}")
    print(f"   New announcements: {len(new_announcements)}")

    if not new_announcements:
        print("\nNo new SME announcements to send")

        # Send status update only on first run
        if is_first_run_today:
            status_msg = f"""ℹ️ <b>NSE SME Bot Status</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')}
📊 Total SME announcements: {len(all_sme_announcements)}
✅ No new announcements today"""

            send_telegram(status_msg)
        return

    # Send header
    print("\n" + "="*60)
    print("SENDING TO TELEGRAM")
    print("="*60)

    run_type = "FIRST RUN - ALL SME ANNOUNCEMENTS" if is_first_run_today else "INCREMENTAL SME UPDATE"

    header = f"""📢 <b>NSE {run_type}</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')}
🏭 SME Platform
📊 New announcements: {len(new_announcements)}

Sending..."""

    send_telegram(header)
    time.sleep(TELEGRAM_DELAY)

    # Send new announcements
    successfully_sent = []

    for i, ann in enumerate(new_announcements, 1):
        print(f"\nSending {i}/{len(new_announcements)}...")

        msg = format_announcement(ann, i)

        if send_telegram(msg):
            # Mark as sent
            ann_id = ann.get('seq_id')
            if ann_id is not None:
                ann_id = str(ann_id)
                sent_ids.add(ann_id)
                successfully_sent.append(ann_id)
            print(f"Sent {i}/{len(new_announcements)}")
        else:
            print(f"Failed to send {i}/{len(new_announcements)}")

        time.sleep(TELEGRAM_DELAY)

    # Save updated list of sent announcements
    save_sent_announcements(sent_ids)

    # Summary
    summary = f"""✅ <b>COMPLETE</b>

📤 New SME announcements sent: {len(successfully_sent)}
📊 Total SME sent today: {len(sent_ids)}
⏰ {now.strftime('%H:%M:%S')}"""

    send_telegram(summary)

    print("\n" + "="*60)
    print(f"DONE - Sent {len(successfully_sent)} SME announcements")
    print(f"Tracking file: {SENT_FILE}")
    print("="*60)


if __name__ == "__main__":
    main()
