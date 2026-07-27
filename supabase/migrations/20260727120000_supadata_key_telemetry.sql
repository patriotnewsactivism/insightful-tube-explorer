-- ══════════════════════════════════════════════════════════════════════════════
-- Supadata Key Telemetry — for APEX portfolio monitoring
--
-- Tracks the health of Supadata API keys so the APEX autonomous workforce can
-- monitor the key pool and alert when keys approach or hit their allotment.
-- Stores ONLY a non-reversible fingerprint (last 4 chars) — never the full key
-- value. The keys themselves live in the worker's env vars (SUPADATA_API_KEY /
-- SUPADATA_API_KEY_N), not in this table.
--
-- Access: service-role only. RLS is enabled with NO user policies, so only the
-- service role (worker + APEX, which both bypass RLS) can read/write.
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.supadata_key_status (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key_fingerprint TEXT NOT NULL UNIQUE,        -- last 4 chars only; never the full key
  label TEXT,                                   -- optional human label (e.g. "free-acct-1")
  status TEXT NOT NULL DEFAULT 'active',       -- active | exhausted | recovering
  exhausted_until TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  last_exhausted_at TIMESTAMPTZ,
  exhausted_count INTEGER NOT NULL DEFAULT 0,
  last_http_status INTEGER,
  last_error_message TEXT,
  remaining_credits JSONB,                     -- snapshot from GET /me (plan, credits, etc.)
  last_checked_at TIMESTAMPTZ,                  -- when /me was last polled
  worker_version TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_supadata_key_status_fp
  ON public.supadata_key_status(key_fingerprint);
CREATE INDEX IF NOT EXISTS idx_supadata_key_status_status
  ON public.supadata_key_status(status);

ALTER TABLE public.supadata_key_status ENABLE ROW LEVEL SECURITY;

-- No SELECT/INSERT/UPDATE/DELETE policies for authenticated (anon) users.
-- Only the service role can access this operational telemetry table, since the
-- service role bypasses RLS. This keeps key health data private to the worker
-- and APEX (both use the service role key).

CREATE TRIGGER supadata_key_status_updated_at BEFORE UPDATE ON public.supadata_key_status
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();
