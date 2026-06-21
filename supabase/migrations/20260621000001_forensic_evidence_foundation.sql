-- ══════════════════════════════════════════════════════════════════════════════
-- Phase 1: Forensic Evidence Foundation
-- Adds cryptographic hash chain, chain of custody logging, and raw transcript
-- preservation for court-admissible evidence handling.
-- ══════════════════════════════════════════════════════════════════════════════

-- ── 1A: Forensic columns on analyses ─────────────────────────────────────────

-- SHA-256 hash of the raw transcript at the exact moment of capture
ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS evidence_hash TEXT;

-- Precise timestamp of when the transcript was captured (distinct from created_at)
ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;

-- Metadata about the capture environment (source, worker version, method, etc.)
ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS capture_metadata JSONB;

-- ── 1C: Raw transcript preservation ──────────────────────────────────────────
-- Byte-exact original transcript text, separate from the JSONB `raw_transcript`
-- field which mixes metadata + text. This column is NEVER modified after capture.
ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS preserved_raw_transcript TEXT;

-- Independent hash of just the raw transcript text for integrity verification
ALTER TABLE public.analyses ADD COLUMN IF NOT EXISTS preserved_raw_hash TEXT;

-- ── 1B: Chain of Custody Log ─────────────────────────────────────────────────
-- Immutable, append-only audit trail of every action taken on an analysis.
-- No UPDATE or DELETE policies — once written, entries cannot be modified.

CREATE TABLE IF NOT EXISTS public.custody_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  user_id UUID REFERENCES auth.users(id),
  action TEXT NOT NULL,
  details JSONB DEFAULT '{}',
  actor TEXT NOT NULL DEFAULT 'system',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS custody_log_analysis_idx
  ON public.custody_log(analysis_id, created_at);

CREATE INDEX IF NOT EXISTS custody_log_action_idx
  ON public.custody_log(action);

ALTER TABLE public.custody_log ENABLE ROW LEVEL SECURITY;

-- Users can view custody logs for their own analyses
CREATE POLICY "own custody select" ON public.custody_log
  FOR SELECT USING (
    auth.uid() = user_id
    OR EXISTS (
      SELECT 1 FROM public.analyses a
      WHERE a.id = custody_log.analysis_id AND a.user_id = auth.uid()
    )
  );

-- Insert is allowed (worker uses service role, frontend uses auth)
CREATE POLICY "authenticated insert" ON public.custody_log
  FOR INSERT WITH CHECK (true);

-- NO UPDATE policy — append-only
-- NO DELETE policy — immutable audit trail
