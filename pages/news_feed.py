import sqlite3
import streamlit as st

DB_PATH = "news_vault.db"
PAGE_SIZE = 25

st.set_page_config(layout="wide", page_title="Global Intel Stream")
st.title("🗲 Global Intelligence Streaming Timeline")
st.caption("Chronological intelligence updates tracking global macro developments.")


def reset_page():
    st.session_state.feed_page = 0


def clear_filters():
    st.query_params.clear()
    st.session_state.keyword_filter = ""
    st.session_state.topic_filter = "All Topics"
    st.session_state.country_filter = "All Countries"
    st.session_state.source_filter = "All Sources"
    st.session_state.period_filter = "Past 24 Hours"
    reset_page()


# Use the incoming map link as the initial country selection.
url_country = st.query_params.get("country", "").upper()

with sqlite3.connect(DB_PATH) as conn:
    available_countries = [
        row[0] for row in conn.execute(
            """
            SELECT DISTINCT UPPER(TRIM(country_code))
            FROM articles
            WHERE country_code IS NOT NULL AND TRIM(country_code) <> ''
            ORDER BY 1
            """
        ).fetchall()
    ]
    available_sources = [
        row[0] for row in conn.execute(
            """
            SELECT DISTINCT TRIM(source)
            FROM articles
            WHERE source IS NOT NULL AND TRIM(source) <> ''
            ORDER BY 1 COLLATE NOCASE
            """
        ).fetchall()
    ]

country_options = ["All Countries"] + available_countries
source_options = ["All Sources"] + available_sources
if url_country and url_country not in country_options:
    country_options.append(url_country)
    country_options = ["All Countries"] + sorted(country_options[1:])

# Detect a new map-link country, but don't overwrite the user's manual filter
# selection on ordinary Streamlit reruns.
if "last_map_country" not in st.session_state or st.session_state.last_map_country != url_country:
    st.session_state.country_filter = url_country if url_country else "All Countries"
    st.session_state.last_map_country = url_country
    reset_page()

st.session_state.setdefault("feed_page", 0)
st.session_state.setdefault("keyword_filter", "")
st.session_state.setdefault("topic_filter", "All Topics")
st.session_state.setdefault("source_filter", "All Sources")
st.session_state.setdefault("period_filter", "Past 24 Hours")

col_search, col_topic, col_country, col_source, col_period, col_clear = st.columns([2, 1, 1, 1.25, 1.2, 1])

with col_search:
    search_query = st.text_input(
        "🔍 Keyword Search", placeholder="Search text...",
        key="keyword_filter", on_change=reset_page,
    )

with col_topic:
    selected_topic = st.selectbox(
        "🎯 Focus Topic",
        ["All Topics", "Business", "Technology", "Conflict", "Sports", "Culture", "General"],
        key="topic_filter", on_change=reset_page,
    )

with col_country:
    selected_country = st.selectbox(
        "🌍 Country Filter", country_options,
        key="country_filter", on_change=reset_page,
    )

with col_source:
    selected_source = st.selectbox(
        "📰 News Source", source_options,
        key="source_filter", on_change=reset_page,
    )

with col_period:
    selected_period = st.selectbox(
        "📅 Time Period",
        ["Past 24 Hours", "Past 7 Days", "Past 30 Days", "Past 90 Days", "All History"],
        key="period_filter", on_change=reset_page,
    )

with col_clear:
    st.write("##")
    if st.button("🔄 Clear Filters", use_container_width=True, on_click=clear_filters):
        pass

if selected_country != "All Countries":
    st.info(f"📍 Filtering by region matrix tag: **{selected_country}**")

# Build one parameterized WHERE clause, reused for the count and page queries.
conditions = ["1=1"]
params = []

# Accommodate the two-letter codes your original feed already supported.
country_aliases = {"USA": "US", "GBR": "GB", "FRA": "FR", "ETH": "ET"}
if selected_country != "All Countries":
    other_code = country_aliases.get(selected_country)
    if other_code:
        conditions.append("country_code IN (?, ?)")
        params.extend([selected_country, other_code])
    else:
        conditions.append("country_code = ?")
        params.append(selected_country)

if selected_topic != "All Topics":
    conditions.append("topic_tag = ?")
    params.append(selected_topic)

if selected_source != "All Sources":
    conditions.append("TRIM(source) = ?")
    params.append(selected_source)

if search_query.strip():
    conditions.append("(title LIKE ? OR summary LIKE ?)")
    term = f"%{search_query.strip()}%"
    params.extend([term, term])

period_to_modifier = {
    "Past 24 Hours": "-1 day",
    "Past 7 Days": "-7 days",
    "Past 30 Days": "-30 days",
    "Past 90 Days": "-90 days",
}
if selected_period in period_to_modifier:
    conditions.append("timestamp >= datetime('now', ?)")
    params.append(period_to_modifier[selected_period])

where_clause = " AND ".join(conditions)

with sqlite3.connect(DB_PATH) as conn:
    total_articles = conn.execute(
        f"SELECT COUNT(*) FROM articles WHERE {where_clause}", params
    ).fetchone()[0]

    total_pages = max(1, (total_articles + PAGE_SIZE - 1) // PAGE_SIZE)
    st.session_state.feed_page = min(st.session_state.feed_page, total_pages - 1)
    page = st.session_state.feed_page

    articles = conn.execute(
        f"""
        SELECT title, source, summary, timestamp, country_code,
               link_url, topic_tag, image_url
        FROM articles
        WHERE {where_clause}
        ORDER BY timestamp DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, PAGE_SIZE, page * PAGE_SIZE],
    ).fetchall()

if not articles:
    st.info("No matching briefings found for these filters.")
else:
    start = page * PAGE_SIZE + 1
    end = min((page + 1) * PAGE_SIZE, total_articles)
    st.caption(f"Showing {start:,}–{end:,} of {total_articles:,} articles")

    for title, source, summary, timestamp, country, link_url, topic, image_url in articles:
        with st.container(border=True):
            col_img, col_text, col_metrics = st.columns([1, 3, 1])
            with col_img:
                if image_url:
                    st.image(image_url, use_container_width=True)
                else:
                    st.caption("🖼️ No image available")
            with col_text:
                st.markdown(f"### [{title}]({link_url})")
                st.caption(f"📢 **{source}** • 🕒 {str(timestamp)[:16]}")
                st.markdown(summary or "")
            with col_metrics:
                st.write("##")
                st.metric(label="Region", value=country)
                st.caption(f"🏷️ Category: **{topic}**")

    def previous_page():
        st.session_state.feed_page = max(0, st.session_state.feed_page - 1)

    def next_page():
        st.session_state.feed_page = min(total_pages - 1, st.session_state.feed_page + 1)

    prev_col, center_col, next_col = st.columns([1, 2, 1])
    with prev_col:
        st.button("← Previous", disabled=page == 0, on_click=previous_page)
    with center_col:
        st.markdown(f"<div style='text-align:center'>Page {page + 1:,} of {total_pages:,}</div>", unsafe_allow_html=True)
    with next_col:
        st.button("Next →", disabled=page >= total_pages - 1, on_click=next_page)
