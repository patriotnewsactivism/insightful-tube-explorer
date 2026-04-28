-- Add pasted_transcript column for client-side transcripts
ALTER TABLE public.analyses
ADD COLUMN IF NOT EXISTS pasted_transcript TEXT;
