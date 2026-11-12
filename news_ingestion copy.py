import sqlite3
import urllib.request
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse
import traceback
import feedparser
from groq import Groq
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
    """Creates the SQLite database and news table if they don't exist."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            source TEXT,
            raw_content TEXT,
            summary TEXT,
            country_code TEXT,
            timestamp TEXT,
            link_url TEXT,
            topic_tag TEXT,
            image_url TEXT
        );  

    ''')
    conn.commit()
    conn.close()

# ==========================================
# STAGE 4: LOCAL LLM ARCHITECTURE (OLLAMA)
# ==========================================



# Pulls your API key securely from GitHub Secrets (or your local environment)
client = Groq(
    api_key=os.environ.get("GROQ_API_KEY")
)

def query_local_llm(title, content):
    """
    Sends raw article text to Groq Cloud LLM for rapid processing.
    Replaces the heavy local Ollama engine to eliminate performance drops.
    """
    # Slice the content slightly to prevent huge payloads, though Groq can handle it
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

        You MUST respond with a valid JSON object only. Do not wrap it in markdown code blocks.
    JSON Format:
    {{
        "summary": "- Bullet 1\\n- Bullet 2",
        "country_code": "ISO_CODE",
        "topic": "TOPIC_NAME"
    }}
    """
    
    try:
        # Changed the model to a globally active Groq developer-tier model
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",  # <-- Change this string here
            messages=[
                {
                    "role": "system", 
                    "content": (
                        "You are a strict data-extraction engine. You MUST respond with a valid JSON object only. "
                        "Do not wrap it in markdown backticks or code blocks. "
                        "Match this exact JSON schema: "
                        "{\"summary\": \"- Bullet 1\\n- Bullet 2\", \"country_code\": \"ISO_CODE\"}"
                    )
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            timeout=15 
        )

        
        # Parse the JSON string returned by Groq
        result_json = json.loads(completion.choices[0].message.content)
        
        # Safely extract and return the values just like your original code did
        return result_json.get("summary", "No summary generated."), result_json.get("country_code", "GLOBAL").upper(), result_json.get("topic", "General").capitalize()

    except Exception as e:
        print(f"   ⚠️ Error querying Groq Cloud LLM: {e}")
        return "Failed to generate summary automatically.", "GLOBAL", "General"


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




def run_ingestion_pipeline():
    """Main execution block running 3 times a day via scheduler."""
    initialize_database()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    print(f"[{datetime.now()}] Ingestion cycle started.")
    
    # Updated directly to robust open-access endpoints
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
    
    for source in UPDATED_SOURCES:
        print(f"\n--- Fetching Source: {source['name']} ---")
        try:
            req = urllib.request.Request(source['url'], headers=headers)
            with urllib.request.urlopen(req, timeout=20) as response:
                raw_xml_data = response.read()
            
            feed = feedparser.parse(raw_xml_data)
            if source["name"].startswith("X:"):
                print("RSS.app feed title:", feed.feed.get("title", "Missing"))
                print("Parser error:", feed.get("bozo_exception", "None"))
                print("Response preview:", raw_xml_data[:500].decode(
                    "utf-8", errors="replace"
                ))
            print(f"Success! Found {len(feed.entries)} total articles in this feed.")
            
            if len(feed.entries) == 0:
                continue
            
            

            for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
                if not isinstance(entry, dict):
                    print(f"Skipping invalid entry: {type(entry).__name__}")
                    continue

                print("Entry type:", type(entry).__name__)
                print("Entry preview:", repr(entry)[:200])

                title = entry.get("title", "No Title")

                

                raw_content = clean_html(entry.get('description', entry.get('summary', '')))
                
                # Check for duplicate titles to prevent database clutter
                link_url = entry.get('link', '')
                cursor.execute("""
                    SELECT id FROM articles
                    WHERE (link_url = ? AND ? <> '')
                       OR (title = ? AND source = ?)
                    LIMIT 1
                """, (link_url, link_url, title, source['name']))
                if cursor.fetchone():
                    print(f" ⏭️ Skipping duplicate: {title[:40]}...")
                    continue
                
                print(f" + Processing brand new article: {title[:40]}")
                print("   Sending text to Groq Cloud LLM...")
                
                summary, country_code, topic_tag = query_local_llm(title, raw_content)
                print(f"   LLM Result -> Country: {country_code} | Topic: {topic_tag} | Summary: {summary[:40]}...")
                
                # =========================================================
                # 🚀 ADVANCED DYNAMIC IMAGE EXTRACTION LAYER
                # =========================================================
                image_url = None
                
                # Pattern A: Look in the media_thumbnail extension (BBC standard)
                if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
                    image_url = entry.media_thumbnail[0].get('url')
                    
                # Pattern B: Look in the media_content list (Al Jazeera, DW, FT standard)
                elif 'media_content' in entry and len(entry.media_content) > 0:
                    image_url = entry.media_content[0].get('url')
                    
                # Pattern C: Loop through traditional download enclosure array variants
                elif 'links' in entry:
                    for link in entry.links:
                        if 'image' in link.get('type', ''):
                            image_url = link.get('href')
                            break

                # Pattern D: Fallback to high-quality curated publisher placeholders
                if not image_url:
                    image_ids = {
                        "BBC News (Global Edition)": "https://images.unsplash.com/photo-1675973094287-f4af3e49bb3d?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Red/White news theme
                        "Al Jazeera English": "https://images.unsplash.com/photo-1558623920-92843c867e51?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Gold/Dark geo theme
                        "Reuters": "https://images.unsplash.com/photo-1767621560261-de03d2edecdd?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Classic monochrome print
                        "Financial Times (World)": "https://images.unsplash.com/photo-1621264448270-9ef00e88a935?q=80&w=657&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Salmon pink / Stock chart theme
                        "The Economist (World This Week)": "https://images.unsplash.com/photo-1785314981488-8345bc70dc92?q=80&w=1373&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Red/Deep blue digital print
                        "Semafor": "https://images.unsplash.com/photo-1595194512722-9a5003713e41?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Bright yellow/Neon aesthetic
                        "Deutsche Welle": "https://images.unsplash.com/photo-1618259278412-2819cbdea4dc?q=80&w=1421&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D", # Clean tech blue
                    }
                    image_url= image_ids.get(source['name'], "https://images.unsplash.com/photo-1675973094287-f4af3e49bb3d?q=80&w=1470&auto=format&fit=crop&ixlib=rb-4.1.0&ixid=NHwxMjA3fDA%3D")
                    

                    

                # =========================================================

                cursor.execute('''
                    INSERT OR IGNORE INTO articles (title, source, raw_content, summary, country_code, timestamp, link_url, topic_tag, image_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (title, source['name'], raw_content, summary, country_code, datetime.now().isoformat(), entry.get('link', ''), topic_tag, image_url))
                
                print(f"   Database Status: Saved under Topic: [{topic_tag}]")
                print(f"   Database Status: Row processed successfully (Row status count: {cursor.rowcount})")

                
            conn.commit() # Save transactions after completing each feed cluster
        except Exception as e:
            print(f"❌ Error processing {source['name']}: {e}")
            traceback.print_exc()
    

            
    conn.close()
    print(f"\n[{datetime.now()}] Ingestion cycle completed successfully.")

if __name__ == "__main__":
    run_ingestion_pipeline()
