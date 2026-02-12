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

# --- NSE settings ---
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}
NSE_MAX_RETRIES = 3

# --- BSE settings ---
BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_ATTACHMENT_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_SCRIP_CACHE_FILE = "bse_sme_scrips.json"
BSE_MAX_RETRIES = 3
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin": "https://www.bseindia.com/",
    "Referer": "https://www.bseindia.com/",
    "Connection": "keep-alive",
}

# --- Telegram settings ---
TELEGRAM_DELAY = 3  # seconds between messages
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_MAX_MSG_LEN = 4096


def _atomic_json_write(file_path, data):
    """Write JSON data to file atomically using temp file + os.replace."""
    dir_name = os.path.dirname(os.path.abspath(file_path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, file_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


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
    _atomic_json_write(SENT_FILE, data)


# --- BSE scrip cache ---

def load_bse_sme_scrips():
    """Load cached BSE SME scrip codes (groups M and MT). Returns set or None if stale/missing."""
    if os.path.exists(BSE_SCRIP_CACHE_FILE):
        try:
            with open(BSE_SCRIP_CACHE_FILE, 'r') as f:
                data = json.load(f)
                today = datetime.now(IST).strftime('%Y-%m-%d')
                if data.get('date') == today:
                    scrips = set(data.get('scrip_codes', []))
                    logger.info("Loaded %d cached BSE SME scrip codes", len(scrips))
                    return scrips
                else:
                    logger.info("BSE scrip cache is stale (date: %s), will refresh", data.get('date'))
                    return None
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Could not load BSE scrip cache: %s", e)
            return None
    return None


def save_bse_sme_scrips(scrip_codes):
    """Save BSE SME scrip codes cache atomically"""
    data = {
        'date': datetime.now(IST).strftime('%Y-%m-%d'),
        'scrip_codes': list(scrip_codes)
    }
    _atomic_json_write(BSE_SCRIP_CACHE_FILE, data)


def fetch_bse_sme_scrip_codes():
    """Fetch BSE SME scrip codes for groups M and MT. Returns set or None on failure."""
    scrip_codes = set()

    for group in ("M", "MT"):
        url = f"{BSE_API_URL}/ListofScripData/w"
        params = {
            "scripcode": "",
            "Group": group,
            "industry": "",
            "segment": "Equity",
            "status": "Active",
        }

        try:
            logger.info("Fetching BSE scrip codes for group %s...", group)
            response = requests.get(url, headers=BSE_HEADERS, params=params, timeout=15)

            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    for item in data:
                        code = str(item.get('SCRIP_CD', '')).strip()
                        if code:
                            scrip_codes.add(code)
                    logger.info("Group %s: %d scrip codes", group, len(data))
                else:
                    logger.warning("BSE listSecurities for group %s returned unexpected format", group)
            else:
                logger.error("BSE listSecurities HTTP %d for group %s", response.status_code, group)
                return None
        except requests.exceptions.RequestException as e:
            logger.error("BSE listSecurities error for group %s: %s", group, e)
            return None

        time.sleep(1)

    if not scrip_codes:
        logger.error("BSE returned 0 scrip codes for groups M and MT")
        return None

    logger.info("Total BSE SME scrip codes: %d", len(scrip_codes))
    return scrip_codes


# --- Telegram ---

def send_telegram(msg):
    """Send message to Telegram with retry on rate limit"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID not set")
        return False

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


# --- NSE fetch ---

def fetch_nse_sme_announcements():
    """Fetch NSE SME announcements. Returns list on success, None on failure."""
    api_url = "https://www.nseindia.com/api/corporate-announcements?index=sme"

    for attempt in range(1, NSE_MAX_RETRIES + 1):
        session = requests.Session()
        session.headers.update(NSE_HEADERS)

        try:
            logger.info("Fetching NSE SME announcements (attempt %d/%d)...", attempt, NSE_MAX_RETRIES)

            session.get("https://www.nseindia.com", timeout=10)
            time.sleep(2)

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

                logger.info("Fetched %d NSE SME announcements", len(announcements))
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


# --- BSE fetch ---

def fetch_bse_sme_announcements(sme_scrip_codes):
    """Fetch BSE equity announcements and filter to SME scrips. Returns list or None on failure."""
    today = datetime.now(IST)
    date_str = today.strftime('%Y%m%d')

    url = f"{BSE_API_URL}/AnnSubCategoryGetData/w"
    params = {
        "pageno": 1,
        "strCat": "-1",
        "subcategory": "-1",
        "strPrevDate": date_str,
        "strToDate": date_str,
        "strSearch": "P",
        "strscrip": "",
        "strType": "C",
    }

    all_announcements = []

    for attempt in range(1, BSE_MAX_RETRIES + 1):
        try:
            logger.info("Fetching BSE equity announcements (attempt %d/%d)...", attempt, BSE_MAX_RETRIES)
            response = requests.get(url, headers=BSE_HEADERS, params=params, timeout=15)

            if response.status_code == 200:
                data = response.json()

                if not isinstance(data, dict) or 'Table' not in data:
                    logger.warning("BSE announcements returned unexpected format")
                    return []

                page_items = data.get('Table', [])
                total_count = 0
                if data.get('Table1'):
                    total_count = data['Table1'][0].get('ROWCNT', 0)

                all_announcements.extend(page_items)
                logger.info("BSE page 1: %d items, total=%d", len(page_items), total_count)

                # Paginate if needed
                page_no = 2
                while len(all_announcements) < total_count and page_no <= 50:
                    time.sleep(0.5)
                    page_params = dict(params, pageno=page_no)
                    resp = requests.get(url, headers=BSE_HEADERS, params=page_params, timeout=15)
                    if resp.status_code == 200:
                        page_data = resp.json()
                        items = page_data.get('Table', [])
                        if not items:
                            break
                        all_announcements.extend(items)
                        logger.info("BSE page %d: %d items (total so far: %d)", page_no, len(items), len(all_announcements))
                    else:
                        logger.warning("BSE page %d HTTP %d, stopping pagination", page_no, resp.status_code)
                        break
                    page_no += 1

                break  # Success, exit retry loop

            elif response.status_code >= 500:
                logger.warning("BSE server error %d, will retry...", response.status_code)
            else:
                logger.error("BSE returned %d", response.status_code)
                return None

        except requests.exceptions.Timeout:
            logger.warning("BSE request timed out (attempt %d/%d)", attempt, BSE_MAX_RETRIES)
        except requests.exceptions.RequestException as e:
            logger.warning("BSE request error (attempt %d/%d): %s", attempt, BSE_MAX_RETRIES, e)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("BSE returned invalid JSON: %s", e)
            return None

        if attempt < BSE_MAX_RETRIES:
            wait = 2 ** attempt
            logger.info("Waiting %ds before retry...", wait)
            time.sleep(wait)
    else:
        logger.error("Failed to fetch from BSE after %d attempts", BSE_MAX_RETRIES)
        return None

    # Filter to SME scrip codes (SCRIP_CD is int in announcements, string in cache)
    sme_announcements = [
        ann for ann in all_announcements
        if str(ann.get('SCRIP_CD', '')).strip() in sme_scrip_codes
    ]

    logger.info("BSE announcements: total=%d, SME filtered=%d", len(all_announcements), len(sme_announcements))
    return sme_announcements


# --- ID and formatting ---

def get_announcement_id(ann, exchange="nse"):
    """Get a unique ID for an announcement, prefixed by exchange."""
    if exchange == "bse":
        news_id = ann.get('NEWSID')
        if news_id is not None:
            return f"bse_{news_id}"
        scrip_cd = str(ann.get('SCRIP_CD', ''))
        news_dt = ann.get('NEWS_DT', '')
        newssub = ann.get('NEWSSUB', '')[:50]
        fallback = hashlib.md5(f"{scrip_cd}|{news_dt}|{newssub}".encode()).hexdigest()[:12]
        logger.warning("No NEWSID for BSE scrip %s, using fallback ID: %s", scrip_cd, fallback)
        return f"bse_fallback_{fallback}"
    else:
        seq_id = ann.get('seq_id')
        if seq_id is not None:
            return f"nse_{seq_id}"
        symbol = ann.get('symbol', '')
        date = ann.get('an_dt', '')
        desc = ann.get('desc', '')[:50]
        fallback = hashlib.md5(f"{symbol}|{date}|{desc}".encode()).hexdigest()[:12]
        logger.warning("No seq_id for %s, using fallback ID: %s", symbol, fallback)
        return f"nse_fallback_{fallback}"


def format_announcement(ann, index=None, exchange="nse"):
    """Format announcement for Telegram with proper HTML escaping"""
    if exchange == "bse":
        symbol = html.escape(str(ann.get('SCRIP_CD', 'N/A')))
        company = html.escape(ann.get('SLONGNAME', 'N/A'))
        subject = html.escape(ann.get('NEWSSUB', 'N/A'))
        # Parse ISO datetime to readable date
        raw_dt = ann.get('NEWS_DT', '')
        try:
            # Handle variable fractional second lengths (e.g. .94, .373, .12345)
            dt_clean = raw_dt.split('.')[0] if '.' in raw_dt else raw_dt
            date = datetime.fromisoformat(dt_clean).strftime('%d-%b-%Y %H:%M')
        except (ValueError, TypeError):
            date = html.escape(str(raw_dt))
        attachment = ann.get('ATTACHMENTNAME', '')
        if attachment and not attachment.startswith('http'):
            attachment = BSE_ATTACHMENT_BASE + attachment
        exchange_label = "BSE"
    else:
        symbol = html.escape(ann.get('symbol', 'N/A'))
        company = html.escape(ann.get('sm_name', 'N/A'))
        subject = html.escape(ann.get('desc', 'N/A'))
        date = html.escape(ann.get('an_dt', 'N/A'))
        attachment = ann.get('attchmntFile', '')
        exchange_label = "NSE"

    if len(subject) > 100:
        subject = subject[:97] + "..."

    if index is None:
        header = f"🔔 <b>{exchange_label} SME ANNOUNCEMENT</b>"
    else:
        header = f"🔔 <b>NEW SME ANNOUNCEMENT #{index}</b> [{exchange_label}]"

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


# --- Main ---

def main():
    """Main function"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN and CHAT_ID environment variables are required")
        exit(1)

    now = datetime.now(IST)

    logger.info("=" * 60)
    logger.info("SME ANNOUNCEMENT BOT (NSE + BSE) - %s", now.strftime('%d-%m-%Y %H:%M:%S IST'))
    logger.info("=" * 60)

    # Load previously sent announcements
    sent_ids = load_sent_announcements()
    is_first_run_today = len(sent_ids) == 0

    if is_first_run_today:
        logger.info("First run of the day - will send all SME announcements")
    else:
        logger.info("Incremental run - %d announcements already sent today", len(sent_ids))

    # --- Fetch NSE ---
    nse_announcements = fetch_nse_sme_announcements()
    nse_ok = nse_announcements is not None
    if not nse_ok:
        nse_announcements = []
        logger.error("NSE fetch failed")

    # --- Fetch BSE ---
    bse_announcements = []
    bse_ok = False

    sme_scrip_codes = load_bse_sme_scrips()
    if sme_scrip_codes is None:
        sme_scrip_codes = fetch_bse_sme_scrip_codes()
        if sme_scrip_codes:
            save_bse_sme_scrips(sme_scrip_codes)
        else:
            logger.error("Failed to fetch BSE SME scrip codes")

    if sme_scrip_codes:
        bse_result = fetch_bse_sme_announcements(sme_scrip_codes)
        if bse_result is not None:
            bse_announcements = bse_result
            bse_ok = True
        else:
            logger.error("BSE announcements fetch failed")

    # --- Handle total failure ---
    if not nse_ok and not bse_ok:
        error_msg = f"""❌ <b>SME Bot Error</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST

Failed to fetch SME announcements from both NSE and BSE.
Will retry on next run."""

        send_telegram(error_msg)
        logger.error("Both NSE and BSE fetch failed")
        return

    # --- Merge and tag with exchange ---
    tagged = []
    for ann in nse_announcements:
        tagged.append(("nse", ann))
    for ann in bse_announcements:
        tagged.append(("bse", ann))

    logger.info("Total fetched: NSE=%d, BSE=%d", len(nse_announcements), len(bse_announcements))

    # --- Filter for new announcements ---
    new_tagged = []
    for exchange, ann in tagged:
        ann_id = get_announcement_id(ann, exchange)
        if ann_id not in sent_ids:
            new_tagged.append((exchange, ann))

    nse_new = sum(1 for ex, _ in new_tagged if ex == "nse")
    bse_new = sum(1 for ex, _ in new_tagged if ex == "bse")

    logger.info("Summary: total=%d, already_sent=%d, new=%d (NSE: %d, BSE: %d)",
                len(tagged), len(sent_ids), len(new_tagged), nse_new, bse_new)

    if not new_tagged:
        logger.info("No new SME announcements to send")

        if is_first_run_today:
            exchange_note = ""
            if not nse_ok:
                exchange_note = "\n⚠️ NSE fetch failed"
            elif not bse_ok:
                exchange_note = "\n⚠️ BSE fetch failed"

            status_msg = f"""ℹ️ <b>SME Bot Status</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST
📊 NSE: {len(nse_announcements)} | BSE: {len(bse_announcements)}
✅ No new announcements today{exchange_note}"""

            send_telegram(status_msg)
        return

    # --- Send header ---
    logger.info("=" * 60)
    logger.info("SENDING TO TELEGRAM")
    logger.info("=" * 60)

    run_type = "FIRST RUN - ALL SME ANNOUNCEMENTS" if is_first_run_today else "INCREMENTAL SME UPDATE"

    exchange_warning = ""
    if not nse_ok:
        exchange_warning = "\n⚠️ NSE fetch failed - showing BSE only"
    elif not bse_ok:
        exchange_warning = "\n⚠️ BSE fetch failed - showing NSE only"

    header = f"""📢 <b>{run_type}</b>

📅 {now.strftime('%d-%m-%Y')}
⏰ {now.strftime('%H:%M:%S')} IST
🏭 SME Platform (NSE + BSE)
📊 New: {len(new_tagged)} (NSE: {nse_new}, BSE: {bse_new}){exchange_warning}

Sending..."""

    send_telegram(header)
    time.sleep(TELEGRAM_DELAY)

    # --- Send announcements ---
    successfully_sent = []

    for i, (exchange, ann) in enumerate(new_tagged, 1):
        logger.info("Sending %d/%d [%s]...", i, len(new_tagged), exchange.upper())

        msg = format_announcement(ann, i, exchange)

        if send_telegram(msg):
            ann_id = get_announcement_id(ann, exchange)
            sent_ids.add(ann_id)
            successfully_sent.append(ann_id)
            logger.info("Sent %d/%d", i, len(new_tagged))
        else:
            logger.error("Failed to send %d/%d", i, len(new_tagged))

        if successfully_sent and i % 5 == 0:
            save_sent_announcements(sent_ids)

        time.sleep(TELEGRAM_DELAY)

    # Final save
    save_sent_announcements(sent_ids)

    # Summary
    nse_sent = sum(1 for aid in successfully_sent if aid.startswith("nse_"))
    bse_sent = sum(1 for aid in successfully_sent if aid.startswith("bse_"))

    summary = f"""✅ <b>COMPLETE</b>

📤 Sent: {len(successfully_sent)} (NSE: {nse_sent}, BSE: {bse_sent})
📊 Total sent today: {len(sent_ids)}
⏰ {now.strftime('%H:%M:%S')} IST"""

    send_telegram(summary)

    logger.info("=" * 60)
    logger.info("DONE - Sent %d announcements (NSE: %d, BSE: %d)", len(successfully_sent), nse_sent, bse_sent)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
