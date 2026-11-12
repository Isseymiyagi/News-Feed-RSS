import sqlite3
import urllib.request
import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse
import traceback
import feedparser
from groq import Groq, RateLimitError
import time
from email.utils import parsedate_to_datetime
from datetime import timezone
import os




# ==========================================
# CONFIGURATION & SOURCES
# ==========================================
DB_NAME = "news_vault.db"
MAX_ENTRIES_PER_FEED = 50

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
    conn.commit()
    return conn


# ==========================================
# STAGE 4: GROQ LLM WITH AUTOMATIC MODEL FALLBACK
# ==========================================
# All models must support response_format={"type": "json_object"}.
GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

# Remember rate-limited models throughout this ingestion process.
model_cooldowns = {}

client = Groq(
    api_key=os.environ.get("GROQ_API_KEY"),
    max_retries=0,
)

class AllModelsUnavailable(Exception):
    """No configured Groq model can process the article right now."""


def retry_after_seconds(error):
    """Read Groq's Retry-After header (seconds or HTTP date)."""
    response = getattr(error, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header:
        try:
            return max(1.0, float(header))
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(header)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return max(1.0, retry_time.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    return 60.0  # Conservative fallback when Groq omits Retry-After.


def query_local_llm(title, content):
    """Summarize and categorize an article, switching models on 429 errors."""
    truncated_content = content[:3000]
    prompt = f"""
    You are an AI intelligence agent sorting global news.
    Analyze this news item:
    Title: {title}
    Content: {truncated_content}

    Your job:
    1. Summarize it into exactly 2 punchy bullet points.
    2. Extract the main country it focuses on and provide its 3-letter ISO country code (e.g., USA, JPN, BRA, GBR). If it is global, use 'GLOBAL'.
    3. Categorize this article into EXACTLY ONE of the following topics: Sports, Business, Economics, Finance, Technology, Conflict, Culture, or General.

    Respond with a valid JSON object only, without markdown:
    {{
        "summary": "- Bullet 1\\n- Bullet 2",
        "country_code": "ISO_CODE",
        "topic": "TOPIC_NAME"
    }}
    """

    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict data-extraction engine. Respond with JSON only, "
                "with keys summary, country_code, and topic."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    for model in GROQ_MODELS:
        remaining = model_cooldowns.get(model, 0) - time.time()
        if remaining > 0:
            print(f"   ⏭️ {model} cooling down ({remaining:.0f}s remaining)")
            continue

        try:
            print(f"   🤖 Trying Groq model: {model}")
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=20,
            )
            result = json.loads(completion.choices[0].message.content)
            summary = result.get("summary")
            country = result.get("country_code")
            topic = result.get("topic")
            allowed_topics = {
                "Sports", "Business", "Economics", "Finance", "Technology",
                "Conflict", "Culture", "General",
            }
            if not all(isinstance(v, str) and v.strip() for v in (summary, country, topic)):
                raise ValueError("Model returned missing/empty JSON fields")
            topic = topic.strip().capitalize()
            if topic not in allowed_topics:
                raise ValueError(f"Unexpected topic from {model}: {topic}")
            print(f"   ✅ LLM succeeded with {model}")
            return summary, country.strip().upper(), topic, model

        except RateLimitError as exc:
            delay = retry_after_seconds(exc)
            model_cooldowns[model] = time.time() + delay
            print(f"   ⚠️ {model} rate-limited; cooldown {delay:.0f}s. Switching models...")
        except (ValueError, json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
            print(f"   ⚠️ Invalid model output from {model}: {exc}. Switching models...")
        # Authentication, connection, and other API failures propagate to caller
        # instead of silently writing incorrect labels to SQLite.

    raise AllModelsUnavailable("All Groq models are rate-limited or returned invalid output")


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
    """Process oldest due jobs, retrying later without losing the originating record."""
    processed = 0
    while True:
        now = datetime.now().isoformat()
        item = conn.execute("""
            SELECT id, title, raw_content, attempt_count FROM articles
            WHERE processing_status IN ('pending', 'retry')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id ASC LIMIT 1
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
            # Return the model used as well, to make fallback activity auditable.
            summary, country, topic, model = query_local_llm(title, content or '')
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
            # Respect each model's Retry-After; don't hammer every pending row.
            future = [t for t in model_cooldowns.values() if t > time.time()]
            delay = max(60, min(future) - time.time()) if future else 300
            retry_at = (datetime.now() + timedelta(seconds=delay + 5)).isoformat()
            conn.execute("""
                UPDATE articles SET processing_status = 'retry',
                    next_attempt_at = ?, last_error = ? WHERE id = ?
            """, (retry_at, str(exc)[:1000], article_id))
            conn.commit()
            print(f"   ⏳ Article {article_id} retained; next attempt after {retry_at}.")
            break  # Other queued articles stay pending until the next run.
        except Exception as exc:
            # Retry transient network, timeout and unexpected errors, with capped backoff.
            attempts = previous_attempts + 1
            delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
            retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat()
            conn.execute("""
                UPDATE articles SET processing_status = 'retry',
                    next_attempt_at = ?, last_error = ? WHERE id = ?
            """, (retry_at, str(exc)[:1000], article_id))
            conn.commit()
            print(f"   ⚠️ Article {article_id} queued for retry at {retry_at}: {exc}")
            # Continue with other due articles rather than losing this one.

    stats = dict(conn.execute("""
        SELECT processing_status, COUNT(*) FROM articles GROUP BY processing_status
    """).fetchall())
    print(f"Processing finished: {processed} completed this run. Queue totals: {stats}")


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
