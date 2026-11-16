"""Stage 5E private single-reader storage. Never import in a public/untrusted client."""
from news_vault_supabase_db import get_connection


def _target(article_id=None, event_id=None):
    if (article_id is None) == (event_id is None):
        raise ValueError('Provide exactly one article_id or event_id')
    return ('event_id', int(event_id)) if event_id is not None else ('article_id', int(article_id))


def get_state(article_id=None, event_id=None):
    field, value = _target(article_id, event_id)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT EXISTS(SELECT 1 FROM public.reader_bookmarks WHERE {field} = %s)', (value,))
        bookmarked = cur.fetchone()[0]
        cur.execute(f'SELECT signal FROM public.reader_feedback WHERE {field} = %s', (value,))
        row = cur.fetchone()
    return bookmarked, row[0] if row else None


def set_bookmark(enabled, article_id=None, event_id=None):
    field, value = _target(article_id, event_id)
    with get_connection() as conn, conn.cursor() as cur:
        if enabled:
            cur.execute(f'INSERT INTO public.reader_bookmarks({field}) VALUES (%s) ON CONFLICT DO NOTHING', (value,))
        else:
            cur.execute(f'DELETE FROM public.reader_bookmarks WHERE {field} = %s', (value,))


def set_feedback(signal, article_id=None, event_id=None):
    if signal not in ('interesting', 'not_interested', None):
        raise ValueError('Invalid feedback')
    field, value = _target(article_id, event_id)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f'DELETE FROM public.reader_feedback WHERE {field} = %s', (value,))
        if signal is not None:
            cur.execute(f'INSERT INTO public.reader_feedback({field}, signal) VALUES (%s, %s)', (value, signal))


def get_preferences():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT topics, countries, publishers FROM public.reader_preferences WHERE singleton = TRUE')
        row = cur.fetchone()
    return {'topics': row[0], 'countries': row[1], 'publishers': row[2]} if row else {'topics': [], 'countries': [], 'publishers': []}


def save_preferences(topics, countries, publishers):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO public.reader_preferences(singleton, topics, countries, publishers)
                       VALUES(TRUE, %s, %s, %s)
                       ON CONFLICT (singleton) DO UPDATE SET
                       topics = EXCLUDED.topics, countries = EXCLUDED.countries,
                       publishers = EXCLUDED.publishers, updated_at = now()''',
                    (list(topics), list(countries), list(publishers)))


def saved_stories():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute('''SELECT b.id, b.article_id, b.event_id,
                         COALESCE(a.title, e_article.title),
                         COALESCE(a.source, e_article.source),
                         COALESCE(a.link_url, e_article.link_url),
                         b.created_at
                       FROM public.reader_bookmarks b
                       LEFT JOIN public.articles a ON a.id = b.article_id
                       LEFT JOIN public.news_events e ON e.id = b.event_id
                       LEFT JOIN public.articles e_article ON e_article.id = e.representative_article_id
                       ORDER BY b.created_at DESC, b.id DESC''')
        return cur.fetchall()
