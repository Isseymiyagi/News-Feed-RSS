"""Stage 5B: deterministic, versioned scoring of Stage 5A intelligence metadata.

Run `python importance_scoring.py` after applying 003_importance_scoring.sql.
No model calls are made; rerunning is safe. Only completed articles with metadata are scored.
"""
import argparse
import json
from news_vault_supabase_db import get_connection

VERSION = 1
IMPACT = {'LOW': 0.25, 'MEDIUM': 0.60, 'HIGH': 1.0}
SCOPE = {'LOCAL': 0.15, 'NATIONAL': 0.50, 'REGIONAL': 0.75, 'INTERNATIONAL': 1.0}
EVENT = {
    'MILITARY_CONFLICT': 1.0, 'NATURAL_DISASTER': 1.0, 'PUBLIC_HEALTH': 0.90,
    'MONETARY_POLICY': 0.90, 'ELECTION': 0.85, 'POLICY_CHANGE': 0.80,
    'SECURITY_INCIDENT': 0.80, 'ECONOMIC_INDICATOR': 0.75,
    'DIPLOMACY': 0.75, 'LEGAL_PROCEEDING': 0.65,
    'SCIENTIFIC_DISCOVERY': 0.65, 'MARKET_MOVEMENT': 0.60,
    'CORPORATE_EVENT': 0.45, 'TECHNOLOGY_RELEASE': 0.40,
    'CULTURAL_EVENT': 0.35, 'SPORTING_EVENT': 0.35, 'OTHER': 0.25,
}

def score_importance(metadata):
    """Return a 0-100 heuristic score, or None if required metadata is missing."""
    if not isinstance(metadata, dict):
        return None
    impact = IMPACT.get(metadata.get('impact_level'))
    scope = SCOPE.get(metadata.get('geographic_scope'))
    event = EVENT.get(metadata.get('event_type'))
    affected = metadata.get('countries_affected')
    evidence = metadata.get('impact_evidence')
    if impact is None or scope is None or event is None or not isinstance(affected, list) or not isinstance(evidence, str):
        return None
    # Counts are capped to avoid rewarding implausibly long country lists.
    country_signal = min(len(set(x for x in affected if isinstance(x, str))), 5) / 5
    # This measures whether the model supplied a nontrivial explanation, NOT factual verification.
    evidence_signal = 1.0 if len(evidence.strip()) >= 40 else (0.5 if evidence.strip() else 0.0)
    score = 100 * (0.35 * impact + 0.25 * event + 0.20 * scope + 0.10 * country_signal + 0.10 * evidence_signal)
    return max(0, min(100, round(score)))

def backfill(batch_size=250):
    total = 0
    with get_connection() as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, intelligence_metadata
                    FROM articles
                    WHERE processing_status = 'completed'
                      AND intelligence_metadata IS NOT NULL
                      AND (importance_version IS DISTINCT FROM %s OR importance_score IS NULL)
                    ORDER BY id LIMIT %s
                ''', (VERSION, batch_size))
                rows = cur.fetchall()
                if not rows:
                    break
                for article_id, metadata in rows:
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata)
                    score = score_importance(metadata)
                    if score is None:
                        # Mark as reviewed for this version to avoid infinite loops.
                        cur.execute('UPDATE articles SET importance_version = %s WHERE id = %s', (VERSION, article_id))
                    else:
                        cur.execute('UPDATE articles SET importance_score = %s, importance_version = %s WHERE id = %s', (score, VERSION, article_id))
                        total += 1
            conn.commit()
    print(f'Scored {total} completed articles (version {VERSION}).')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=250)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error('--batch-size must be positive')
    backfill(args.batch_size)
