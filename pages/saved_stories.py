import streamlit as st
from reader_store import saved_stories, set_bookmark

st.set_page_config(layout='wide', page_title='Saved Stories')
st.title('🔖 Saved Stories')
st.caption('Bookmarks are private to this single-reader installation.')
try:
    stories = saved_stories()
    if not stories:
        st.info('No saved stories yet. Use Save on a news card.')
    for bookmark_id, article_id, event_id, title, source, link, created_at in stories:
        with st.container(border=True):
            st.markdown(f'**{title or "Untitled story"}**')
            st.caption(f'{"Event" if event_id is not None else "Article"} · {source or "Unknown publisher"} · Saved {created_at:%Y-%m-%d}')
            if link:
                st.link_button('Read article', link)
            if st.button('Remove bookmark', key=f'remove_{bookmark_id}'):
                set_bookmark(False, article_id=article_id, event_id=event_id)
                st.rerun()
except Exception as exc:
    st.error(f'Unable to load bookmarks: {exc}')
