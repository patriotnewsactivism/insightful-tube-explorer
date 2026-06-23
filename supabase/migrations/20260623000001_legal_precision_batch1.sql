-- ══════════════════════════════════════════════════════════════════════════════
-- Legal Precision Enhancement — Batch 1
-- Adds confidence scoring columns, verification status, and legal_entities table.
-- ══════════════════════════════════════════════════════════════════════════════

-- ── 1: Confidence scoring + verification on existing tables ──────────────────

-- facts: upgrade from TEXT confidence to numeric score, add verification
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

CREATE POLICY "own legal_entities select" ON public.legal_entities
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own legal_entities insert" ON public.legal_entities
  FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own legal_entities update" ON public.legal_entities
  FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "own legal_entities delete" ON public.legal_entities
  FOR DELETE USING (auth.uid() = user_id);

CREATE TRIGGER legal_entities_updated_at BEFORE UPDATE ON public.legal_entities
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ── 3: Add provenance tracking to analyses ───────────────────────────────────

-- Track which AI model produced each insight for provenance
ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS ai_provenance JSONB DEFAULT '{}';
