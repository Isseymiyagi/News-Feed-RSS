-- Stage 5B: additive, deterministic importance scoring.
ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS importance_score SMALLINT;
ALTER TABLE public.articles ADD COLUMN IF NOT EXISTS importance_version INTEGER;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'articles_importance_score_range' AND conrelid = 'public.articles'::regclass) THEN
    ALTER TABLE public.articles ADD CONSTRAINT articles_importance_score_range CHECK (importance_score BETWEEN 0 AND 100) NOT VALID;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_articles_importance_feed ON public.articles (importance_score DESC, "timestamp" DESC, id DESC) WHERE processing_status = 'completed' AND importance_score IS NOT NULL;
