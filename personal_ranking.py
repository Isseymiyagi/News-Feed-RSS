"""Transparent, deterministic personal ranking. No AI calls or database writes."""
from datetime import datetime, timezone


def _items(value):
    return value if isinstance(value, list) else []


def _norm(value):
    return str(value or '').strip().casefold()


def _age_days(timestamp, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            # Legacy timestamps are timezone-naive; assume UTC consistently.
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 86400)
    except (TypeError, ValueError):
        return 365.0


def score_story(story, preferences, article_feedback=None, event_feedback=None, now=None):
    """Returns (score, explanations). Importance stays the dominant signal.

    Story keys: id, event_id, importance_score, timestamp, source, country_code,
    intelligence_metadata. Feedback dicts map integer IDs to signal strings.
    """
    article_feedback = article_feedback or {}
    event_feedback = event_feedback or {}
    metadata = story.get('intelligence_metadata') or {}
    if not isinstance(metadata, dict):
        metadata = {}
    topic_values = [metadata.get('primary_topic'), *_items(metadata.get('secondary_topics'))]
    topics = {_norm(v) for v in topic_values if v}
    countries = {_norm(story.get('country_code'))}
    countries.update(_norm(v) for v in _items(metadata.get('countries_affected')) if v)
    countries.update(_norm(v) for v in _items(metadata.get('countries_mentioned')) if v)
    pref_topics = {_norm(v) for v in preferences.get('topics', [])}
    pref_countries = {_norm(v) for v in preferences.get('countries', [])}
    pref_publishers = {_norm(v) for v in preferences.get('publishers', [])}
    importance = max(0, min(100, int(story.get('importance_score') or 0)))
    score = importance * 0.65
    reasons = [f'Importance {importance}/100']
    if topics & pref_topics:
        score += 18
        reasons.append('Preferred topic')
    if countries & pref_countries:
        score += 12
        reasons.append('Preferred country')
    if _norm(story.get('source')) in pref_publishers:
        score += 8
        reasons.append('Preferred publisher')
    # Gentle freshness boost, unlike the global Top Stories feed.
    age = _age_days(story.get('timestamp'), now)
    freshness = max(0.0, 8 * (1 - age / 14))
    score += freshness
    if freshness >= 2:
        reasons.append('Recent coverage')
    signal = event_feedback.get(story.get('event_id')) if story.get('event_id') is not None else None
    if signal is None:
        signal = article_feedback.get(story.get('id'))
    if signal == 'interesting':
        score += 12
        reasons.append('Marked interesting')
    elif signal == 'not_interested':
        score -= 45
        reasons.append('Marked not interested')
    return round(score, 2), reasons


def rank_stories(stories, preferences, article_feedback=None, event_feedback=None, now=None):
    scored = []
    for story in stories:
        value, reasons = score_story(story, preferences, article_feedback, event_feedback, now)
        scored.append({**story, 'personal_score': value, 'reasons': reasons})
    return sorted(scored, key=lambda s: (s['personal_score'], str(s.get('timestamp') or ''), s['id']), reverse=True)
