import os
import io
import re
import json
import time
import random
import requests
import zipfile
import gzip as _gzip
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

import fitz
from groq import Groq
from google import genai
from google.genai import types
from fpdf import FPDF

# Google Drive API Libraries
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ========================================================================
# LOAD CONFIGURATION
# ========================================================================
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
    CFG = json.load(_f)

GROQ_API_KEY        = CFG["api_keys"]["groq_api_key"]
GEMINI_API_KEY      = CFG["api_keys"]["gemini_api_key"]
TELEGRAM_BOT_TOKEN  = CFG["telegram"]["bot_token"]
TELEGRAM_CHAT_ID    = CFG["telegram"]["chat_id"]

BATCH_CHUNK_SIZE    = CFG["pipeline_settings"]["batch_chunk_size"]
OUTPUT_DIR_NAME     = CFG["pipeline_settings"]["output_folder_name"]
JSON_FILE           = CFG["pipeline_settings"]["json_log_file"]
MAX_TEXT_CHARS      = CFG["pipeline_settings"]["max_text_chars_per_doc"]

# Window settings from config
MARKET_OPEN_HOUR    = CFG["window_settings"]["market_open_hour"]
MARKET_OPEN_MINUTE  = CFG["window_settings"]["market_open_minute"]
MARKET_CLOSE_HOUR   = CFG["window_settings"]["market_close_hour"]
MARKET_CLOSE_MINUTE = CFG["window_settings"]["market_close_minute"]

# Debug override: set to 1/2/3 in config.json to test with last N hours of data.
# Set to 0 for normal 24-hour dual-window production mode.
DEBUG_OVERRIDE_HOURS = CFG["pipeline_settings"].get("debug_override_hours", 0)

GROQ_INTER_BATCH_DELAY = CFG["rate_limits"]["groq_inter_batch_delay_seconds"]
GEMINI_RETRY_WAIT      = CFG["rate_limits"]["gemini_retry_wait_seconds"]
BATCH_PAUSE_MIN        = CFG["rate_limits"].get("batch_pause_min_seconds", 20)
BATCH_PAUSE_MAX        = CFG["rate_limits"].get("batch_pause_max_seconds", 30)

NOISE_KEYWORDS = CFG["filter"]["noise_keywords"]
TIER1_PHRASES  = CFG["filter"]["tier1_material_phrases"]
TIER2_PATTERNS = CFG["filter"]["tier2_regex_patterns"]
FILTER_RULES   = CFG["filter"]["rules"]

SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/drive']

# ========================================================================
# AI CLIENTS
# ========================================================================
groq_client   = Groq(api_key=GROQ_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ========================================================================
# NSE NETWORK
# ========================================================================
NSE_BASE_URL = "https://www.nseindia.com"
REFERER_URL  = NSE_BASE_URL + "/companies-listing/corporate-filings-announcements"

BROWSER_HEADERS = {
    "User-Agent"                : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept"                    : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language"           : "en-US,en;q=0.9",
    "Connection"                : "keep-alive",
    "Upgrade-Insecure-Requests" : "1",
    "Sec-Fetch-Dest"            : "document",
    "Sec-Fetch-Mode"            : "navigate",
    "Sec-Fetch-Site"            : "none",
    "Sec-Fetch-User"            : "?1",
    "Cache-Control"             : "max-age=0",
}

API_HEADERS = {
    "User-Agent"       : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept"           : "application/json, text/plain, */*",
    "Accept-Language"  : "en-US,en;q=0.9",
    "Referer"          : REFERER_URL,
    "X-Requested-With" : "XMLHttpRequest",
    "Sec-Fetch-Dest"   : "empty",
    "Sec-Fetch-Mode"   : "cors",
    "Sec-Fetch-Site"   : "same-origin",
}

SYSTEM_INSTRUCTION = """
You are an elite institutional financial research analyst for Indian NSE SME listed companies.
Extract corporate events from filings and output ONLY in the exact pipe-delimited format below.

=======================================================================
MANDATORY OUTPUT FORMAT
=======================================================================

CATEGORY|Full Company Name (TICKER) - Detail with numbers - Sentiment

CATEGORY must be ONE of:
  ACTIONABLE       = Revenue/PAT/EBITDA numbers present OR order with Rs. value OR acquisition
  EVENT            = Board meeting / AGM / EGM / investor meet / earnings call notice
  RESULTS_TODAY    = Financial results ALREADY DECLARED with actual Revenue/PAT figures
  MARKET_SUMMARY   = Expansion / capacity / stake change / order without value / leadership
  CORPORATE_ACTION = Dividend / bonus shares / rights / warrant conversion / ESOP / buyback

DECISION TREE:
  1. Actual Revenue/PAT/EBITDA numbers in filing? → RESULTS_TODAY
  2. Order with Rs. Cr/Lakh value OR acquisition deal? → ACTIONABLE
  3. Board meeting / AGM / EGM / investor meet / earnings call? → EVENT
  4. Dividend / warrant conversion / ESOP grant / bonus / rights? → CORPORATE_ACTION
  5. Everything else material → MARKET_SUMMARY

=======================================================================
CRITICAL MERGING RULE — ONE COMPANY = ONE LINE PER CATEGORY
=======================================================================

If ONE PDF has multiple data points for the SAME company → MERGE into ONE concise line.
NEVER write 3 lines for the same company from the same filing.

WRONG:
  MARKET_SUMMARY|Oriana Power (ORIANA) - 835 MW Solar delivered - Positive
  MARKET_SUMMARY|Oriana Power (ORIANA) - 1000 MWh BESS pipeline - Positive
  MARKET_SUMMARY|Oriana Power (ORIANA) - 2500 MW target FY27 - Positive

CORRECT:
  MARKET_SUMMARY|Oriana Power Limited (ORIANA) - 835+ MW Solar delivered, 700+ MW under execution, 1000+ MWh BESS pipeline, 2,500+ MW target FY27 - Long Term Positive

WRONG (2 results lines):
  RESULTS_TODAY|Oriana Power (ORIANA) - FY26 Revenue Rs.1,81,367 Lakhs - Positive
  RESULTS_TODAY|Oriana Power (ORIANA) - Revenue grew 83.7% YoY - Positive

CORRECT (merged):
  RESULTS_TODAY|Oriana Power Limited (ORIANA) - FY26 Revenue Rs.1,81,367 Lakhs (+83.7% YoY), PAT Rs.25,233.58 Lakhs (+59.1% YoY), EBITDA Rs.42,537 Lakhs (+73.4% YoY), PAT Margin 13.91% - Positive

=======================================================================
RESULTS_TODAY — IMPORTANT
=======================================================================

Use RESULTS_TODAY ONLY for filings with ACTUAL declared numbers (Revenue, PAT figures).
"Board meeting to consider results" = EVENT, NOT RESULTS_TODAY.
If filing has actual Revenue + PAT figures = RESULTS_TODAY.

=======================================================================
CORPORATE_ACTION — IMPORTANT
=======================================================================

Use ONLY for: dividend (Rs./share), bonus (ratio), rights (ratio+price),
warrant conversion (qty+price), ESOP grant (options count), buyback (price+qty).
If no real corporate action in batch — output NOTHING for this category.
Do NOT force corporate actions where none exist.

=======================================================================
FORMAT RULES
=======================================================================

- Full company name + (TICKER) always
- Include ALL key numbers: Rs. Cr/Lakh, %, MW, MTPA, store count, EPS
- Sentiment: Positive / Negative / Neutral / Long Term Positive / Avoid
- One merged line per company per category — NO duplicates
- NO headers, NO markdown, NO bullets, NO intro/outro text
- Zero material events in entire batch → output: NO_MATERIAL_EVENTS

=======================================================================
SENTIMENT
=======================================================================

Positive           → Revenue/PAT growth, order win, expansion, dividend declared
Negative           → Net loss, decline, key resignation, cancellation
Neutral            → Meeting scheduled, ESOP adoption pending approval
Long Term Positive → Multi-year expansion, large capacity pipeline, strategic JV
Avoid              → Widening losses, auditor disclaimer, fraud, regulatory probe

=======================================================================
EXACT EXAMPLE OUTPUT
=======================================================================

RESULTS_TODAY|Oriana Power Limited (ORIANA) - FY26 Revenue Rs.1,81,367 Lakhs (+83.7% YoY), PAT Rs.25,233.58 Lakhs (+59.1% YoY), EBITDA Rs.42,537 Lakhs (+73.4% YoY) - Positive
RESULTS_TODAY|Neetu Yoshi International Limited (NEETUYOSHI) - FY26 net profit Rs.25.01 Cr up 52% YoY - Positive
ACTIONABLE|Master Components Limited (MASTER) - Work order from Alpha Industries Rs.14.50 Cr, delivery Q2FY27 - Positive
EVENT|HOEC Limited (HOEC) - Board meeting June 11, 2026 to consider Q4FY26 results; earnings call June 12 - Neutral
EVENT|CreditAccess Grameen Limited (CREDITACC) - 35th AGM scheduled July 03, 2026 - Neutral
MARKET_SUMMARY|Oriana Power Limited (ORIANA) - 835+ MW Solar delivered, 700+ MW under execution, 1000+ MWh BESS pipeline, 2,500+ MW target FY27 - Long Term Positive
MARKET_SUMMARY|Patel Retail Limited (PATELRET) - Opens 52nd store in Rasulgarh, expanding national footprint - Positive
MARKET_SUMMARY|Sagar Cements Limited (SAGCEM) - Increased group cement capacity to 11.00 MTPA with new unit - Positive
CORPORATE_ACTION|Transsteel Seating Limited (TRANSTEEL) - 2,50,000 warrants converted to equity at Rs.80/share, face value Rs.10 - Positive
CORPORATE_ACTION|EMA Partners India Limited (EMAPARTNER) - ESOP Scheme 2026: 15,00,000 options granted to eligible employees - Neutral
CORPORATE_ACTION|Kirloskar Ferrous Industries Limited (KIRLFER) - Board to consider final dividend for FY26 - Positive
"""

# ========================================================================
# GOOGLE DRIVE AUTOMATION LOGIC
# ========================================================================
def get_drive_service():
    creds = None
    token_file         = CFG.get("google_drive", {}).get("token_file", "token.json")
    client_secret_file = CFG.get("google_drive", {}).get("client_secret_file", "client_secret.json")

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_file):
                raise FileNotFoundError(f"Missing Google Cloud secret file: {client_secret_file}")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, 'w') as token:
            token.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def get_or_create_drive_folder(service, folder_name, parent_id=None):
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    items   = results.get('files', [])
    if items:
        return items[0]['id']

    folder_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
    if parent_id:
        folder_metadata['parents'] = [parent_id]

    folder = service.files().create(body=folder_metadata, fields='id').execute()
    return folder['id']


def upload_file_to_drive(service, parent_folder_id, filename, file_bytes, mime_type="application/pdf"):
    file_metadata = {'name': filename, 'parents': [parent_folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    file  = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')


# ========================================================================
# 24-HOUR DUAL WINDOW LOGIC
# ========================================================================
def get_dual_windows():
    """
    Returns two windows that together cover exactly 24 hours:

    Window 1 — Market Hours:
        Yesterday market_open_hour:market_open_minute
        → Yesterday market_close_hour:market_close_minute

    Window 2 — After Hours:
        Yesterday market_close_hour:market_close_minute
        → Today market_open_hour:market_open_minute  (= script run time ~8:00 AM)

    GitHub Actions runs this daily at market open time so that
    the two windows together cover the full previous 24 hours
    without missing any announcement.
    """
    now       = datetime.now()
    today     = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    # Window 1: yesterday 08:00 AM → yesterday 03:30 PM
    w1_start = yesterday.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    )
    w1_end = yesterday.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0, microsecond=0
    )

    # Window 2: yesterday 03:30 PM → today 08:00 AM (now)
    w2_start = w1_end
    w2_end   = today.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0, microsecond=0
    )

    return w1_start, w1_end, w2_start, w2_end


# ========================================================================
# PDF TEXT CLEANER
# ========================================================================
def clean_pdf_text(raw_text: str) -> str:
    lines         = raw_text.split("\n")
    clean         = []
    skip_patterns = [
        r"^\s*page\s+\d+",
        r"^\s*\d+\s*$",
        r"cin\s*[:\-]\s*[a-z]\d{5}",
        r"isin\s*[:\-]\s*[a-z]{2}\d",
        r"^\s*www\.",
        r"bse\s+scrip\s+code",
        r"national\s+stock\s+exchange",
        r"bombay\s+stock\s+exchange",
        r"^\s*tel\s*[:\-\.]",
        r"^\s*fax\s*[:\-\.]",
        r"corporate\s+identification\s+number",
        r"^\s*email\s*[:\-]",
        r"registered\s+office\s*[:\-]",
        r"^\s*[A-Z]{2}-\d{6}",
    ]

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if len(line_stripped) < 4:
            continue
        skip = False
        for pat in skip_patterns:
            if re.search(pat, line_stripped, re.IGNORECASE):
                skip = True
                break
        if not skip:
            clean.append(line_stripped)

    seen_lines = []
    prev = None
    for ln in clean:
        if ln != prev:
            seen_lines.append(ln)
            prev = ln

    return "\n".join(seen_lines)


# ========================================================================
# PDF TEXT EXTRACTOR (RAM — no disk write)
# ========================================================================
def extract_text_and_tables_from_stream(pdf_bytes: bytes) -> str:
    try:
        doc       = fitz.open(stream=pdf_bytes, filetype="pdf")
        all_parts = []

        for page_num, page in enumerate(doc):
            plain_text = page.get_text("text")
            if plain_text.strip():
                all_parts.append(plain_text)

            page_dict = page.get_text("dict")
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_text = " ".join(
                        span.get("text", "").strip()
                        for span in line.get("spans", [])
                        if span.get("text", "").strip()
                    )
                    if line_text and any(ch.isdigit() for ch in line_text):
                        if line_text not in plain_text:
                            all_parts.append(f"[TABLE P{page_num+1}]: {line_text}")

        doc.close()
        raw = "\n".join(all_parts)
        return clean_pdf_text(raw)

    except Exception as e:
        print(f"         [PyMuPDF Error] {e}")
        return ""


# ========================================================================
# MATERIALITY FILTER
# ========================================================================
def is_material(text: str) -> bool:
    min_chars = FILTER_RULES["min_text_length_chars"]
    if not text or len(text.strip()) < min_chars:
        return False

    text_lower     = text.lower()
    noise_score    = sum(1 for kw in NOISE_KEYWORDS if kw in text_lower)
    tier1_score    = sum(1 for ph in TIER1_PHRASES  if ph in text_lower)
    tier2_score    = sum(1 for pat in TIER2_PATTERNS if re.search(pat, text_lower))
    material_score = tier1_score + tier2_score

    if noise_score >= FILTER_RULES["noise_threshold_for_rejection"] and material_score == 0:
        return False
    if material_score == 0:
        return False
    if (noise_score >= FILTER_RULES["noise_dominant_threshold"]
            and material_score <= FILTER_RULES["noise_dominant_material_max"]):
        return False
    if tier1_score >= 1:
        return True
    if tier2_score >= FILTER_RULES["tier2_min_matches_if_no_tier1"]:
        return True
    return False


# ========================================================================
# GROQ AI — Primary summarizer
# ========================================================================
def fetch_groq_batch_summary(payload: str) -> str | None:
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user",   "content": (
                    "Process these NSE SME filings. "
                    "Extract material events in the CATEGORY|format specified:\n\n" + payload
                )}
            ],
            temperature=0.05,
            max_tokens=1500
        )
        if response.choices and response.choices[0].message.content:
            result = response.choices[0].message.content.strip()
            return "" if result == "NO_MATERIAL_EVENTS" else result
        return ""
    except Exception as e:
        if any(kw in str(e).lower() for kw in ["rate", "429", "token", "limit", "quota"]):
            print(f"      [Groq] Rate limit — switching to Gemini...")
            return None
        print(f"      [Groq Error] {e}")
        return None


# ========================================================================
# GEMINI AI — Fallback summarizer
# ========================================================================
def fetch_gemini_batch_summary(payload: str) -> str:
    for attempt in range(1, 4):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=(
                    "Process these NSE SME filings. "
                    "Extract material events in the CATEGORY|format specified:\n\n" + payload
                ),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.05,
                    max_output_tokens=1500,
                )
            )
            if response.text:
                result = response.text.strip()
                return "" if result == "NO_MATERIAL_EVENTS" else result
            return ""
        except Exception as e:
            if any(kw in str(e) for kw in ["429", "RESOURCE_EXHAUSTED", "503"]):
                print(f"      [Gemini] Attempt {attempt}/3 busy. Waiting {GEMINI_RETRY_WAIT}s...")
                time.sleep(GEMINI_RETRY_WAIT)
                continue
            print(f"      [Gemini Error] {e}")
            return ""
    print("      [Gemini] All attempts failed.")
    return ""


def fetch_ai_summary(payload: str) -> str:
    result = fetch_groq_batch_summary(payload)
    if result is None:
        time.sleep(2)
        result = fetch_gemini_batch_summary(payload)
    return result if result else ""


# ========================================================================
# STEP 1: FETCH NSE SME ANNOUNCEMENTS FOR A GIVEN WINDOW
# ========================================================================
def fetch_announcements_for_window(session, start_window: datetime, end_window: datetime) -> list:
    """
    Fetches all NSE SME announcements that fall within [start_window, end_window].
    Uses a date range slightly wider than the window to ensure no records are missed.
    Returns a list of filtered announcement dicts.
    """
    nse_from = (start_window - timedelta(days=1)).strftime("%d-%m-%Y")
    nse_to   = end_window.strftime("%d-%m-%Y")
    api_url  = (NSE_BASE_URL + "/api/corporate-announcements?index=sme"
                + "&from_date=" + nse_from + "&to_date=" + nse_to)

    print(f"\n   API: ?from_date={nse_from}&to_date={nse_to}")

    raw_records = None
    for attempt in range(1, 4):
        try:
            print(f"   Fetching (attempt {attempt}/3)...")
            response = session.get(api_url, timeout=(10, 35), stream=False)
            print(f"   HTTP {response.status_code} | Content-Type: {response.headers.get('Content-Type','?')}")

            if response.status_code != 200:
                time.sleep(5)
                continue

            ct = response.headers.get("Content-Type", "")
            if "html" in ct.lower():
                print("   [WARNING] HTML response (blocked). Retrying in 10s...")
                time.sleep(10)
                continue

            response.encoding = "utf-8"
            try:
                raw_records = response.json()
                print(f"   JSON OK — {len(raw_records) if isinstance(raw_records, list) else 'dict'} records")
                break
            except Exception as je:
                try:
                    decoded     = _gzip.decompress(response.content).decode("utf-8")
                    raw_records = json.loads(decoded)
                    print(f"   JSON OK (gzip manual) — {len(raw_records)} records")
                    break
                except Exception:
                    print(f"   [WARNING] Parse failed ({je}). Retrying in 10s...")
                    time.sleep(10)
        except Exception as e:
            print(f"   [WARNING] Request error: {e}. Retrying in 5s...")
            time.sleep(5)

    if raw_records is None:
        print("   [ERROR] All attempts failed.")
        return []

    if isinstance(raw_records, dict):
        raw_records = raw_records.get("data", raw_records.get("rows", []))
    if not isinstance(raw_records, list):
        print("   [ERROR] Unexpected response format.")
        return []

    filtered_data = []
    seen_links    = set()
    count         = 1

    for item in raw_records:
        if not isinstance(item, dict):
            continue
        date_str = item.get("an_dt")
        link     = item.get("attchmntFile", "")
        symbol   = item.get("symbol", "UNKNOWN")
        if not date_str:
            continue
        try:
            an_dt = datetime.strptime(date_str, "%d-%b-%Y %H:%M:%S")
        except ValueError:
            continue
        if start_window <= an_dt <= end_window and link not in seen_links:
            seen_links.add(link)
            print(f"   [{count:02d}] {symbol:<14} | {date_str}")
            filtered_data.append({
                "symbol"      : symbol,
                "companyName" : item.get("sm_name", symbol),
                "datetime"    : date_str,
                "link"        : link
            })
            count += 1

    return filtered_data


def get_all_announcements():
    """
    DEBUG MODE  (debug_override_hours > 0 in config.json):
        Single window = last N hours. Good for quick testing.
        Set debug_override_hours: 1, 2, or 3 in config.json.

    PRODUCTION MODE (debug_override_hours = 0):
        Dual window covering exact 24 hours:
          Window 1 (Market Hours) : Yesterday 08:00 AM to Yesterday 03:30 PM
          Window 2 (After Hours)  : Yesterday 03:30 PM to Today 08:00 AM
        GitHub Actions triggers this daily at 08:00 AM IST.

    Returns: (session, w1_start, w1_end, w2_start, w2_end, w1_items, w2_items)
    """
    now = datetime.now()

    if DEBUG_OVERRIDE_HOURS and DEBUG_OVERRIDE_HOURS > 0:
        # Debug single window: last N hours only
        w1_start = now - timedelta(hours=DEBUG_OVERRIDE_HOURS)
        w1_end   = now
        w2_start = now  # empty — debug mode has no Window 2
        w2_end   = now
        print("\n" + "=" * 70)
        print("  STEP 1: FETCHING NSE SME ANNOUNCEMENTS  [DEBUG MODE]")
        print(f"  Last {DEBUG_OVERRIDE_HOURS} hour(s) | Set debug_override_hours:0 for production")
        print(f"  Window : {w1_start.strftime('%d-%b-%Y %I:%M %p')} to {w1_end.strftime('%d-%b-%Y %I:%M %p')}")
        print("=" * 70)
    else:
        # Production: dual window covering full 24 hours
        w1_start, w1_end, w2_start, w2_end = get_dual_windows()
        print("\n" + "=" * 70)
        print("  STEP 1: FETCHING NSE SME ANNOUNCEMENTS  [PRODUCTION 24hr]")
        print(f"  Window 1 (Market Hours) : {w1_start.strftime('%d-%b-%Y %I:%M %p')} to {w1_end.strftime('%d-%b-%Y %I:%M %p')}")
        print(f"  Window 2 (After Hours)  : {w2_start.strftime('%d-%b-%Y %I:%M %p')} to {w2_end.strftime('%d-%b-%Y %I:%M %p')}")
        print("=" * 70)

    # Build one shared session with homepage + announcements page warm-up for valid cookies
    session = requests.Session()
    retry_strategy = Retry(total=4, backoff_factor=2,
                           status_forcelist=[429, 500, 502, 503, 504],
                           raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://",  adapter)
    session.mount("https://", adapter)
    session.headers.update(BROWSER_HEADERS)

    try:
        print("   Connecting to NSE (homepage)...")
        r1 = session.get(NSE_BASE_URL, timeout=(10, 30))
        print(f"   Homepage: HTTP {r1.status_code} | Cookies: {len(session.cookies)}")
        time.sleep(2)
    except Exception as e:
        print(f"   [WARNING] Homepage failed: {e}")

    try:
        print("   Loading announcements page...")
        r2 = session.get(REFERER_URL, timeout=(10, 30))
        print(f"   Announcements page: HTTP {r2.status_code}")
        time.sleep(2)
    except Exception as e:
        print(f"   [WARNING] Announcements page failed: {e}")

    session.headers.update(API_HEADERS)
    session.headers.pop("Accept-Encoding", None)

    # Fetch Window 1 (always)
    label_w1 = f"Debug Last {DEBUG_OVERRIDE_HOURS}hr" if (DEBUG_OVERRIDE_HOURS and DEBUG_OVERRIDE_HOURS > 0) else "Market Hours"
    print(f"\n   --- Fetching Window 1 ({label_w1}) ---")
    w1_items = fetch_announcements_for_window(session, w1_start, w1_end)
    print(f"   Window 1 total: {len(w1_items)} announcements")

    # Fetch Window 2 only in production mode
    w2_items = []
    if not (DEBUG_OVERRIDE_HOURS and DEBUG_OVERRIDE_HOURS > 0):
        time.sleep(3)
        print(f"\n   --- Fetching Window 2 (After Hours) ---")
        w2_items = fetch_announcements_for_window(session, w2_start, w2_end)
        print(f"   Window 2 total: {len(w2_items)} announcements")

    # Save combined log to JSON for reference
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump({"window1": w1_items, "window2": w2_items}, f, indent=4)

    return session, w1_start, w1_end, w2_start, w2_end, w1_items, w2_items



# ========================================================================
# STEP 2: STREAM FILTER & UPLOAD TO GOOGLE DRIVE
# ========================================================================
def stream_filter_and_save(filtered_items: list, session) -> tuple:
    """
    For each announcement:
      1. Download the PDF into RAM (no local disk save)
      2. Extract text using PyMuPDF
      3. Run materiality filter
      4. If material → upload to Google Drive
      5. Return (useful_list, discarded_list)
    """
    print("\n" + "=" * 70)
    print("  STEP 2: SMART STREAM FILTER")
    print("=" * 70)

    drive_service  = get_drive_service()
    root_folder_id = get_or_create_drive_folder(drive_service, OUTPUT_DIR_NAME)
    print(f"   [Google Drive] Target Root Verified: '{OUTPUT_DIR_NAME}' (ID: {root_folder_id})")

    useful_list    = []
    discarded_list = []
    total          = len(filtered_items)

    for idx, item in enumerate(filtered_items, start=1):
        symbol       = item.get("symbol", "UNKNOWN")
        raw_name     = item.get("companyName", symbol)
        ann_time     = item.get("datetime", "")
        link         = item.get("link", "")
        company_name = raw_name.replace("/", "-").replace("\\", "-").replace(":", "-").strip()

        print(f"\n   [{idx:02d}/{total}] {symbol} — {company_name}")

        if not link:
            print(f"         Skip — No file link")
            discarded_list.append((symbol, company_name, "No file attachment"))
            continue

        try:
            filing_dt = datetime.strptime(ann_time, "%d-%b-%Y %H:%M:%S")
            month_str = filing_dt.strftime("%B %Y")
            time_str  = ann_time.replace(":", "-")
            filename  = f"{symbol}_{time_str}.pdf"
        except Exception as e:
            print(f"         Skip — Date error: {e}")
            discarded_list.append((symbol, company_name, "Date parse error"))
            continue

        try:
            print(f"         Fetching...", end="", flush=True)
            res = session.get(link, timeout=(10, 35))
            if res.status_code != 200:
                print(f" HTTP {res.status_code}")
                discarded_list.append((symbol, company_name, f"Download failed HTTP {res.status_code}"))
                continue
            downloaded_bytes = res.content
            print(f" {len(downloaded_bytes)//1024} KB")
        except Exception as e:
            print(f" Error: {e}")
            discarded_list.append((symbol, company_name, "Download error"))
            continue

        # Handle ZIP archives that contain the actual PDF inside
        pdf_bytes = None
        if link.lower().endswith(".zip") or zipfile.is_zipfile(io.BytesIO(downloaded_bytes)):
            try:
                print("         [ZIP Detected] Extracting PDF from archive...")
                with zipfile.ZipFile(io.BytesIO(downloaded_bytes)) as z:
                    pdf_names = [f for f in z.namelist() if f.lower().endswith(".pdf")]
                    if pdf_names:
                        pdf_bytes = z.read(pdf_names[0])
                    else:
                        print("         Discard — No PDF found inside ZIP")
                        discarded_list.append((symbol, company_name, "ZIP archive contains no PDF files"))
                        continue
            except Exception as ze:
                print(f"         [ZIP Error] Extraction failed: {ze}")
                discarded_list.append((symbol, company_name, "ZIP extraction error"))
                continue
        else:
            pdf_bytes = downloaded_bytes

        extracted_text = extract_text_and_tables_from_stream(pdf_bytes)
        if not extracted_text.strip():
            print(f"         Discard — Empty/image PDF")
            discarded_list.append((symbol, company_name, "Scanned/image-only PDF"))
            continue

        _n  = sum(1 for kw in NOISE_KEYWORDS if kw in extracted_text.lower())
        _t1 = sum(1 for ph in TIER1_PHRASES  if ph in extracted_text.lower())
        _t2 = sum(1 for pat in TIER2_PATTERNS if re.search(pat, extracted_text.lower()))
        print(f"         noise={_n} | tier1={_t1} | tier2={_t2} | chars={len(extracted_text)}")

        if not is_material(extracted_text):
            print(f"         Discard — Compliance noise")
            discarded_list.append((symbol, company_name, "Compliance noise (e-voting/AGM/routine)"))
            continue

        print(f"         MATERIAL — Uploading to Drive...")
        try:
            # Drive folder hierarchy: Root > Company Name > Month Year
            company_folder_id = get_or_create_drive_folder(drive_service, company_name, root_folder_id)
            month_folder_id   = get_or_create_drive_folder(drive_service, month_str, company_folder_id)
            file_id           = upload_file_to_drive(drive_service, month_folder_id, filename, pdf_bytes)
            print(f"         [Drive Saved] File ID: {file_id}")
            useful_list.append((symbol, ann_time, extracted_text, f"DRIVE:{file_id}", link))
        except Exception as e:
            print(f"         Google Drive Save error: {e}")
            useful_list.append((symbol, ann_time, extracted_text, "SAVE_FAILED", link))

    print(f"\n   Result: {len(useful_list)} material | {len(discarded_list)} discarded")
    return useful_list, discarded_list


# ========================================================================
# STEP 3: BATCH AI PROCESSING
# ========================================================================
def run_ai_batch_processing(useful_extracted: list) -> str:
    print("\n" + "=" * 70)
    print("  STEP 3: AI BATCH PROCESSING")
    print(f"  Batch: {BATCH_CHUNK_SIZE} filings | Pause: {BATCH_PAUSE_MIN}-{BATCH_PAUSE_MAX}s | Max chars/filing: {MAX_TEXT_CHARS}")
    print("=" * 70)

    total         = len(useful_extracted)
    total_batches = (total + BATCH_CHUNK_SIZE - 1) // BATCH_CHUNK_SIZE
    consolidated  = ""
    batch_num     = 1

    for i in range(0, total, BATCH_CHUNK_SIZE):
        chunk   = useful_extracted[i : i + BATCH_CHUNK_SIZE]
        payload = ""

        for symbol, ann_time, text, _, _ in chunk:
            truncated = text[:MAX_TEXT_CHARS]
            payload  += f"\n{'='*60}\n"
            payload  += f"FILING | TICKER: {symbol} | TIME: {ann_time}\n"
            payload  += truncated
            payload  += f"\nEND | {symbol}\n"

        tickers = [c[0] for c in chunk]
        print(f"\n   Batch {batch_num}/{total_batches} — {', '.join(tickers)}")

        result = fetch_ai_summary(payload)

        if result and result.strip():
            consolidated += result.strip() + "\n"
            lines = [l for l in result.split("\n") if l.strip()]
            print(f"   Done — {len(lines)} lines")
        else:
            print(f"   No material events in this batch")

        batch_num += 1
        if i + BATCH_CHUNK_SIZE < total:
            pause = random.randint(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
            print(f"   Pausing {pause}s...")
            time.sleep(pause)

    return consolidated.strip()


# ========================================================================
# STEP 4A: PARSE AI OUTPUT INTO SECTIONS
# ========================================================================
def parse_ai_output(ai_text: str, link_map: dict) -> dict:
    sections = {
        "actionable"       : [],
        "events"           : [],
        "results_today"    : [],
        "market_summary"   : [],
        "corporate_actions": [],
    }
    category_map = {
        "ACTIONABLE"      : "actionable",
        "EVENT"           : "events",
        "RESULTS_TODAY"   : "results_today",
        "MARKET_SUMMARY"  : "market_summary",
        "CORPORATE_ACTION": "corporate_actions",
    }
    for raw_line in ai_text.split("\n"):
        line = raw_line.strip().lstrip("- ").strip()
        if not line:
            continue
        if "|" in line:
            parts    = line.split("|", 1)
            category = parts[0].strip().upper()
            content  = parts[1].strip()
        else:
            category = "MARKET_SUMMARY"
            content  = line
        ticker_match = re.search(r'\(([A-Z0-9\-&]+)\)', content)
        pdf_link = ""
        if ticker_match:
            pdf_link = link_map.get(ticker_match.group(1), "")
        bucket = category_map.get(category, "market_summary")
        sections[bucket].append({"text": content, "link": pdf_link})
    return sections


# ========================================================================
# PDF HELPERS
# ========================================================================
def clean_for_pdf(text: str) -> str:
    # Dashes and quotes
    text = text.replace('\u2013', '-').replace('\u2014', '-')
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2022', '-').replace('\u00b7', '-')
    # Arrows — AI output mein aate hain (→ ← ↑ ↓ ⇒ ⇐)
    text = text.replace('\u2192', '->').replace('\u2190', '<-')
    text = text.replace('\u2191', '^').replace('\u2193', 'v')
    text = text.replace('\u21d2', '=>').replace('\u21d0', '<=')
    text = text.replace('\u2794', '->').replace('\u27a1', '->')
    # Bullets and misc symbols
    text = text.replace('\u2023', '-').replace('\u25cf', '-')
    text = text.replace('\u25cb', '-').replace('\u25aa', '-')
    text = text.replace('\u2714', '+').replace('\u2713', '+')
    text = text.replace('\u2715', 'x').replace('\u2716', 'x')
    text = text.replace('\u20b9', 'Rs.').replace('\u20ac', 'EUR')
    text = text.replace('\u00d7', 'x').replace('\u00f7', '/')
    # Markdown cleanup
    text = text.replace('**', '').replace('*', '')
    # Final safety net: drop any remaining non-latin-1 characters
    text = text.encode('latin-1', 'ignore').decode('latin-1')
    return text


def split_sentiment(text: str):
    sentiments = ["Long Term Positive", "Positive", "Negative", "Avoid", "Neutral"]
    for s in sentiments:
        suffix = f" - {s}"
        if text.endswith(suffix):
            return text[: -len(suffix)].strip(), s
    return text.strip(), ""


def _get_font_paths():
    import urllib.request
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_reg  = os.path.join(script_dir, "DejaVuSans.ttf")
    local_bold = os.path.join(script_dir, "DejaVuSans-Bold.ttf")
    local_itl  = os.path.join(script_dir, "DejaVuSans-Oblique.ttf")
    local_emj  = os.path.join(script_dir, "NotoColorEmoji.ttf")

    sys_reg  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    sys_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    sys_itl  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"
    sys_emj  = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"

    reg  = sys_reg  if os.path.exists(sys_reg)  else local_reg
    bold = sys_bold if os.path.exists(sys_bold) else local_bold
    itl  = sys_itl  if os.path.exists(sys_itl)  else local_itl
    emj  = sys_emj  if os.path.exists(sys_emj)  else local_emj

    sources = {
        reg : "https://raw.githubusercontent.com/bitfocus/companion/refs/heads/main/assets/fonts/DejaVuSans.ttf",
        bold: "https://raw.githubusercontent.com/bitfocus/companion/refs/heads/main/assets/fonts/DejaVuSans-Bold.ttf",
        itl : "https://github.com/dejavu-fonts/dejavu-fonts/raw/refs/heads/main/TTF/DejaVuSans-Oblique.ttf",
        emj : None,
    }

    for path, url in sources.items():
        if not os.path.exists(path) and url:
            try:
                print(f"   Downloading font: {os.path.basename(path)}...")
                urllib.request.urlretrieve(url, path)
            except Exception as e:
                print(f"   Font download failed ({os.path.basename(path)}): {e}")

    return (
        reg  if os.path.exists(reg)  else None,
        bold if os.path.exists(bold) else None,
        itl  if os.path.exists(itl)  else None,
        emj  if os.path.exists(emj)  else None,
    )

FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_EMOJI = _get_font_paths()


# ========================================================================
# STEP 4B: PDF GENERATOR — dual window in single file
# ========================================================================
class MarketBriefPDF(FPDF):
    def __init__(self, overall_start: datetime, overall_end: datetime, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.overall_start = overall_start
        self.overall_end   = overall_end

    def _setup_fonts(self):
        try:
            self.add_font("DJ", fname=FONT_REGULAR)
            self.add_font("DJ", style="B", fname=FONT_BOLD)
            self.add_font("DJ", style="I", fname=FONT_ITALIC)
            if FONT_EMOJI and os.path.exists(FONT_EMOJI):
                self.add_font("Emoji", fname=FONT_EMOJI)
                self.set_fallback_fonts(["Emoji"])
            self._fonts_ready = True
        except Exception:
            self._fonts_ready = False

    def _f(self, style="", size=9):
        if getattr(self, "_fonts_ready", False):
            self.set_font("DJ", style=style, size=size)
        else:
            h_style = {"B": "B", "I": "I", "": ""}.get(style, "")
            self.set_font("Helvetica", h_style, size)

    def header(self):
        self._setup_fonts()
        self._f("B", 14)
        self.set_text_color(30, 30, 30)
        self.cell(0, 10, "NSE SME Announcements", align="C", ln=True)
        self._f("", 9)
        self.set_text_color(100, 100, 100)
        date_str = (
            f"Daily Market Brief  |  "
            f"{self.overall_start.strftime('%d-%b-%Y %I:%M %p')}"
            f"  to  "
            f"{self.overall_end.strftime('%d-%b-%Y %I:%M %p')}"
        )
        self.cell(0, 6, date_str, align="C", ln=True)
        self.ln(2)
        self.set_draw_color(180, 180, 180)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self._f("I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"Auto-generated by NSE SME Pipeline v3.0  |  Page {self.page_no()}", align="C")

    def window_header(self, title: str, time_range: str, is_market_hours: bool):
        """Prints a bold colored section divider for each window."""
        self.ln(3)
        # Blue accent bar for market hours, gray for after hours
        if is_market_hours:
            self.set_fill_color(26, 86, 219)   # blue
        else:
            self.set_fill_color(107, 114, 128)  # gray
        self.cell(0, 1.5, "", ln=True, fill=True)
        self.ln(2)

        self._f("B", 11)
        self.set_text_color(30, 30, 30)
        self.cell(0, 7, f"  {title}", ln=True)

        self._f("", 9)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, f"  {time_range}", ln=True)
        self.ln(3)

    def section_title(self, title: str):
        self._f("B", 10)
        self.set_fill_color(240, 240, 240)
        self.set_text_color(30, 30, 30)
        self.cell(0, 7, f"  {title}", ln=True, fill=True)
        self.ln(2)

    def body_text(self, text: str):
        self._f("", 9)
        self.set_text_color(50, 50, 50)
        self.multi_cell(0, 5.5, clean_for_pdf(text))
        self.ln(2)

    def no_events_placeholder(self):
        """Shown when a section has zero entries."""
        self._f("I", 8.5)
        self.set_text_color(180, 180, 180)
        self.cell(0, 5.5, "  No material events in this window.", ln=True)
        self.ln(2)

    def summary_line(self, main_text: str, sentiment: str, pdf_url: str = ""):
        color_map = {
            "Positive"          : (21, 128, 61),
            "Long Term Positive": (21, 128, 61),
            "Negative"          : (185, 28, 28),
            "Avoid"             : (185, 28, 28),
            "Neutral"           : (50, 50, 50),
        }
        line_color = color_map.get(sentiment, (50, 50, 50))

        parts        = main_text.split(" - ", 1)
        company_part = clean_for_pdf(parts[0].strip())
        detail_part  = clean_for_pdf(parts[1].strip()) if len(parts) == 2 else ""

        self._f("", 9)
        self.set_text_color(*line_color)
        self.write(5.5, "- ")
        self.write(5.5, company_part)

        if detail_part:
            self.write(5.5, f" - {detail_part}")

        if sentiment:
            self.write(5.5, f" - {sentiment}")

        if pdf_url:
            self._f("", 9)
            self.set_text_color(0, 102, 204)
            self.write(5.5, "  [Link]", link=pdf_url)

        self.ln(5.5)
        self.ln(1)


def _render_window_sections(pdf: MarketBriefPDF, sections: dict, show_filtered: bool, discarded_list: list):
    """
    Renders the 5 content sections for one window.
    show_filtered=True  → shows "Filtered Out" section (after-hours window)
    show_filtered=False → hides "Filtered Out" section (market-hours window)
    """

    # Actionable
    pdf.section_title("Actionable")
    if sections["actionable"]:
        parts_list = []
        for entry in sections["actionable"]:
            text = entry["text"]
            main, sentiment = split_sentiment(text)
            p       = main.split(" - ", 1)
            company = clean_for_pdf(p[0])
            detail  = clean_for_pdf(p[1]) if len(p) > 1 else ""
            short   = (detail[:55] + "...") if len(detail) > 55 else detail
            parts_list.append(f"{company} ({short})" if short else company)
        pdf.body_text(", ".join(parts_list) + ".")
    else:
        pdf.no_events_placeholder()

    # Events
    pdf.section_title("Events")
    if sections["events"]:
        events_parts = []
        for entry in sections["events"]:
            main, _ = split_sentiment(entry["text"])
            events_parts.append(clean_for_pdf(main))
        pdf.body_text("  \xb7  ".join(events_parts))
    else:
        pdf.no_events_placeholder()

    # Market Summary
    pdf.section_title("Market Summary")
    if sections["market_summary"]:
        for entry in sections["market_summary"]:
            main, sentiment = split_sentiment(entry["text"])
            pdf.summary_line(main, sentiment, entry["link"])
        pdf.ln(1)
    else:
        pdf.no_events_placeholder()

    # Results Today
    pdf.section_title("Results Today")
    if sections["results_today"]:
        for entry in sections["results_today"]:
            main, sentiment = split_sentiment(entry["text"])
            pdf.summary_line(main, sentiment, entry["link"])
        pdf.ln(1)
    else:
        pdf.no_events_placeholder()

    # Corporate Actions
    pdf.section_title("Corporate Actions")
    if sections["corporate_actions"]:
        for entry in sections["corporate_actions"]:
            main, sentiment = split_sentiment(entry["text"])
            pdf.summary_line(main, sentiment, entry["link"])
        pdf.ln(1)
    else:
        pdf.no_events_placeholder()

    # Filtered Out — only shown in after-hours window
    if show_filtered and discarded_list:
        pdf.ln(2)
        pdf.section_title("Filtered Out (Not Material)")
        for sym, cname, reason in discarded_list:
            line = clean_for_pdf(f"{sym} ({cname}) - {reason}")
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(150, 150, 150)
            pdf.multi_cell(0, 4.5, f"  - {line}")
        pdf.ln(2)


def generate_pdf(
    sections_w1: dict,
    sections_w2: dict,
    discarded_list: list,
    w1_start: datetime, w1_end: datetime,
    w2_start: datetime, w2_end: datetime
) -> tuple:
    """
    Generates a single PDF with two sections:
      - Window 1: Market Hours (no Filtered Out)
      - Window 2: After Hours  (with Filtered Out)
    Returns (pdf_bytes, filename).
    """
    pdf = MarketBriefPDF(overall_start=w1_start, overall_end=w2_end)
    pdf.add_page()
    pdf.set_margins(12, 15, 12)
    pdf.set_auto_page_break(auto=True, margin=15)

    # Window 1 — Market Hours
    w1_range = (f"{w1_start.strftime('%d-%b-%Y %I:%M %p')}"
                f"  to  {w1_end.strftime('%d-%b-%Y %I:%M %p')}")
    pdf.window_header("Market Hours", w1_range, is_market_hours=True)
    _render_window_sections(pdf, sections_w1, show_filtered=False, discarded_list=[])

    # Window 2 — After Hours
    w2_range = (f"{w2_start.strftime('%d-%b-%Y %I:%M %p')}"
                f"  to  {w2_end.strftime('%d-%b-%Y %I:%M %p')}")
    pdf.window_header("After Hours", w2_range, is_market_hours=False)
    _render_window_sections(pdf, sections_w2, show_filtered=True, discarded_list=discarded_list)

    # Build filename from overall window dates
    start_str = w1_start.strftime("%d-%b-%Y_%I%M%p")
    end_str   = w2_end.strftime("%d-%b-%Y_%I%M%p")
    filename  = f"NSE_SME_Market_Brief_{start_str}_To_{end_str}.pdf"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    filepath   = os.path.join(script_dir, filename)
    pdf.output(filepath)
    print(f"   PDF saved locally: {filename}")

    with open(filepath, "rb") as f:
        return f.read(), filename


# ========================================================================
# STEP 4C: TELEGRAM DELIVERY
# ========================================================================
def send_to_telegram(
    pdf_bytes: bytes, pdf_name: str,
    w1_material: int, w2_material: int,
    sections_w1: dict, sections_w2: dict,
    w1_start: datetime, w2_end: datetime
):
    print("\n" + "=" * 70)
    print("  STEP 4: SENDING PDF TO TELEGRAM")
    print("=" * 70)

    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    # Count entries per category per window
    def counts(s):
        return (len(s.get("actionable", [])),
                len(s.get("events", [])),
                len(s.get("market_summary", [])),
                len(s.get("results_today", [])),
                len(s.get("corporate_actions", [])))

    a1, ev1, ms1, rt1, ca1 = counts(sections_w1)
    a2, ev2, ms2, rt2, ca2 = counts(sections_w2)

    caption = (
        f"*Daily Market Brief — NSE SME*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 *Period:* {w1_start.strftime('%d-%b-%Y %I:%M %p')} → {w2_end.strftime('%d-%b-%Y %I:%M %p')}\n\n"
        f"*Market Hours Window*\n"
        f"🎯 Actionable: {a1}  |  📋 Events: {ev1}\n"
        f"📊 Market Summary: {ms1}  |  📈 Results: {rt1}  |  🏢 Corp Actions: {ca1}\n"
        f"📁 Filings: {w1_material}\n\n"
        f"*After Hours Window*\n"
        f"🎯 Actionable: {a2}  |  📋 Events: {ev2}\n"
        f"📊 Market Summary: {ms2}  |  📈 Results: {rt2}  |  🏢 Corp Actions: {ca2}\n"
        f"📁 Filings: {w2_material}\n\n"
        f"✅ *Status:* Report Ready"
    )

    try:
        files    = {"document": (pdf_name, io.BytesIO(pdf_bytes))}
        data     = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
        response = requests.post(tg_url, files=files, data=data, timeout=40)
        if response.status_code == 200:
            print("   PDF sent to Telegram successfully.")
        else:
            print(f"   Telegram error — HTTP {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"   Telegram failed: {e}")


# ========================================================================
# MAIN PIPELINE
# ========================================================================
def run_pipeline():
    print("\n" + "=" * 70)
    print("  NSE SME SMART STREAM PIPELINE  v4.0  (24-Hour Dual Window)")
    print(f"  Run Time: {datetime.now().strftime('%d %b %Y | %I:%M:%S %p')}")
    print("=" * 70)

    # Step 1: Fetch both windows
    session, w1_start, w1_end, w2_start, w2_end, w1_items, w2_items = get_all_announcements()

    if not w1_items and not w2_items:
        print("\n[Exit] No announcements found in either window.")
        return

    # Step 2: Filter and upload — Window 1
    print("\n   === Processing Window 1 (Market Hours) ===")
    useful_w1, discarded_w1 = ([], [])
    if w1_items:
        useful_w1, discarded_w1 = stream_filter_and_save(w1_items, session)
    else:
        print("   No announcements in Window 1.")

    # Step 2: Filter and upload — Window 2
    print("\n   === Processing Window 2 (After Hours) ===")
    useful_w2, discarded_w2 = ([], [])
    if w2_items:
        useful_w2, discarded_w2 = stream_filter_and_save(w2_items, session)
    else:
        print("   No announcements in Window 2.")

    # Step 3: AI processing for each window independently
    ai_text_w1 = ""
    if useful_w1:
        print("\n   === AI Processing — Window 1 ===")
        ai_text_w1 = run_ai_batch_processing(useful_w1)

    ai_text_w2 = ""
    if useful_w2:
        print("\n   === AI Processing — Window 2 ===")
        ai_text_w2 = run_ai_batch_processing(useful_w2)

    # Build link maps: ticker → NSE PDF URL
    link_map_w1 = {item[0]: item[4] for item in useful_w1 if len(item) > 4}
    link_map_w2 = {item[0]: item[4] for item in useful_w2 if len(item) > 4}

    # Parse AI output into sections for each window
    sections_w1 = parse_ai_output(ai_text_w1, link_map_w1)
    sections_w2 = parse_ai_output(ai_text_w2, link_map_w2)

    # Step 4: Generate single PDF with both windows
    pdf_bytes, pdf_name = generate_pdf(
        sections_w1, sections_w2,
        discarded_w2,          # Only after-hours discards shown in PDF
        w1_start, w1_end,
        w2_start, w2_end
    )

    # Step 4C: Send to Telegram
    send_to_telegram(
        pdf_bytes, pdf_name,
        len(useful_w1), len(useful_w2),
        sections_w1, sections_w2,
        w1_start, w2_end
    )

    print("\n" + "=" * 70)
    print("  PIPELINE COMPLETE")
    print(f"  Finished: {datetime.now().strftime('%d %b %Y | %I:%M:%S %p')}")
    print("=" * 70)


if __name__ == "__main__":
    run_pipeline()