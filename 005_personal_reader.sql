-- Stage 5E part 1: single-reader private preferences and signals.
-- Requires public.articles and public.news_events from prior stages.
CREATE TABLE IF NOT EXISTS public.reader_bookmarks (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id BIGINT REFERENCES public.articles(id) ON DELETE CASCADE,
  event_id BIGINT REFERENCES public.news_events(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT bookmark_exactly_one_target CHECK ((article_id IS NOT NULL) <> (event_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reader_bookmark_article ON public.reader_bookmarks(article_id) WHERE article_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_reader_bookmark_event ON public.reader_bookmarks(event_id) WHERE event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.reader_feedback (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  article_id BIGINT REFERENCES public.articles(id) ON DELETE CASCADE,
  event_id BIGINT REFERENCES public.news_events(id) ON DELETE CASCADE,
  signal TEXT NOT NULL CHECK (signal IN ('interesting', 'not_interested')),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT feedback_exactly_one_target CHECK ((article_id IS NOT NULL) <> (event_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reader_feedback_article ON public.reader_feedback(article_id) WHERE article_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_reader_feedback_event ON public.reader_feedback(event_id) WHERE event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.reader_preferences (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  topics TEXT[] NOT NULL DEFAULT '{}',
  countries TEXT[] NOT NULL DEFAULT '{}',
  publishers TEXT[] NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO public.reader_preferences(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;

ALTER TABLE public.reader_bookmarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reader_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reader_preferences ENABLE ROW LEVEL SECURITY;
-- No anon/authenticated policies: access via trusted private Python DB connection only.
