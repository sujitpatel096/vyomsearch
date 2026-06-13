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

SENT_FILE = "sent_announcements.json"
IST = timezone(timedelta(hours=5, minutes=30))

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}
NSE_MAX_RETRIES = 3

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

TELEGRAM_DELAY = 3
TELEGRAM_MAX_RETRIES = 3
TELEGRAM_MAX_MSG_LEN = 4096


# ── HTML TEMPLATE FOR TABLE IMAGE ──────────────────────────────────────────────

TABLE_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500&family=DM+Mono:wght@300;400&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: #080E18;
    font-family: 'DM Sans', sans-serif;
    font-weight: 300;
    color: #C8D8E8;
    padding: 24px;
    min-width: 900px;
  }

  .container {
    background: #0D1525;
    border: 1px solid rgba(46, 216, 195, 0.15);
    border-radius: 12px;
    overflow: hidden;
  }

  .header {
    background: linear-gradient(135deg, #0F2035, #0D1525);
    padding: 20px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid rgba(46, 216, 195, 0.1);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .logo-mark {
    width: 36px;
    height: 36px;
  }

  .header-title {
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 4px;
    background: linear-gradient(135deg, #2ED8C3, #5B7CF7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  .header-sub {
    font-size: 10px;
    letter-spacing: 2px;
    color: rgba(255,255,255,0.25);
    margin-top: 3px;
  }

  .header-right {
    text-align: right;
  }

  .exchange-badge {
    font-size: 11px;
    letter-spacing: 3px;
    font-weight: 500;
    padding: 4px 12px;
    border-radius: 4px;
    border: 1px solid rgba(46, 216, 195, 0.3);
    color: #2ED8C3;
  }

  .meta {
    font-size: 10px;
    color: rgba(255,255,255,0.25);
    margin-top: 6px;
    letter-spacing: 1px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
  }

  thead tr {
    background: #111D30;
    border-bottom: 1px solid rgba(46, 216, 195, 0.15);
  }

  thead th {
    padding: 12px 16px;
    text-align: left;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 3px;
    color: rgba(46, 216, 195, 0.7);
    text-transform: uppercase;
  }

  tbody tr {
    border-bottom: 1px solid rgba(255,255,255,0.04);
    transition: background 0.2s;
  }

  tbody tr:nth-child(even) {
    background: rgba(255,255,255,0.015);
  }

  tbody tr:last-child {
    border-bottom: none;
  }

  tbody td {
    padding: 13px 16px;
    font-size: 12px;
    vertical-align: middle;
  }

  .td-sr {
    color: rgba(255,255,255,0.25);
    font-size: 11px;
    width: 36px;
  }

  .td-date {
    color: rgba(255,255,255,0.45);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    white-space: nowrap;
    width: 90px;
  }

  .td-time {
    color: rgba(255,255,255,0.35);
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    width: 54px;
  }

  .td-company {
    color: #E8F0F8;
    font-weight: 400;
    max-width: 180px;
  }

  .td-ticker {
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    font-weight: 500;
    color: #2ED8C3;
    white-space: nowrap;
    width: 110px;
  }

  .td-desc {
    color: rgba(255,255,255,0.45);
    font-size: 11px;
    max-width: 240px;
    line-height: 1.5;
  }

  .td-doc {
    width: 40px;
    text-align: center;
  }

  .doc-icon {
    display: inline-block;
    width: 26px;
    height: 26px;
    background: rgba(46, 216, 195, 0.1);
    border: 1px solid rgba(46, 216, 195, 0.25);
    border-radius: 5px;
    font-size: 12px;
    line-height: 26px;
    text-align: center;
    color: #2ED8C3;
  }

  .no-doc {
    color: rgba(255,255,255,0.15);
    font-size: 11px;
  }

  .footer {
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-top: 1px solid rgba(255,255,255,0.04);
    background: #080E18;
  }

  .footer-brand {
    font-size: 10px;
    letter-spacing: 3px;
    background: linear-gradient(135deg, #2ED8C3, #5B7CF7);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  .footer-note {
    font-size: 10px;
    color: rgba(255,255,255,0.15);
    letter-spacing: 1px;
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="header-left">
      <svg class="logo-mark" viewBox="0 0 140 140" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="lg" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#2ED8C3"/>
            <stop offset="100%" stop-color="#5B7CF7"/>
          </linearGradient>
        </defs>
        <line x1="30" y1="20" x2="30" y2="80" stroke="url(#lg)" stroke-width="3" stroke-linecap="round"/>
        <line x1="30" y1="20" x2="110" y2="20" stroke="url(#lg)" stroke-width="3" stroke-linecap="round"/>
        <line x1="110" y1="20" x2="110" y2="110" stroke="url(#lg)" stroke-width="3" stroke-linecap="round"/>
        <line x1="110" y1="110" x2="50" y2="110" stroke="url(#lg)" stroke-width="3" stroke-linecap="round"/>
        <polyline points="50,45 70,80 90,45" fill="none" stroke="url(#lg)" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div>
        <div class="header-title">VYOM CAPITAL</div>
        <div class="header-sub">SME MARKET INTELLIGENCE</div>
      </div>
    </div>
    <div class="header-right">
      <div class="exchange-badge">{{ exchange_label }} SME</div>
      <div class="meta">{{ date_str }} &nbsp;·&nbsp; {{ time_str }} IST &nbsp;·&nbsp; {{ count }} announcements</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>No</th>
        <th>Date</th>
        <th>Time</th>
        <th>Company</th>
        <th>Ticker</th>
        <th>Description</th>
        <th>Doc</th>
      </tr>
    </thead>
    <tbody>
      {% for row in rows %}
      <tr>
        <td class="td-sr">{{ row.sr }}</td>
        <td class="td-date">{{ row.date }}</td>
        <td class="td-time">{{ row.time }}</td>
        <td class="td-company">{{ row.company }}</td>
        <td class="td-ticker">#{{ row.ticker }}</td>
        <td class="td-desc">{{ row.desc }}</td>
        <td class="td-doc">
          {% if row.has_doc %}
            <span class="doc-icon">↗</span>
          {% else %}
            <span class="no-doc">—</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <div class="footer">
    <div class="footer-brand">VYOM CAPITAL · vyomcapital.in</div>
    <div class="footer-note">Data sourced from NSE &amp; BSE · For informational purposes only</div>
  </div>
</div>
</body>
</html>
"""


# ── ATOMIC WRITE ────────────────────────────────────────────────────────────────

def _atomic_json_write(file_path, data):
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


# ── SENT ANNOUNCEMENTS ──────────────────────────────────────────────────────────

def load_sent_announcements():
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
    data = {
        'date': datetime.now(IST).strftime('%Y-%m-%d'),
        'sent_ids': list(sent_ids)
    }
    _atomic_json_write(SENT_FILE, data)


# ── BSE SCRIP CACHE (now stores scrip_id lookup too) ───────────────────────────

def load_bse_sme_scrips():
    if os.path.exists(BSE_SCRIP_CACHE_FILE):
        try:
            with open(BSE_SCRIP_CACHE_FILE, 'r') as f:
                data = json.load(f)
                today = datetime.now(IST).strftime('%Y-%m-%d')
                if data.get('date') == today:
                    scrips = set(data.get('scrip_codes', []))
                    ticker_map = data.get('ticker_map', {})
                    logger.info("Loaded %d cached BSE SME scrip codes", len(scrips))
                    return scrips, ticker_map
                else:
                    logger.info("BSE scrip cache stale, will refresh")
                    return None, {}
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Could not load BSE scrip cache: %s", e)
            return None, {}
    return None, {}


def save_bse_sme_scrips(scrip_codes, ticker_map):
    data = {
        'date': datetime.now(IST).strftime('%Y-%m-%d'),
        'scrip_codes': list(scrip_codes),
        'ticker_map': ticker_map,
    }
    _atomic_json_write(BSE_SCRIP_CACHE_FILE, data)


def fetch_bse_sme_scrip_codes():
    """Fetch BSE SME scrip codes + build ticker_map {scrip_cd: scrip_id}"""
    scrip_codes = set()
    ticker_map = {}

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
                        scrip_id = str(item.get('scrip_id', '')).strip()
                        if code:
                            scrip_codes.add(code)
                            if scrip_id:
                                ticker_map[code] = scrip_id.upper()
                    logger.info("Group %s: %d scrip codes", group, len(data))
                else:
                    logger.warning("BSE listSecurities for group %s unexpected format", group)
                    return None, {}
            else:
                logger.error("BSE listSecurities HTTP %d for group %s", response.status_code, group)
                return None, {}
        except requests.exceptions.RequestException as e:
            logger.error("BSE listSecurities error for group %s: %s", group, e)
            return None, {}
        time.sleep(1)

    if not scrip_codes:
        logger.error("BSE returned 0 scrip codes")
        return None, {}

    logger.info("Total BSE SME scrip codes: %d, ticker map: %d", len(scrip_codes), len(ticker_map))
    return scrip_codes, ticker_map


# ── TELEGRAM ────────────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID not set")
        return False

    if len(msg) > TELEGRAM_MAX_MSG_LEN:
        msg = msg[:TELEGRAM_MAX_MSG_LEN - 20] + "\n... (truncated)"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            response = requests.post(url, data={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }, timeout=10)
            if response.status_code == 200 and response.json().get('ok'):
                return True
            elif response.status_code == 429:
                retry_after = response.json().get('parameters', {}).get('retry_after', 5)
                wait_time = max(retry_after, 2 ** attempt)
                logger.warning("Rate limited. Retry %d/%d after %ds", attempt, TELEGRAM_MAX_RETRIES, wait_time)
                time.sleep(wait_time)
            else:
                logger.error("Telegram HTTP %d: %s", response.status_code, response.text[:100])
                return False
        except requests.exceptions.RequestException as e:
            logger.error("Telegram request error: %s", e)
            if attempt < TELEGRAM_MAX_RETRIES:
                time.sleep(2 ** attempt)
    return False


def send_telegram_photo(image_path, caption):
    """Send a photo to Telegram with a caption containing clickable links"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN or CHAT_ID not set")
        return False

    if len(caption) > TELEGRAM_MAX_MSG_LEN:
        caption = caption[:TELEGRAM_MAX_MSG_LEN - 20] + "\n... (truncated)"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    for attempt in range(1, TELEGRAM_MAX_RETRIES + 1):
        try:
            with open(image_path, 'rb') as photo:
                response = requests.post(url, data={
                    "chat_id": CHAT_ID,
                    "caption": caption,
                    "parse_mode": "HTML",
                }, files={"photo": photo}, timeout=30)

            if response.status_code == 200 and response.json().get('ok'):
                logger.info("Photo sent successfully")
                return True
            elif response.status_code == 429:
                retry_after = response.json().get('parameters', {}).get('retry_after', 5)
                wait_time = max(retry_after, 2 ** attempt)
                logger.warning("Rate limited. Retry %d/%d after %ds", attempt, TELEGRAM_MAX_RETRIES, wait_time)
                time.sleep(wait_time)
            else:
                logger.error("Telegram sendPhoto HTTP %d: %s", response.status_code, response.text[:200])
                return False
        except requests.exceptions.RequestException as e:
            logger.error("Telegram photo request error: %s", e)
            if attempt < TELEGRAM_MAX_RETRIES:
                time.sleep(2 ** attempt)
    return False


# ── IMAGE GENERATION ────────────────────────────────────────────────────────────

def generate_table_image(rows, exchange_label, output_path):
    """Render HTML table to PNG using Playwright"""
    try:
        from jinja2 import Template
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        logger.error("Missing dependency: %s. Run: pip install playwright jinja2 && playwright install chromium", e)
        return False

    now = datetime.now(IST)

    template = Template(TABLE_HTML)
    rendered_html = template.render(
        exchange_label=exchange_label,
        date_str=now.strftime('%d %b %Y'),
        time_str=now.strftime('%H:%M'),
        count=len(rows),
        rows=rows,
    )

    # Write HTML to temp file
    fd, html_path = tempfile.mkstemp(suffix='.html')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(rendered_html)

        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
            page = browser.new_page(viewport={"width": 960, "height": 800})
            page.goto(f"file://{os.path.abspath(html_path)}")
            page.wait_for_timeout(1500)  # wait for Google Fonts to load

            # Screenshot just the container div
            container = page.query_selector('.container')
            if container:
                container.screenshot(path=output_path)
            else:
                page.screenshot(path=output_path, full_page=True)

            browser.close()

        logger.info("Table image generated: %s", output_path)
        return True

    except Exception as e:
        logger.error("Failed to generate table image: %s", e)
        return False
    finally:
        if os.path.exists(html_path):
            os.unlink(html_path)


def build_caption(rows, exchange_label, tickers):
    """Build Telegram caption with clickable links and Twitter hashtags"""
    now = datetime.now(IST)
    lines = [
        f"📊 <b>{exchange_label} SME ANNOUNCEMENTS</b>",
        f"📅 {now.strftime('%d-%b-%Y')} · ⏰ {now.strftime('%H:%M')} IST · {len(rows)} new\n",
    ]

    # Clickable document links
    has_links = False
    for row in rows:
        if row.get('url'):
            safe_url = html.escape(row['url'], quote=True)
            company = html.escape(row['company'])
            lines.append(f"🔗 {row['sr']}. <a href='{safe_url}'>{company}</a>")
            has_links = True

    if not has_links:
        lines.append("📎 No documents attached")

    # Twitter hashtags
    hashtags = " ".join([f"#{t}" for t in tickers if t])
    if hashtags:
        lines.append(f"\n🐦 {hashtags} #SME #{exchange_label} #PreIPO #India")

    return "\n".join(lines)


# ── ANNOUNCEMENT PROCESSING ─────────────────────────────────────────────────────

def build_rows(announcements_with_exchange, ticker_map):
    """Convert raw announcements into clean row dicts for table rendering"""
    rows = []
    for i, (exchange, ann) in enumerate(announcements_with_exchange, 1):
        if exchange == "bse":
            scrip_cd = str(ann.get('SCRIP_CD', ''))
            ticker = ticker_map.get(scrip_cd, scrip_cd)  # text ticker or fallback to numeric
            company = str(ann.get('SLONGNAME', ann.get('Scrip_Name', 'N/A')))
            desc = str(ann.get('NEWSSUB', 'N/A'))
            raw_dt = ann.get('NEWS_DT', '')
            try:
                dt_clean = raw_dt.split('.')[0] if '.' in raw_dt else raw_dt
                dt_obj = datetime.fromisoformat(dt_clean)
                date_str = dt_obj.strftime('%d-%b-%Y')
                time_str = dt_obj.strftime('%H:%M')
            except (ValueError, TypeError):
                date_str = str(raw_dt)[:10]
                time_str = ""
            attachment = ann.get('ATTACHMENTNAME', '')
            url = (BSE_ATTACHMENT_BASE + attachment) if attachment else ''

        else:  # NSE
            ticker = str(ann.get('symbol', 'N/A')).upper()
            company = str(ann.get('sm_name', 'N/A'))
            desc = str(ann.get('desc', 'N/A'))
            raw_dt = ann.get('an_dt', '')
            try:
                dt_obj = datetime.strptime(raw_dt, '%d-%b-%Y %H:%M')
                date_str = dt_obj.strftime('%d-%b-%Y')
                time_str = dt_obj.strftime('%H:%M')
            except (ValueError, TypeError):
                date_str = str(raw_dt)[:10]
                time_str = ""
            url = ann.get('attchmntFile', '')

        rows.append({
            'sr': i,
            'date': date_str,
            'time': time_str,
            'company': html.escape(company[:28]),
            'ticker': html.escape(ticker),
            'desc': html.escape(desc[:45]),
            'has_doc': bool(url),
            'url': url,
        })
    return rows


def send_announcements_as_table(announcements_with_exchange, exchange_label, ticker_map):
    """Generate image table and send to Telegram. Falls back to text if image fails."""
    if not announcements_with_exchange:
        return []

    rows = build_rows(announcements_with_exchange, ticker_map)
    tickers = [r['ticker'] for r in rows]

    # Split into batches of 25 (keeps image readable)
    batch_size = 25
    sent_count = 0

    for batch_start in range(0, len(rows), batch_size):
        batch_rows = rows[batch_start:batch_start + batch_size]
        batch_anns = announcements_with_exchange[batch_start:batch_start + batch_size]
        batch_tickers = tickers[batch_start:batch_start + batch_size]

        # Renumber rows within batch
        for j, row in enumerate(batch_rows, batch_start + 1):
            row['sr'] = j

        caption = build_caption(batch_rows, exchange_label, batch_tickers)

        # Try image approach first
        fd, img_path = tempfile.mkstemp(suffix='.png')
        os.close(fd)

        image_ok = generate_table_image(batch_rows, exchange_label, img_path)

        if image_ok and os.path.exists(img_path):
            success = send_telegram_photo(img_path, caption)
            try:
                os.unlink(img_path)
            except Exception:
                pass
        else:
            # Fallback: send caption as text only
            logger.warning("Image generation failed, falling back to text")
            if os.path.exists(img_path):
                try:
                    os.unlink(img_path)
                except Exception:
                    pass
            success = send_telegram(caption)

        if success:
            sent_count += len(batch_anns)
        else:
            logger.error("Failed to send batch %d-%d", batch_start + 1, batch_start + len(batch_anns))

        time.sleep(TELEGRAM_DELAY)

    return sent_count


# ── NSE FETCH ───────────────────────────────────────────────────────────────────

def fetch_nse_sme_announcements():
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
                logger.warning("NSE server error %d, retrying...", response.status_code)
            else:
                logger.error("NSE returned %d", response.status_code)
                return None
        except requests.exceptions.Timeout:
            logger.warning("NSE timeout (attempt %d/%d)", attempt, NSE_MAX_RETRIES)
        except requests.exceptions.RequestException as e:
            logger.warning("NSE error (attempt %d/%d): %s", attempt, NSE_MAX_RETRIES, e)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("NSE invalid JSON: %s", e)
            return None
        finally:
            session.close()
        if attempt < NSE_MAX_RETRIES:
            time.sleep(2 ** attempt)
    return None


# ── BSE FETCH ───────────────────────────────────────────────────────────────────

def _filter_bse_sme(items, sme_scrip_codes):
    return [ann for ann in items if str(ann.get('SCRIP_CD', '')).strip() in sme_scrip_codes]


def fetch_bse_sme_announcements(sme_scrip_codes, sent_ids=None):
    today = datetime.now(IST)
    date_str = today.strftime('%Y%m%d')
    is_incremental = bool(sent_ids)

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

    sme_announcements = []
    pages_without_new_sme = 0

    for attempt in range(1, BSE_MAX_RETRIES + 1):
        try:
            logger.info("Fetching BSE equity announcements (attempt %d/%d)...", attempt, BSE_MAX_RETRIES)
            response = requests.get(url, headers=BSE_HEADERS, params=params, timeout=15)

            if response.status_code == 200:
                data = response.json()
                if not isinstance(data, dict) or 'Table' not in data:
                    logger.warning("BSE unexpected format")
                    return []

                page_items = data.get('Table', [])
                total_count = 0
                if data.get('Table1'):
                    total_count = data['Table1'][0].get('ROWCNT', 0)

                page_sme = _filter_bse_sme(page_items, sme_scrip_codes)
                sme_announcements.extend(page_sme)
                fetched_so_far = len(page_items)

                logger.info("BSE page 1: %d items (SME: %d), total=%d", len(page_items), len(page_sme), total_count)

                if is_incremental and page_sme:
                    new_on_page = sum(1 for a in page_sme if get_announcement_id(a, "bse") not in sent_ids)
                    if new_on_page == 0:
                        pages_without_new_sme += 1

                page_no = 2
                while fetched_so_far < total_count and page_no <= 50:
                    if is_incremental and pages_without_new_sme >= 3:
                        logger.info("Early stop: 3 consecutive pages with no new SME")
                        break
                    time.sleep(0.5)
                    page_params = dict(params, pageno=page_no)
                    resp = requests.get(url, headers=BSE_HEADERS, params=page_params, timeout=15)
                    if resp.status_code == 200:
                        page_data = resp.json()
                        items = page_data.get('Table', [])
                        if not items:
                            break
                        page_sme = _filter_bse_sme(items, sme_scrip_codes)
                        sme_announcements.extend(page_sme)
                        fetched_so_far += len(items)
                        if is_incremental:
                            new_on_page = sum(1 for a in page_sme if get_announcement_id(a, "bse") not in sent_ids) if page_sme else 0
                            pages_without_new_sme = 0 if new_on_page > 0 else pages_without_new_sme + 1
                        logger.info("BSE page %d: %d items, SME: %d", page_no, len(items), len(page_sme))
                    else:
                        break
                    page_no += 1
                break

            elif response.status_code >= 500:
                logger.warning("BSE server error %d, retrying...", response.status_code)
            else:
                logger.error("BSE returned %d", response.status_code)
                return None

        except requests.exceptions.Timeout:
            logger.warning("BSE timeout (attempt %d/%d)", attempt, BSE_MAX_RETRIES)
        except requests.exceptions.RequestException as e:
            logger.warning("BSE error (attempt %d/%d): %s", attempt, BSE_MAX_RETRIES, e)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("BSE invalid JSON: %s", e)
            return None

        if attempt < BSE_MAX_RETRIES:
            time.sleep(2 ** attempt)
    else:
        logger.error("Failed to fetch from BSE after %d attempts", BSE_MAX_RETRIES)
        return None

    logger.info("BSE SME announcements total: %d", len(sme_announcements))
    return sme_announcements


# ── ID GENERATION ───────────────────────────────────────────────────────────────

def get_announcement_id(ann, exchange="nse"):
    if exchange == "bse":
        news_id = ann.get('NEWSID')
        if news_id is not None:
            return f"bse_{news_id}"
        scrip_cd = str(ann.get('SCRIP_CD', ''))
        news_dt = ann.get('NEWS_DT', '')
        newssub = ann.get('NEWSSUB', '')[:50]
        fallback = hashlib.md5(f"{scrip_cd}|{news_dt}|{newssub}".encode()).hexdigest()[:12]
        return f"bse_fallback_{fallback}"
    else:
        seq_id = ann.get('seq_id')
        if seq_id is not None:
            return f"nse_{seq_id}"
        symbol = ann.get('symbol', '')
        date = ann.get('an_dt', '')
        desc = ann.get('desc', '')[:50]
        fallback = hashlib.md5(f"{symbol}|{date}|{desc}".encode()).hexdigest()[:12]
        return f"nse_fallback_{fallback}"


# ── MAIN ────────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("BOT_TOKEN and CHAT_ID environment variables are required")
        exit(1)

    now = datetime.now(IST)
    logger.info("=" * 60)
    logger.info("SME ANNOUNCEMENT BOT (NSE + BSE) - %s", now.strftime('%d-%m-%Y %H:%M:%S IST'))
    logger.info("=" * 60)

    sent_ids = load_sent_announcements()
    is_first_run_today = len(sent_ids) == 0

    if is_first_run_today:
        logger.info("First run of the day")
    else:
        logger.info("Incremental run - %d announcements already sent", len(sent_ids))

    # ── Fetch NSE ──
    nse_announcements = fetch_nse_sme_announcements()
    nse_ok = nse_announcements is not None
    if not nse_ok:
        nse_announcements = []

    # ── Fetch BSE ──
    bse_announcements = []
    bse_ok = False
    ticker_map = {}

    sme_scrip_codes, ticker_map = load_bse_sme_scrips()
    if sme_scrip_codes is None:
        sme_scrip_codes, ticker_map = fetch_bse_sme_scrip_codes()
        if sme_scrip_codes:
            save_bse_sme_scrips(sme_scrip_codes, ticker_map)
        else:
            logger.error("Failed to fetch BSE SME scrip codes")

    if sme_scrip_codes:
        bse_result = fetch_bse_sme_announcements(sme_scrip_codes, sent_ids=sent_ids)
        if bse_result is not None:
            bse_announcements = bse_result
            bse_ok = True

    # ── Handle total failure ──
    if not nse_ok and not bse_ok:
        error_msg = (
            f"❌ <b>SME Bot Error</b>\n\n"
            f"📅 {now.strftime('%d-%m-%Y')} ⏰ {now.strftime('%H:%M')} IST\n"
            f"Failed to fetch from both NSE and BSE. Will retry on next run."
        )
        send_telegram(error_msg)
        return

    # ── Tag with exchange ──
    tagged = [("nse", ann) for ann in nse_announcements] + [("bse", ann) for ann in bse_announcements]

    # ── Filter new ──
    new_tagged = [(ex, ann) for ex, ann in tagged if get_announcement_id(ann, ex) not in sent_ids]
    nse_new = sum(1 for ex, _ in new_tagged if ex == "nse")
    bse_new = sum(1 for ex, _ in new_tagged if ex == "bse")

    logger.info("Total: %d, already sent: %d, new: %d (NSE: %d, BSE: %d)",
                len(tagged), len(sent_ids), len(new_tagged), nse_new, bse_new)

    if not new_tagged:
        logger.info("No new announcements to send")
        if is_first_run_today:
            send_telegram(
                f"ℹ️ <b>SME Bot Status</b>\n\n"
                f"📅 {now.strftime('%d-%m-%Y')} ⏰ {now.strftime('%H:%M')} IST\n"
                f"📊 NSE: {len(nse_announcements)} | BSE: {len(bse_announcements)}\n"
                f"✅ No new announcements today"
            )
        return

    # ── Send header ──
    run_type = "FIRST RUN — ALL SME ANNOUNCEMENTS" if is_first_run_today else "INCREMENTAL SME UPDATE"
    exchange_warning = ""
    if not nse_ok:
        exchange_warning = "\n⚠️ NSE fetch failed — BSE only"
    elif not bse_ok:
        exchange_warning = "\n⚠️ BSE fetch failed — NSE only"

    send_telegram(
        f"📢 <b>{run_type}</b>\n\n"
        f"📅 {now.strftime('%d-%m-%Y')} ⏰ {now.strftime('%H:%M')} IST\n"
        f"📊 New: {len(new_tagged)} (NSE: {nse_new}, BSE: {bse_new}){exchange_warning}"
    )
    time.sleep(TELEGRAM_DELAY)

    # ── Send tables grouped by exchange ──
    nse_group = [(ex, ann) for ex, ann in new_tagged if ex == "nse"]
    bse_group = [(ex, ann) for ex, ann in new_tagged if ex == "bse"]

    total_sent = 0

    for exchange_label, group in [("NSE", nse_group), ("BSE", bse_group)]:
        if not group:
            continue
        sent = send_announcements_as_table(group, exchange_label, ticker_map)
        total_sent += sent

        # Mark all as sent
        for ex, ann in group:
            ann_id = get_announcement_id(ann, ex)
            sent_ids.add(ann_id)
        save_sent_announcements(sent_ids)

    # ── Summary ──
    nse_sent = sum(1 for aid in sent_ids if aid.startswith("nse_"))
    bse_sent = sum(1 for aid in sent_ids if aid.startswith("bse_"))

    send_telegram(
        f"✅ <b>COMPLETE</b>\n\n"
        f"📤 Sent today: {len(sent_ids)} (NSE: {nse_sent}, BSE: {bse_sent})\n"
        f"⏰ {now.strftime('%H:%M')} IST"
    )

    logger.info("=" * 60)
    logger.info("DONE")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
