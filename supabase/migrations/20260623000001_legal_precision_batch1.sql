-- ══════════════════════════════════════════════════════════════════════════════
-- Legal Precision Enhancement — Batch 1
-- 1. Creates missing core tables (facts, entities, entity_mentions, quotes,
--    timeline_events, contradictions) if they don't exist yet.
-- 2. Adds confidence scoring columns, verification status, and legal_entities table.
-- ══════════════════════════════════════════════════════════════════════════════


-- ══════════════════════════════════════════════════════════════════════════════
-- PART A: Create any missing core tables the worker depends on
-- ══════════════════════════════════════════════════════════════════════════════

-- ── facts ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.facts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  claim TEXT NOT NULL,
  category TEXT DEFAULT 'general',
  source_timestamp NUMERIC,
  citation TEXT DEFAULT '',
  confidence TEXT DEFAULT 'medium',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_analysis_idx ON public.facts(analysis_id);
CREATE INDEX IF NOT EXISTS facts_user_idx ON public.facts(user_id);
ALTER TABLE public.facts ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY "own facts select" ON public.facts FOR SELECT USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own facts insert" ON public.facts FOR INSERT WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own facts update" ON public.facts FOR UPDATE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own facts delete" ON public.facts FOR DELETE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── entities ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  entity_type TEXT DEFAULT 'person',
  aliases JSONB DEFAULT '[]',
  description TEXT,
  first_seen_analysis UUID REFERENCES public.analyses(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entities_user_idx ON public.entities(user_id);
ALTER TABLE public.entities ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY "own entities select" ON public.entities FOR SELECT USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own entities insert" ON public.entities FOR INSERT WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own entities update" ON public.entities FOR UPDATE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own entities delete" ON public.entities FOR DELETE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── entity_mentions ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.entity_mentions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id UUID NOT NULL REFERENCES public.entities(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  context TEXT,
  role TEXT,
  mention_count INT DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entity_mentions_entity_idx ON public.entity_mentions(entity_id);
CREATE INDEX IF NOT EXISTS entity_mentions_analysis_idx ON public.entity_mentions(analysis_id);
ALTER TABLE public.entity_mentions ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY "own entity_mentions select" ON public.entity_mentions
    FOR SELECT USING (
      EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own entity_mentions insert" ON public.entity_mentions
    FOR INSERT WITH CHECK (
      EXISTS (SELECT 1 FROM public.entities e WHERE e.id = entity_mentions.entity_id AND e.user_id = auth.uid())
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── quotes ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.quotes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  speaker TEXT DEFAULT 'Unknown',
  quote_text TEXT NOT NULL,
  context TEXT,
  timestamp_seconds NUMERIC,
  significance TEXT DEFAULT 'medium',
  tags JSONB DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quotes_analysis_idx ON public.quotes(analysis_id);
CREATE INDEX IF NOT EXISTS quotes_user_idx ON public.quotes(user_id);
ALTER TABLE public.quotes ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY "own quotes select" ON public.quotes FOR SELECT USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own quotes insert" ON public.quotes FOR INSERT WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own quotes update" ON public.quotes FOR UPDATE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own quotes delete" ON public.quotes FOR DELETE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── timeline_events ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.timeline_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  event_date TEXT DEFAULT 'unknown',
  event_date_precision TEXT DEFAULT 'estimated',
  event_description TEXT NOT NULL,
  source_context TEXT,
  category TEXT DEFAULT 'event',
  confidence TEXT DEFAULT 'medium',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS timeline_events_analysis_idx ON public.timeline_events(analysis_id);
CREATE INDEX IF NOT EXISTS timeline_events_user_idx ON public.timeline_events(user_id);
ALTER TABLE public.timeline_events ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY "own timeline_events select" ON public.timeline_events FOR SELECT USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own timeline_events insert" ON public.timeline_events FOR INSERT WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own timeline_events update" ON public.timeline_events FOR UPDATE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own timeline_events delete" ON public.timeline_events FOR DELETE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── contradictions ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.contradictions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  claim_a_analysis_id UUID REFERENCES public.analyses(id) ON DELETE CASCADE,
  claim_b_analysis_id UUID REFERENCES public.analyses(id) ON DELETE CASCADE,
  claim_a TEXT NOT NULL,
  claim_b TEXT NOT NULL,
  explanation TEXT,
  severity TEXT DEFAULT 'medium',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contradictions_user_idx ON public.contradictions(user_id);
ALTER TABLE public.contradictions ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY "own contradictions select" ON public.contradictions FOR SELECT USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own contradictions insert" ON public.contradictions FOR INSERT WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ══════════════════════════════════════════════════════════════════════════════
-- PART B: Legal Precision Enhancement columns
-- ══════════════════════════════════════════════════════════════════════════════

-- ── 1: Confidence scoring + verification on existing tables ──────────────────

-- facts: add numeric confidence score and verification tracking
ALTER TABLE public.facts ADD COLUMN IF NOT EXISTS confidence_score DECIMAL(3,2) DEFAULT 0.5;
ALTER TABLE public.facts ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'unverified';
ALTER TABLE public.facts ADD COLUMN IF NOT EXISTS verified_by UUID REFERENCES auth.users(id);
ALTER TABLE public.facts ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE public.facts ADD COLUMN IF NOT EXISTS verification_notes TEXT;

-- quotes
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS confidence_score DECIMAL(3,2) DEFAULT 0.5;
ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'unverified';

-- timeline_events
ALTER TABLE public.timeline_events ADD COLUMN IF NOT EXISTS confidence_score DECIMAL(3,2) DEFAULT 0.5;
ALTER TABLE public.timeline_events ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'unverified';

-- entities
ALTER TABLE public.entities ADD COLUMN IF NOT EXISTS confidence_score DECIMAL(3,2) DEFAULT 0.5;
ALTER TABLE public.entities ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'unverified';

-- ── 2: Legal entities table ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.legal_entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,  -- case_number, statute, regulation, court, judge, law_firm, attorney
  name TEXT NOT NULL,
  jurisdiction TEXT,
  citation_format TEXT,       -- bluebook, apa, chicago
  full_citation TEXT,
  description TEXT,
  first_seen_analysis UUID REFERENCES public.analyses(id) ON DELETE SET NULL,
  confidence_score DECIMAL(3,2) DEFAULT 0.5,
  verification_status TEXT DEFAULT 'unverified',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_legal_entities_user ON public.legal_entities(user_id);
CREATE INDEX IF NOT EXISTS idx_legal_entities_type ON public.legal_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_legal_entities_name ON public.legal_entities(user_id, name);

ALTER TABLE public.legal_entities ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  CREATE POLICY "own legal_entities select" ON public.legal_entities
    FOR SELECT USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own legal_entities insert" ON public.legal_entities
    FOR INSERT WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own legal_entities update" ON public.legal_entities
    FOR UPDATE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE POLICY "own legal_entities delete" ON public.legal_entities
    FOR DELETE USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TRIGGER legal_entities_updated_at BEFORE UPDATE ON public.legal_entities
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 3: Add provenance tracking to analyses ───────────────────────────────────

ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS ai_provenance JSONB DEFAULT '{}';
