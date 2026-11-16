import streamlit as st
from news_vault_supabase_db import get_connection
from reader_store import get_preferences, save_preferences

st.set_page_config(layout='wide', page_title='Reader Preferences')
st.title('⚙️ Reader Preferences')
st.caption('These preferences are saved for the future For You feed. They do not change Latest News or Top Stories.')
try:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT primary_topic FROM (SELECT intelligence_metadata->>'primary_topic' AS primary_topic FROM public.articles) x WHERE primary_topic IS NOT NULL AND primary_topic <> '' ORDER BY 1")
        topics = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT UPPER(TRIM(country_code)) FROM public.articles WHERE country_code IS NOT NULL AND TRIM(country_code) <> '' ORDER BY 1")
        countries = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT TRIM(source) FROM public.articles WHERE source IS NOT NULL AND TRIM(source) <> '' ORDER BY 1")
        publishers = [r[0] for r in cur.fetchall()]
    saved = get_preferences()
    # Preserve saved values even if a publisher or topic is temporarily absent.
    topics = sorted(set(topics) | set(saved['topics']))
    countries = sorted(set(countries) | set(saved['countries']))
    publishers = sorted(set(publishers) | set(saved['publishers']), key=str.casefold)
    with st.form('reader_preferences'):
        chosen_topics = st.multiselect('Preferred AI topics', topics, default=saved['topics'])
        chosen_countries = st.multiselect('Preferred countries', countries, default=saved['countries'])
        chosen_publishers = st.multiselect('Preferred publishers', publishers, default=saved['publishers'])
        if st.form_submit_button('Save preferences'):
            save_preferences(chosen_topics, chosen_countries, chosen_publishers)
            st.success('Preferences saved.')
except Exception as exc:
    st.error(f'Unable to load or save preferences: {exc}')
