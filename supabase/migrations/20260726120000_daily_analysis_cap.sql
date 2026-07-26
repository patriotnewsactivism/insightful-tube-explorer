-- Enforce a rolling 24h cap on analyses per user, at the database level.
-- Root cause being fixed: any authenticated (free) account could queue
-- unlimited single or bulk (up to 50/request) analyses, each burning tokens
-- from the SHARED free-tier LLM keys (Cerebras/OpenRouter/Groq) that every
-- user's requests draw from -- one random signup could exhaust the whole
-- account's daily quota. The app already added a client-side check
-- (src/routes/dashboard.tsx); this trigger is the un-bypassable backstop
-- for anyone hitting the Supabase REST API directly.
--
-- Admins (by email) and any profile explicitly marked unlimited are exempt.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS is_unlimited BOOLEAN NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION public.enforce_daily_analysis_cap()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_email TEXT;
  v_unlimited BOOLEAN;
  v_count INT;
  v_cap CONSTANT INT := 3;
BEGIN
  SELECT email INTO v_email FROM auth.users WHERE id = NEW.user_id;
  IF v_email IN ('don@donmatthews.live', 'mreardon@wtpnews.org', 'patriotnewsactivism@gmail.com') THEN
    RETURN NEW;
  END IF;

  SELECT is_unlimited INTO v_unlimited FROM public.profiles WHERE id = NEW.user_id;
  IF v_unlimited THEN
    RETURN NEW;
  END IF;

  SELECT count(*) INTO v_count
  FROM public.analyses
  WHERE user_id = NEW.user_id
    AND created_at >= now() - interval '24 hours';

  IF v_count >= v_cap THEN
    RAISE EXCEPTION 'Daily limit reached (% analyses per 24h on the free tier). Upgrade for unlimited access.', v_cap
      USING ERRCODE = 'P0001';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_enforce_daily_analysis_cap ON public.analyses;
CREATE TRIGGER trg_enforce_daily_analysis_cap
  BEFORE INSERT ON public.analyses
  FOR EACH ROW EXECUTE FUNCTION public.enforce_daily_analysis_cap();
