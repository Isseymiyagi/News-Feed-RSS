import sqlite3
import urllib.request
import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse
import traceback
import feedparser
from groq import Groq, RateLimitError, BadRequestError
from openai import OpenAI, RateLimitError as DeepInfraRateLimitError, APIStatusError as DeepInfraAPIStatusError, APIConnectionError as DeepInfraConnectionError, APITimeoutError as DeepInfraTimeoutError
import time
from email.utils import parsedate_to_datetime
from datetime import timezone
import os




# ==========================================
# CONFIGURATION & SOURCES
# ==========================================
DB_NAME = "news_vault.db"
MAX_ENTRIES_PER_FEED = 50  # Number of RSS items considered per source, NOT processing capacity.
MAX_PROCESSING_MINUTES = 30  # Maximum LLM processing time for one workflow run.
MAX_ARTICLES_PER_RUN = 600   # Safety ceiling; the time/API budget usually limits throughput.
MAX_COOLDOWN_WAIT_SECONDS = 120  # Wait for brief 429 cooldowns, not daily exhaustion.
DEEPINFRA_DAILY_ESTIMATED_BUDGET_USD = 0.50  # Local estimated usage ceiling, not a provider-enforced spend limit.
DEEPINFRA_MAX_OUTPUT_TOKENS = 1024

# ==========================================
# STAGE 5: DATABASE SETUP
# ==========================================
def initialize_database():
    """Create the database and migrate older news_vault.db files without deleting articles."""
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, source TEXT, raw_content TEXT, summary TEXT,
            country_code TEXT, timestamp TEXT, link_url TEXT,
            topic_tag TEXT, image_url TEXT
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    extra_columns = {
        "processing_status": "TEXT NOT NULL DEFAULT 'completed'",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
        "last_error": "TEXT",
        "processed_at": "TEXT",
        "model_used": "TEXT",
    }
    for name, definition in extra_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {definition}")

    # Previous script wrote a failure message as if it were a successful summary.
    conn.execute("""
        UPDATE articles
        SET processing_status = 'retry', summary = NULL,
            country_code = NULL, topic_tag = NULL, next_attempt_at = NULL,
            last_error = 'Legacy Groq failure placeholder; queued for reprocessing'
        WHERE summary = 'Failed to generate summary automatically.'
    """)
    # Crash recovery: a job interrupted mid-request is eligible for retry.
    conn.execute("""
        UPDATE articles SET processing_status = 'retry', next_attempt_at = NULL
        WHERE processing_status = 'processing'
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_queue ON articles(processing_status, next_attempt_at, id)")
    # Persistent estimate ledger survives scheduled runs, to avoid unbounded paid overflow.
    conn.execute("""CREATE TABLE IF NOT EXISTS paid_inference_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        charged_utc_date TEXT NOT NULL,
        article_id INTEGER NOT NULL,
        model TEXT NOT NULL,
        reserved_usd REAL NOT NULL,
        actual_usd REAL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paid_inference_day ON paid_inference_ledger(charged_utc_date)")
    conn.commit()
    return conn


# ==========================================
# STAGE 4: GROQ PRIMARY; DEEPINFRA PAID OVERFLOW
# ==========================================
GROQ_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
]
DEEPINFRA_MODEL = "openai/gpt-oss-120b"
DEEPINFRA_INPUT_PRICE_PER_MILLION = 0.037
DEEPINFRA_OUTPUT_PRICE_PER_MILLION = 0.17
MODEL_ORDER = [f"groq/{m}" for m in GROQ_MODELS] + [f"deepinfra/{DEEPINFRA_MODEL}"]
model_cooldowns = {}

groq_key = os.environ.get("GROQ_API_KEY")
deepinfra_key = os.environ.get("DEEPINFRA_API_KEY")
groq_client = Groq(api_key=groq_key, max_retries=0) if groq_key else None
deepinfra_client = OpenAI(
    api_key=deepinfra_key,
    base_url="https://api.deepinfra.com/v1/openai",
    max_retries=0,
    timeout=40,
) if deepinfra_key else None

class AllModelsUnavailable(Exception):
    """No currently available provider successfully processed this article."""

def retry_after_seconds(error, default=60.0):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    header = headers.get("retry-after") if headers else None
    if header:
        try:
            return max(1.0, float(header))
        except (ValueError, TypeError):
            try:
                retry_time = parsedate_to_datetime(header)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return max(1.0, retry_time.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    return default

def parse_article_json(text, model):
    if not text:
        raise ValueError(f"Empty JSON response from {model}")
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("JSON response is not an object")
    summary, country, topic = (result.get(k) for k in ("summary", "country_code", "topic"))
    allowed = {"Sports", "Business", "Economics", "Finance", "Technology", "Conflict", "Culture", "General"}
    if not all(isinstance(value, str) and value.strip() for value in (summary, country, topic)):
        raise ValueError("Missing summary, country_code, or topic")
    topic = topic.strip().capitalize()
    country = country.strip().upper()
    if topic not in allowed:
        raise ValueError(f"Invalid topic: {topic}")
    if not re.fullmatch(r"[A-Z]{3}|GLOBAL", country):
        raise ValueError(f"Invalid ISO country code: {country}")
    return summary.strip(), country, topic

def estimate_deepinfra_reservation(messages):
    # Conservative *estimate*, not a guaranteed token/spend cap. Reserve in
    # advance; the model may use more input tokens than this estimate.
    input_chars = sum(len(m["content"]) for m in messages)
    input_tokens = max(2000, (input_chars + 2) // 3 + 300)
    return (input_tokens * DEEPINFRA_INPUT_PRICE_PER_MILLION +
            DEEPINFRA_MAX_OUTPUT_TOKENS * DEEPINFRA_OUTPUT_PRICE_PER_MILLION) / 1_000_000

def deepinfra_budget_available(conn, estimate):
    today = datetime.now(timezone.utc).date().isoformat()
    spent = conn.execute("""
        SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0)
        FROM paid_inference_ledger WHERE charged_utc_date = ?
    """, (today,)).fetchone()[0]
    return spent + estimate <= DEEPINFRA_DAILY_ESTIMATED_BUDGET_USD

def reserve_deepinfra(conn, article_id, estimate):
    if not deepinfra_budget_available(conn, estimate):
        return None
    now = datetime.now(timezone.utc)
    cursor = conn.execute("""
        INSERT INTO paid_inference_ledger
            (charged_utc_date, article_id, model, reserved_usd, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (now.date().isoformat(), article_id, DEEPINFRA_MODEL, estimate, now.isoformat()))
    conn.commit()
    return cursor.lastrowid

def record_deepinfra_usage(conn, ledger_id, completion):
    usage = getattr(completion, "usage", None)
    if not usage:
        return
    # DeepInfra may provide estimated_cost; fall back to token-based pricing.
    cost = getattr(usage, "estimated_cost", None)
    if cost is None:
        inp = getattr(usage, "prompt_tokens", None)
        out = getattr(usage, "completion_tokens", None)
        if inp is None or out is None:
            return
        cost = (inp * DEEPINFRA_INPUT_PRICE_PER_MILLION +
                out * DEEPINFRA_OUTPUT_PRICE_PER_MILLION) / 1_000_000
    conn.execute("UPDATE paid_inference_ledger SET actual_usd = ? WHERE id = ?",
                 (float(cost), ledger_id))
    conn.commit()
    print(f"   💵 DeepInfra estimated request cost: ${float(cost):.6f}")

def query_local_llm(title, content, conn, article_id):
    """Use Groq capacity first; DeepInfra handles overflow without losing rows."""
    prompt = f"""
You are an AI intelligence agent sorting global news.
Title: {title}
Content: {(content or '')[:3000]}

Summarize in exactly two factual bullet points; identify the primary country
with its three-letter ISO code (or GLOBAL); classify into exactly ONE of:
Sports, Business, Economics, Finance, Technology, Conflict, Culture, General.
Respond ONLY with valid JSON:
{{"summary":"- Bullet 1\\n- Bullet 2","country_code":"ISO_CODE","topic":"TOPIC_NAME"}}
"""
    messages = [
        {"role": "system", "content": "You are a strict news extraction engine. Return only JSON with keys summary, country_code, topic."},
        {"role": "user", "content": prompt},
    ]
    if not groq_client and not deepinfra_client:
        raise RuntimeError("Configure GROQ_API_KEY and/or DEEPINFRA_API_KEY")

    for model in MODEL_ORDER:
        is_paid = model.startswith("deepinfra/")
        if (is_paid and not deepinfra_client) or (not is_paid and not groq_client):
            continue
        remaining = model_cooldowns.get(model, 0) - time.time()
        if remaining > 0:
            print(f"   ⏭️ {model} cooling down ({remaining:.0f}s remaining)")
            continue
        ledger_id = None
        try:
            if is_paid:
                estimate = estimate_deepinfra_reservation(messages)
                ledger_id = reserve_deepinfra(conn, article_id, estimate)
                if ledger_id is None:
                    # Prevent repeated attempts during the current run.
                    model_cooldowns[model] = time.time() + 86400
                    print("   💰 DeepInfra local daily estimated budget reached; retaining queue")
                    continue
                print(f"   💳 Trying DeepInfra paid overflow: {DEEPINFRA_MODEL}")
                response = deepinfra_client.chat.completions.create(
                    model=DEEPINFRA_MODEL,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=DEEPINFRA_MAX_OUTPUT_TOKENS,
                )
                record_deepinfra_usage(conn, ledger_id, response)
            else:
                print(f"   🤖 Trying Groq model: {model}")
                response = groq_client.chat.completions.create(
                    model=model.removeprefix("groq/"),
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    timeout=20,
                )
            summary, country, topic = parse_article_json(response.choices[0].message.content, model)
            print(f"   ✅ LLM succeeded with {model}")
            return summary, country, topic, model
        except (RateLimitError, DeepInfraRateLimitError) as exc:
            delay = retry_after_seconds(exc)
            model_cooldowns[model] = time.time() + delay
            print(f"   ⚠️ {model} rate-limited; cooldown {delay:.0f}s; trying fallback")
        except (BadRequestError, DeepInfraAPIStatusError) as exc:
            # Groq JSON generation errors and DeepInfra 4xx/5xx. Auth errors
            # are configuration problems: surface rather than silently retry.
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise
            if status in (429, 500, 502, 503, 504):
                model_cooldowns[model] = time.time() + retry_after_seconds(exc)
                print(f"   ⚠️ {model} API unavailable ({status}); trying fallback")
                continue
            if "json_validate_failed" in str(exc):
                print(f"   ⚠️ {model} could not produce valid JSON; trying fallback")
                continue
            raise
        except (DeepInfraConnectionError, DeepInfraTimeoutError) as exc:
            model_cooldowns[model] = time.time() + 60
            print(f"   ⚠️ DeepInfra connection/timeout: {exc}; queued for retry if needed")
        except (ValueError, KeyError, IndexError, AttributeError) as exc:
            print(f"   ⚠️ Invalid model output from {model}: {exc}; trying fallback")
    raise AllModelsUnavailable("All configured models unavailable or produced invalid output")


# ==========================================
# STAGE 3: DATA INGESTION ENGINE
# ==========================================
def clean_html(raw_html):
    """Helper function to strip HTML tags from RSS content descriptions."""
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html)

class _FirstImageParser(HTMLParser):
    """Extract the first actual <img> URL from RSS HTML descriptions."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image_url = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img" or self.image_url:
            return
        attributes = dict(attrs)
        for name in ("src", "data-src"):
            url = attributes.get(name)
            if url and urlparse(url).scheme in ("http", "https"):
                self.image_url = url
                return



def extract_image_url(entry):
    """Extract image URLs safely from different RSS formats."""

    def valid_url(url):
        return (
            isinstance(url, str)
            and url.startswith(("https://", "http://"))
        )

    def as_dict(item):
        return item if isinstance(item, dict) else {}

    # 1. Media RSS
    for item in entry.get("media_content", []) or []:
        media = as_dict(item)
        url = media.get("url")
        media_type = str(media.get("type") or "").lower()
        medium = str(media.get("medium") or "").lower()

        if valid_url(url) and (
            media_type.startswith("image/")
            or medium == "image"
            or (not media_type and medium != "video")
        ):
            return url

    # 2. Thumbnails
    for item in entry.get("media_thumbnail", []) or []:
        thumbnail = as_dict(item)
        url = thumbnail.get("url")

        if valid_url(url):
            return url

    # 3. Enclosures and links
    for field in ("enclosures", "links"):
        for item in entry.get(field, []) or []:
            media = as_dict(item)
            url = media.get("href") or media.get("url")
            media_type = str(media.get("type") or "").lower()

            if valid_url(url) and (
                media_type.startswith("image/")
                or (
                    not media_type
                    and url.split("?")[0].lower().endswith(
                        (".jpg", ".jpeg", ".png", ".webp", ".gif")
                    )
                )
            ):
                return url

    # 4. HTML images in descriptions
    sections = [
        entry.get("summary", ""),
        entry.get("description", "")
    ]

    for item in entry.get("content", []) or []:
        if isinstance(item, dict):
            sections.append(item.get("value", ""))
        elif isinstance(item, str):
            sections.append(item)

    for html in sections:
        if not isinstance(html, str) or not html:
            continue

        parser = _FirstImageParser()
        parser.feed(html)

        if parser.image_url:
            return parser.image_url

    return None




def collect_articles(conn):
    """Collect RSS entries to SQLite first, independently of Groq availability."""
    cursor = conn.cursor()
    print(f"[{datetime.now()}] RSS collection started.")
    UPDATED_SOURCES = [
        {
             "name": "BBC News (Global Edition)", 
            "url": "http://newsrss.bbc.co.uk/rss/newsonline_uk_edition/world/rss.xml"
        },
        {
            "name": "Al Jazeera English", 
            "url": "https://www.aljazeera.com/xml/rss/all.xml"
        },
        {
            "name": "The Economist (International)", 
            "url": "http://www.economist.com/rss/international_rss.xml"
        },
        {
            "name": "Semafor", 
            "url": "https://www.semafor.com/rss.xml"
        },
        {
            "name": "Reuters", 
            "url": "https://news.google.com/rss/search?q=when:24h+site:reuters.com&ceid=US:en&hl=en-US&gl=US"
        },
        {
            "name": "Financial Times", 
            "url": "https://www.ft.com/?format=rss"
        },
        {
            "name": "The Economist (World This Week)", 
            "url": "https://www.economist.com/the-world-this-week/rss.xml"
        },
        {
            "name": "Deutsche Welle", 
            "url": "http://rss.dw.com/rdf/rss-en-all"
        },
        {
            "name": "Investing.com", 
            "url": "https://www.investing.com/rss/news.rss"
        },
        {
            "name": "X: All_Source_News",
            "url": "https://rss.app/feeds/PHomIbMQAn8pLuHf.xml"

        },
        {
            "name": "X: The Iran Watcher",
            "url": "https://rss.app/feeds/m7Asihe4jsVF0T8k.xml"
        },
        {
            "name": "Telegram: Middle East Observer",
            "url": "https://rss.app/feeds/CNNIvVSz8LVU0qaz.xml"
        },
        {
            "name": "Instagram: Atlas News",
            "url": "https://rss.app/feeds/Nl4o5QWWCDXRSzKj.xml"
        },
        {
            "name": "War Fronts",
            "url":"https://fronts.co/rss.xml"
        }

    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    collected = 0
    for source in UPDATED_SOURCES:
        print(f"\n--- Fetching Source: {source['name']} ---")
        try:
            req = urllib.request.Request(source['url'], headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                raw_xml_data = response.read()
            feed = feedparser.parse(raw_xml_data)
            print(f"Found {len(feed.entries)} articles in {source['name']}.")

            for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
                if not isinstance(entry, dict):
                    continue
                title = entry.get('title') or 'No Title'
                link_url = entry.get('link') or ''
                raw_content = clean_html(entry.get('description') or entry.get('summary') or '')
                cursor.execute("""
                    SELECT id FROM articles
                    WHERE (link_url = ? AND ? <> '') OR (title = ? AND source = ?)
                    LIMIT 1
                """, (link_url, link_url, title, source['name']))
                if cursor.fetchone():
                    continue  # Existing pending, retry and completed rows must not be duplicated.

                image_url = extract_image_url(entry)
                if not image_url:
                    image_ids = {
                        "BBC News (Global Edition)": "https://images.unsplash.com/photo-1675973094287-f4af3e49bb3d?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                        "Al Jazeera English": "https://images.unsplash.com/photo-1558623920-92843c867e51?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                        "Reuters": "https://images.unsplash.com/photo-1767621560261-de03d2edecdd?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                        "Financial Times (World)": "https://images.unsplash.com/photo-1621264448270-9ef00e88a935?q=80&w=657&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                        "The Economist (World This Week)": "https://images.unsplash.com/photo-1785314981488-8345bc70dc92?q=80&w=1373&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                        "Semafor": "https://images.unsplash.com/photo-1595194512722-9a5003713e41?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                        "Deutsche Welle": "https://images.unsplash.com/photo-1618259278412-2819cbdea4dc?q=80&w=1421&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D",
                    }
                    image_url = image_ids.get(source['name'], image_ids['BBC News (Global Edition)'])

                cursor.execute("""
                    INSERT INTO articles
                        (title, source, raw_content, timestamp, link_url, image_url, processing_status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """, (title, source['name'], raw_content, datetime.now().isoformat(), link_url, image_url))
                conn.commit()  # Save EACH item immediately; a later failure cannot erase it.
                collected += 1
        except Exception as exc:
            print(f"❌ RSS error from {source['name']}: {exc}")
            traceback.print_exc()
    print(f"RSS collection finished: {collected} new queued articles.")


def process_pending_articles(conn):
    """Work through pending/retry rows within a time budget, pausing for short 429s."""
    processed = 0
    started = time.monotonic()
    deadline = started + MAX_PROCESSING_MINUTES * 60

    while processed < MAX_ARTICLES_PER_RUN and time.monotonic() < deadline:
        # When all models are cooling down, wait until the nearest one is available.
        # Waiting here (before claiming a row) avoids incrementing attempt_count
        # simply because a model is temporarily unavailable.
        delays = [max(0.0, model_cooldowns.get(m, 0) - time.time())
                  for m in MODEL_ORDER if (deepinfra_client if m.startswith("deepinfra/") else groq_client)]
        if delays and all(d > 0 for d in delays):
            wait = min(delays)
            remaining_budget = deadline - time.monotonic()
            if wait > MAX_COOLDOWN_WAIT_SECONDS or wait + 1 >= remaining_budget:
                print(f"   ⏸️ Models unavailable for {wait:.0f}s; leaving queue intact for next run.")
                break
            print(f"   ⏳ All models cooling down; waiting {wait:.1f}s before continuing...")
            time.sleep(wait + 0.2)
            continue

        now = datetime.now().isoformat()
        item = conn.execute("""
            SELECT id, title, raw_content, attempt_count FROM articles
            WHERE processing_status IN ('pending', 'retry')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY timestamp DESC, id DESC LIMIT 1
        """, (now,)).fetchone()
        if not item:
            break

        article_id, title, content, previous_attempts = item
        conn.execute("""
            UPDATE articles SET processing_status = 'processing',
                attempt_count = attempt_count + 1, last_error = NULL
            WHERE id = ?
        """, (article_id,))
        conn.commit()

        try:
            summary, country, topic, model = query_local_llm(title, content or '', conn, article_id)
            conn.execute("""
                UPDATE articles
                SET summary = ?, country_code = ?, topic_tag = ?,
                    processing_status = 'completed', processed_at = ?,
                    model_used = ?, next_attempt_at = NULL, last_error = NULL
                WHERE id = ?
            """, (summary, country, topic, datetime.now().isoformat(), model, article_id))
            conn.commit()
            processed += 1
            print(f"   ✅ Article {article_id} completed with {model}")

        except AllModelsUnavailable as exc:
            # Leave this article in SQLite; keep trying in THIS run only if a
            # rate-limited model will become available shortly.
            future = [model_cooldowns[m] for m in MODEL_ORDER
                      if model_cooldowns.get(m, 0) > time.time()]
            delay = max(5, min(future) - time.time()) if future else 60
            retry_at = (datetime.now() + timedelta(seconds=delay + 2)).isoformat()
            conn.execute("""
                UPDATE articles SET processing_status = 'retry',
                    next_attempt_at = ?, last_error = ? WHERE id = ?
            """, (retry_at, str(exc)[:1000], article_id))
            conn.commit()
            print(f"   ⏳ Article {article_id} retained; next attempt after {retry_at}.")
            if not future or delay > MAX_COOLDOWN_WAIT_SECONDS:
                break
            budget = deadline - time.monotonic()
            if delay + 2 >= budget:
                break
            print(f"   ⏳ Waiting {delay:.1f}s for models to recover...")
            time.sleep(delay + 0.2)
            # Other eligible articles can proceed while this row's retry date
            # is in the future; the failed row will be retried when due.

        except Exception as exc:
            attempts = previous_attempts + 1
            delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
            retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat()
            conn.execute("""
                UPDATE articles SET processing_status = 'retry',
                    next_attempt_at = ?, last_error = ? WHERE id = ?
            """, (retry_at, str(exc)[:1000], article_id))
            conn.commit()
            print(f"   ⚠️ Article {article_id} queued for retry at {retry_at}: {exc}")

    stats = dict(conn.execute("""
        SELECT processing_status, COUNT(*) FROM articles GROUP BY processing_status
    """).fetchall())
    elapsed = (time.monotonic() - started) / 60
    print(f"Processing finished: {processed} completed in {elapsed:.1f} min. Queue totals: {stats}")


def run_ingestion_pipeline():
    """Fetch first; then process the durable queue, including older failed articles."""
    conn = initialize_database()
    try:
        collect_articles(conn)
        process_pending_articles(conn)
    finally:
        conn.close()
    print(f"[{datetime.now()}] Ingestion cycle finished.")


if __name__ == "__main__":
    run_ingestion_pipeline()
