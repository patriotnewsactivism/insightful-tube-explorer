-- Reconstruct insight tables that were historically created outside versioned migrations.
-- Derived from the current worker insert shapes and frontend queries.

CREATE TABLE IF NOT EXISTS public.facts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  claim TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  source_timestamp NUMERIC,
  citation TEXT,
  confidence TEXT NOT NULL DEFAULT 'medium',
  verified BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_user_idx ON public.facts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS facts_analysis_idx ON public.facts(analysis_id);
ALTER TABLE public.facts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own facts select" ON public.facts FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own facts insert" ON public.facts FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own facts update" ON public.facts FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own facts delete" ON public.facts FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER facts_updated_at BEFORE UPDATE ON public.facts
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT 'person',
  aliases TEXT[] NOT NULL DEFAULT '{}',
  description TEXT,
  first_seen_analysis UUID REFERENCES public.analyses(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entities_user_name_idx ON public.entities(user_id, name);
CREATE INDEX IF NOT EXISTS entities_type_idx ON public.entities(user_id, entity_type);
ALTER TABLE public.entities ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own entities select" ON public.entities FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own entities insert" ON public.entities FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own entities update" ON public.entities FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own entities delete" ON public.entities FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER entities_updated_at BEFORE UPDATE ON public.entities
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.entity_mentions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  context TEXT,
  role TEXT,
  mention_count INTEGER NOT NULL DEFAULT 1 CHECK (mention_count >= 1),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entity_mentions_entity_idx ON public.entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS entity_mentions_analysis_idx ON public.entity_mentions(analysis_id);
ALTER TABLE public.entity_mentions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own entity_mentions select" ON public.entity_mentions FOR SELECT USING (
  EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
);
CREATE POLICY "own entity_mentions insert" ON public.entity_mentions FOR INSERT WITH CHECK (
  EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
  AND EXISTS (SELECT 1 FROM public.analyses a WHERE a.id = entity_mentions.analysis_id AND a.user_id = auth.uid())
);
CREATE POLICY "own entity_mentions update" ON public.entity_mentions FOR UPDATE USING (
  EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
) WITH CHECK (
  EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
  AND EXISTS (SELECT 1 FROM public.analyses a WHERE a.id = entity_mentions.analysis_id AND a.user_id = auth.uid())
);
CREATE POLICY "own entity_mentions delete" ON public.entity_mentions FOR DELETE USING (
  EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
);

CREATE TABLE IF NOT EXISTS public.quotes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  speaker TEXT NOT NULL DEFAULT 'Unknown',
  quote_text TEXT NOT NULL,
  context TEXT,
  timestamp_seconds NUMERIC,
  significance TEXT NOT NULL DEFAULT 'medium',
  tags TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quotes_user_idx ON public.quotes(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS quotes_analysis_idx ON public.quotes(analysis_id);
ALTER TABLE public.quotes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own quotes select" ON public.quotes FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own quotes insert" ON public.quotes FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own quotes update" ON public.quotes FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own quotes delete" ON public.quotes FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER quotes_updated_at BEFORE UPDATE ON public.quotes
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.timeline_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  event_date TEXT NOT NULL,
  event_date_precision TEXT NOT NULL DEFAULT 'estimated',
  event_description TEXT NOT NULL,
  source_context TEXT,
  category TEXT NOT NULL DEFAULT 'other',
  confidence TEXT NOT NULL DEFAULT 'medium',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS timeline_events_user_date_idx ON public.timeline_events(user_id, event_date);
CREATE INDEX IF NOT EXISTS timeline_events_analysis_idx ON public.timeline_events(analysis_id);
ALTER TABLE public.timeline_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own timeline_events select" ON public.timeline_events FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own timeline_events insert" ON public.timeline_events FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own timeline_events update" ON public.timeline_events FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own timeline_events delete" ON public.timeline_events FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER timeline_events_updated_at BEFORE UPDATE ON public.timeline_events
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.contradictions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  claim_a_analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  claim_b_analysis_id UUID REFERENCES public.analyses(id) ON DELETE CASCADE,
  claim_a TEXT NOT NULL,
  claim_b TEXT NOT NULL,
  explanation TEXT,
  severity TEXT NOT NULL DEFAULT 'medium',
  resolved BOOLEAN NOT NULL DEFAULT false,
  resolution_notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contradictions_user_idx ON public.contradictions(user_id, created_at DESC);
ALTER TABLE public.contradictions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own contradictions select" ON public.contradictions FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own contradictions insert" ON public.contradictions FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own contradictions update" ON public.contradictions FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own contradictions delete" ON public.contradictions FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER contradictions_updated_at BEFORE UPDATE ON public.contradictions
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.chapters (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  chapter_number INTEGER,
  description TEXT,
  color TEXT NOT NULL DEFAULT '#ef4444',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chapters_user_number_idx ON public.chapters(user_id, chapter_number);
ALTER TABLE public.chapters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own chapters select" ON public.chapters FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own chapters insert" ON public.chapters FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own chapters update" ON public.chapters FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own chapters delete" ON public.chapters FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER chapters_updated_at BEFORE UPDATE ON public.chapters
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.chapter_tags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  chapter_id UUID NOT NULL REFERENCES public.chapters(id) ON DELETE CASCADE,
  analysis_id UUID REFERENCES public.analyses(id) ON DELETE CASCADE,
  fact_id UUID REFERENCES public.facts(id) ON DELETE CASCADE,
  quote_id UUID REFERENCES public.quotes(id) ON DELETE CASCADE,
  entity_id UUID REFERENCES public.entities(id) ON DELETE CASCADE,
  timeline_event_id UUID REFERENCES public.timeline_events(id) ON DELETE CASCADE,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chapter_tags_exactly_one_target CHECK (
    num_nonnulls(analysis_id, fact_id, quote_id, entity_id, timeline_event_id) = 1
  )
);
CREATE INDEX IF NOT EXISTS chapter_tags_chapter_idx ON public.chapter_tags(chapter_id);
ALTER TABLE public.chapter_tags ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own chapter_tags select" ON public.chapter_tags FOR SELECT USING (
  EXISTS (SELECT 1 FROM public.chapters c WHERE c.id = chapter_tags.chapter_id AND c.user_id = auth.uid())
);
CREATE POLICY "own chapter_tags insert" ON public.chapter_tags FOR INSERT WITH CHECK (
  EXISTS (SELECT 1 FROM public.chapters c WHERE c.id = chapter_tags.chapter_id AND c.user_id = auth.uid())
);
CREATE POLICY "own chapter_tags update" ON public.chapter_tags FOR UPDATE USING (
  EXISTS (SELECT 1 FROM public.chapters c WHERE c.id = chapter_tags.chapter_id AND c.user_id = auth.uid())
) WITH CHECK (
  EXISTS (SELECT 1 FROM public.chapters c WHERE c.id = chapter_tags.chapter_id AND c.user_id = auth.uid())
);
CREATE POLICY "own chapter_tags delete" ON public.chapter_tags FOR DELETE USING (
  EXISTS (SELECT 1 FROM public.chapters c WHERE c.id = chapter_tags.chapter_id AND c.user_id = auth.uid())
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime')
     AND NOT EXISTS (
       SELECT 1 FROM pg_publication_tables
       WHERE pubname = 'supabase_realtime' AND schemaname = 'public' AND tablename = 'analyses'
     ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.analyses;
  END IF;
END $$;
