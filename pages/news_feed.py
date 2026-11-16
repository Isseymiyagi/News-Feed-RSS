from datetime import datetime, timedelta
from news_vault_supabase_db import get_connection
import streamlit as st
from reader_store import get_state, set_bookmark, set_feedback, get_preferences
from personal_ranking import rank_stories

PAGE_SIZE = 25

st.set_page_config(layout="wide", page_title="Global Intel Stream")
st.title("🗲 Global Intelligence Streaming Timeline")
feed_mode = st.radio("Feed", ["Latest News", "Top Stories", "For You"], horizontal=True, key="feed_mode", on_change=lambda: st.session_state.update(feed_page=0))
st.caption("Browse chronological news, globally important events, or personalized recommendations.")


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

with get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT UPPER(TRIM(country_code))
            FROM articles
            WHERE country_code IS NOT NULL AND TRIM(country_code) <> ''
            ORDER BY 1
        """)
        available_countries = [row[0] for row in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT TRIM(source)
            FROM articles
            WHERE source IS NOT NULL AND TRIM(source) <> ''
            ORDER BY 1
        """)
        available_sources = sorted(
            (row[0] for row in cur.fetchall()), key=str.casefold
        )

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

# For You is a third feed mode on this page. Its ranking is deliberately
# separate from the chronological and global-importance queries below.
if feed_mode == "For You":
    st.caption("Personalized event ranking based on your interests and feedback; global Top Stories stays unchanged.")
    max_candidates = st.selectbox(
        "Stories to consider", [100, 250, 500, 1000], index=1,
        help="The personalization model reranks this importance-selected candidate pool.",
    )
    show_disliked = st.checkbox("Include stories marked Not Interested", value=False)
    try:
        prefs = get_preferences()
        if not any(prefs.values()):
            st.info("No preferences selected yet. Ranking will prioritize importance and freshness.")
            st.page_link("pages/reader_preferences.py", label="Set reader preferences", icon="⚙️")

        fy_conditions = ["a.processing_status = 'completed'", "a.importance_score IS NOT NULL"]
        fy_params = []
        country_aliases_fy = {"USA": "US", "GBR": "GB", "FRA": "FR", "ETH": "ET"}
        if selected_country != "All Countries":
            alias = country_aliases_fy.get(selected_country)
            if alias:
                fy_conditions.append("UPPER(TRIM(a.country_code)) IN (%s, %s)")
                fy_params.extend([selected_country, alias])
            else:
                fy_conditions.append("UPPER(TRIM(a.country_code)) = %s")
                fy_params.append(selected_country)
        if selected_topic != "All Topics":
            fy_conditions.append("a.topic_tag = %s")
            fy_params.append(selected_topic)
        if selected_source != "All Sources":
            fy_conditions.append("TRIM(a.source) = %s")
            fy_params.append(selected_source)
        if search_query.strip():
            fy_conditions.append("(a.title ILIKE %s OR a.summary ILIKE %s)")
            term = f"%{search_query.strip()}%"
            fy_params.extend([term, term])
        fy_periods = {"Past 24 Hours": 1, "Past 7 Days": 7, "Past 30 Days": 30, "Past 90 Days": 90}
        if selected_period in fy_periods:
            cutoff = (datetime.now() - timedelta(days=fy_periods[selected_period])).isoformat()
            fy_conditions.append('a."timestamp" >= %s')
            fy_params.append(cutoff)
        fy_where = " AND ".join(fy_conditions)

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"""
                WITH eligible AS (
                    SELECT a.id, a.title, a.source, a.summary, a."timestamp",
                           a.country_code, a.link_url, a.topic_tag, a.image_url,
                           a.importance_score, a.event_id, a.intelligence_metadata,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE('event:' || a.event_id::text,
                                                     'article:' || a.id::text)
                               ORDER BY a.importance_score DESC, a."timestamp" DESC, a.id DESC
                           ) AS event_rank
                    FROM public.articles a WHERE {fy_where}
                )
                SELECT id, title, source, summary, "timestamp", country_code,
                       link_url, topic_tag, image_url, importance_score, event_id,
                       intelligence_metadata
                FROM eligible WHERE event_rank = 1
                ORDER BY importance_score DESC, "timestamp" DESC, id DESC
                LIMIT %s
            """, [*fy_params, max_candidates])
            cols = [d.name for d in cur.description]
            stories = [dict(zip(cols, row)) for row in cur.fetchall()]
            cur.execute("SELECT article_id, signal FROM public.reader_feedback WHERE article_id IS NOT NULL")
            article_feedback = dict(cur.fetchall())
            cur.execute("SELECT event_id, signal FROM public.reader_feedback WHERE event_id IS NOT NULL")
            event_feedback = dict(cur.fetchall())

        ranked = rank_stories(stories, prefs, article_feedback, event_feedback)
        if not show_disliked:
            ranked = [story for story in ranked if not (
                event_feedback.get(story['event_id']) == 'not_interested'
                if story['event_id'] is not None
                else article_feedback.get(story['id']) == 'not_interested'
            )]
        total = len(ranked)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        st.session_state.feed_page = min(st.session_state.feed_page, total_pages - 1)
        page = st.session_state.feed_page
        st.caption(f"{total} personalized stories from {len(stories)} candidates")
        if not ranked:
            st.info("No matching stories. Try widening the date range or clearing filters.")
        for story in ranked[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            with st.container(border=True):
                image_col, content_col, score_col = st.columns([1, 3, 1])
                with image_col:
                    if story['image_url']:
                        st.image(story['image_url'], use_container_width=True)
                with content_col:
                    title = story['title'] or 'Untitled article'
                    url = story['link_url']
                    if url and str(url).startswith(('https://', 'http://')):
                        st.markdown(f"### [{title}]({url})")
                    else:
                        st.markdown(f"### {title}")
                    st.caption(f"{story['source'] or 'Unknown publisher'} · {str(story['timestamp'])[:16]}")
                    st.write(story['summary'] or '')
                    st.caption("Why this story: " + " · ".join(story['reasons']))
                with score_col:
                    st.metric("For You", f"{story['personal_score']:.1f}")
                    st.caption(f"Global importance: {story['importance_score']}/100")
                target = ({'event_id': story['event_id']} if story['event_id'] is not None
                          else {'article_id': story['id']})
                key = (f"event_{story['event_id']}" if story['event_id'] is not None
                       else f"article_{story['id']}")
                bookmarked, signal = get_state(**target)
                bcol, icol, ncol = st.columns(3)
                with bcol:
                    if st.button("🔖 Saved" if bookmarked else "🔖 Save", key=f"fy_bm_{key}"):
                        set_bookmark(not bookmarked, **target)
                        st.rerun()
                with icol:
                    if st.button("👍 Interesting ✓" if signal == 'interesting' else "👍 Interesting", key=f"fy_like_{key}"):
                        set_feedback(None if signal == 'interesting' else 'interesting', **target)
                        st.rerun()
                with ncol:
                    if st.button("👎 Not interested ✓" if signal == 'not_interested' else "👎 Not interested", key=f"fy_dislike_{key}"):
                        set_feedback(None if signal == 'not_interested' else 'not_interested', **target)
                        st.rerun()
        if ranked:
            prev_col, middle_col, next_col = st.columns([1, 2, 1])
            with prev_col:
                if st.button("← Previous", key="fy_prev", disabled=page == 0):
                    st.session_state.feed_page -= 1
                    st.rerun()
            with middle_col:
                st.markdown(f"<div style='text-align:center'>Page {page + 1} of {total_pages}</div>", unsafe_allow_html=True)
            with next_col:
                if st.button("Next →", key="fy_next", disabled=page >= total_pages - 1):
                    st.session_state.feed_page += 1
                    st.rerun()
    except Exception as exc:
        st.error(f"Unable to load For You feed: {exc}")
    st.stop()

# Build one parameterized WHERE clause, reused for the count and page queries.
conditions = ["1=1"]
if feed_mode == "Top Stories":
    conditions.append("importance_score IS NOT NULL")
params = []

# Accommodate the two-letter codes your original feed already supported.
country_aliases = {"USA": "US", "GBR": "GB", "FRA": "FR", "ETH": "ET"}
if selected_country != "All Countries":
    other_code = country_aliases.get(selected_country)
    if other_code:
        conditions.append("UPPER(TRIM(country_code)) IN (%s, %s)")
        params.extend([selected_country, other_code])
    else:
        conditions.append("UPPER(TRIM(country_code)) = %s")
        params.append(selected_country)

if selected_topic != "All Topics":
    conditions.append("topic_tag = %s")
    params.append(selected_topic)

if selected_source != "All Sources":
    conditions.append("TRIM(source) = %s")
    params.append(selected_source)

if search_query.strip():
    conditions.append("(title ILIKE %s OR summary ILIKE %s)")
    term = f"%{search_query.strip()}%"
    params.extend([term, term])

period_to_days = {
    "Past 24 Hours": 1,
    "Past 7 Days": 7,
    "Past 30 Days": 30,
    "Past 90 Days": 90,
}
if selected_period in period_to_days:
    # The migrated SQLite timestamps are ISO text without timezone information.
    # Compare against an ISO cutoff in the same text format, preserving that schema.
    cutoff = (datetime.now() - timedelta(days=period_to_days[selected_period])).isoformat()
    conditions.append('"timestamp" >= %s')
    params.append(cutoff)

where_clause = " AND ".join(conditions)

# Latest News remains article-based. Top Stories groups by event_id and
# treats unclustered articles as independent stories. Filters apply BEFORE
# ranking, so a matching publisher or country has a matching headline.
with get_connection() as conn:
    with conn.cursor() as cur:
        if feed_mode == "Latest News":
            cur.execute(
                f"SELECT COUNT(*) FROM articles WHERE processing_status = 'completed' AND ({where_clause})",
                params,
            )
            total_articles = cur.fetchone()[0]
            total_pages = max(1, (total_articles + PAGE_SIZE - 1) // PAGE_SIZE)
            st.session_state.feed_page = min(st.session_state.feed_page, total_pages - 1)
            page = st.session_state.feed_page
            cur.execute(
                f"""SELECT id, title, source, summary, "timestamp", country_code,
                           link_url, topic_tag, image_url, importance_score, event_id
                    FROM articles
                    WHERE processing_status = 'completed' AND ({where_clause})
                    ORDER BY "timestamp" DESC, id DESC
                    LIMIT %s OFFSET %s""",
                [*params, PAGE_SIZE, page * PAGE_SIZE],
            )
            articles = cur.fetchall()
        else:
            # DISTINCT ON chooses the highest-scored eligible article per event.
            # GROUP BY includes event id, or the article's own id if unclustered.
            grouped_cte = f"""
                WITH eligible AS (
                    SELECT a.*, CASE WHEN a.event_id IS NOT NULL
                        THEN 'event:' || a.event_id::text
                        ELSE 'article:' || a.id::text END AS story_key
                    FROM articles a
                    WHERE a.processing_status = 'completed' AND ({where_clause})
                ), ranked AS (
                    SELECT eligible.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY story_key
                            ORDER BY importance_score DESC, "timestamp" DESC, id DESC
                        ) AS story_rank
                    FROM eligible
                )
            """
            cur.execute(grouped_cte + "SELECT COUNT(*) FROM ranked WHERE story_rank = 1", params)
            total_articles = cur.fetchone()[0]
            total_pages = max(1, (total_articles + PAGE_SIZE - 1) // PAGE_SIZE)
            st.session_state.feed_page = min(st.session_state.feed_page, total_pages - 1)
            page = st.session_state.feed_page
            cur.execute(
                grouped_cte + """SELECT id, title, source, summary, "timestamp", country_code,
                                  link_url, topic_tag, image_url, importance_score, event_id
                           FROM ranked WHERE story_rank = 1
                           ORDER BY importance_score DESC, "timestamp" DESC, id DESC
                           LIMIT %s OFFSET %s""",
                [*params, PAGE_SIZE, page * PAGE_SIZE],
            )
            articles = cur.fetchall()

        coverage = {}
        event_ids = sorted({a[10] for a in articles if a[10] is not None})
        if feed_mode == "Top Stories" and event_ids:
            # Coverage intentionally includes ALL completed reporting for each
            # event, not just articles matching the active feed filters.
            cur.execute(
                """SELECT event_id, id, title, source, link_url, "timestamp"
                   FROM articles
                   WHERE event_id = ANY(%s) AND processing_status = 'completed'
                   ORDER BY "timestamp" DESC, id DESC""",
                (event_ids,),
            )
            for record in cur.fetchall():
                coverage.setdefault(record[0], []).append(record[1:])

if not articles:
    st.info("No matching briefings found for these filters.")
else:
    start = page * PAGE_SIZE + 1
    end = min((page + 1) * PAGE_SIZE, total_articles)
    st.caption(f"Showing {start:,}–{end:,} of {total_articles:,} {'stories' if feed_mode == 'Top Stories' else 'articles'}")

    for article_id, title, source, summary, timestamp, country, link_url, topic, image_url, importance_score, event_id in articles:
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
                if feed_mode == "Top Stories":
                    st.metric("Importance", f"{importance_score}/100")
            # A clustered Top Story stores feedback at event level; Latest News
            # stores it at article level, preserving separate reader intent.
            target = {'event_id': event_id} if feed_mode == "Top Stories" and event_id is not None else {'article_id': article_id}
            target_key = f"event_{event_id}" if 'event_id' in target else f"article_{article_id}"
            try:
                bookmarked, signal = get_state(**target)
                bcol, icol, ncol = st.columns(3)
                with bcol:
                    if st.button("🔖 Saved" if bookmarked else "🔖 Save", key=f"bm_{feed_mode}_{target_key}"):
                        set_bookmark(not bookmarked, **target)
                        st.rerun()
                with icol:
                    if st.button("👍 Interesting ✓" if signal == 'interesting' else "👍 Interesting", key=f"like_{feed_mode}_{target_key}"):
                        set_feedback(None if signal == 'interesting' else 'interesting', **target)
                        st.rerun()
                with ncol:
                    if st.button("👎 Not interested ✓" if signal == 'not_interested' else "👎 Not interested", key=f"dislike_{feed_mode}_{target_key}"):
                        set_feedback(None if signal == 'not_interested' else 'not_interested', **target)
                        st.rerun()
            except Exception as exc:
                st.error(f"Reader actions unavailable: {exc}")
            if feed_mode == "Top Stories" and event_id is not None:
                event_articles = coverage.get(event_id, [])
                distinct_publishers = len({(item[2] or "").strip().casefold() for item in event_articles if (item[2] or "").strip()})
                st.caption(f"Coverage: {len(event_articles)} articles · {distinct_publishers} publishers")
                if len(event_articles) > 1:
                    with st.expander(f"View coverage from {distinct_publishers} publishers"):
                        for cov_id, cov_title, cov_source, cov_url, cov_timestamp in event_articles:
                            if cov_id == article_id:
                                continue
                            st.markdown(f"- [{cov_title}]({cov_url}) — {cov_source} · {str(cov_timestamp)[:16]}")


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
