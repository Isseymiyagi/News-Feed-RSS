"""Stage 5C: conservative, deterministic event clustering without new AI calls.

Run after ingestion and importance_scoring.py. Existing feed order is unchanged.
Uses AI-generated metadata and article titles, not RSS publisher topic tags.
"""
import argparse
import re
from datetime import datetime, timedelta
from news_vault_supabase_db import get_connection

STOP = set('the a an and or of to in on for with from by at as is are was were after before over amid says say new latest update updates report reports how why what about into its their his her this that will could may more'.split())
WINDOW_HOURS = 72


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", (title or '').lower()) if len(w) >= 3 and w not in STOP}


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def match_score(a, b):
    """Return similarity 0..1 or 0 if hard event-identity gates fail."""
    ma, mb = a['meta'], b['meta']
    if ma.get('event_type') != mb.get('event_type'):
        return 0.0
    ta, tb = parse_time(a['timestamp']), parse_time(b['timestamp'])
    if not ta or not tb or abs((ta-tb).total_seconds()) > WINDOW_HOURS * 3600:
        return 0.0
    wa, wb = tokens(a['title']), tokens(b['title'])
    if min(len(wa), len(wb)) < 4:
        return 0.0
    overlap = len(wa & wb) / len(wa | wb)
    if overlap < 0.52 or len(wa & wb) < 4:
        return 0.0
    ea = {x.casefold().strip() for x in ma.get('entities', []) if isinstance(x, str)}
    eb = {x.casefold().strip() for x in mb.get('entities', []) if isinstance(x, str)}
    ca = set(ma.get('countries_affected', []))
    cb = set(mb.get('countries_affected', []))
    # Shared entity or affected country is required to avoid unrelated same-topic stories.
    if not (ea & eb or ca & cb):
        return 0.0
    # Entity agreement helps distinguish distinct incidents in the same country.
    if ea and eb and not (ea & eb) and overlap < 0.78:
        return 0.0
    return overlap + (0.08 if ea & eb else 0.0)


def cluster(batch_size=250, dry_run=False):
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    assigned = created = 0
    with get_connection() as conn:
        # Serialize clustering jobs; do not interfere with ingestion's different lock.
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)', (68423018,))
            if not cur.fetchone()[0]:
                raise RuntimeError('Another clustering run is already active')
        try:
            with conn.cursor() as cur:
                cur.execute('''SELECT a.id, a.title, a."timestamp", a.intelligence_metadata,
                                      a.event_id, e.event_type
                               FROM articles a LEFT JOIN news_events e ON e.id = a.event_id
                               WHERE a.processing_status = 'completed'
                                 AND a.intelligence_metadata IS NOT NULL
                               ORDER BY a."timestamp" ASC, a.id ASC''')
                rows = cur.fetchall()
            articles = [dict(id=r[0], title=r[1], timestamp=r[2], meta=r[3], event_id=r[4]) for r in rows]
            # Compare to representative articles only: avoids transitive over-merging.
            representatives = list({a['event_id']: a for a in reversed(articles) if a['event_id'] is not None}.values())
            pending = [a for a in articles if a['event_id'] is None]
            if dry_run:
                print(f'Dry run: {len(pending)} unclustered articles; {len(representatives)} previously assigned.')
                return
            for a in pending:
                candidates = [b for b in representatives if b['event_id'] is not None and b['meta'].get('event_type') == a['meta'].get('event_type')]
                best = max(candidates, key=lambda b: match_score(a, b), default=None)
                if best is not None and match_score(a, best) >= 0.60:
                    event_id = best['event_id']
                else:
                    with conn.cursor() as cur:
                        cur.execute('''INSERT INTO news_events (event_type, representative_article_id)
                                       VALUES (%s, %s) RETURNING id''', (a['meta']['event_type'], a['id']))
                        event_id = cur.fetchone()[0]
                    created += 1
                    representatives.append(a)
                with conn.cursor() as cur:
                    cur.execute('UPDATE articles SET event_id = %s WHERE id = %s AND event_id IS NULL', (event_id, a['id']))
                    cur.execute('UPDATE news_events SET last_updated_at = now() WHERE id = %s', (event_id,))
                a['event_id'] = event_id
                assigned += 1
                if assigned % batch_size == 0:
                    conn.commit()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', (68423018,))
            conn.commit()
    print(f'Assigned {assigned} articles to events; created {created} new events.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--batch-size', type=int, default=250)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    cluster(args.batch_size, args.dry_run)
