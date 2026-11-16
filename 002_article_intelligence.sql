-- Stage 5A: add AI-generated article intelligence without changing existing feed columns.
-- Run in Supabase SQL Editor before deploying the new ingestion script.
ALTER TABLE public.articles
ADD COLUMN IF NOT EXISTS intelligence_metadata JSONB;

CREATE INDEX IF NOT EXISTS idx_articles_intelligence_primary_topic
ON public.articles ((intelligence_metadata->>'primary_topic'))
WHERE intelligence_metadata IS NOT NULL;

-- Historical completed articles remain NULL until explicitly backfilled.
-- New ingestion writes validated metadata as part of the completed article update.
