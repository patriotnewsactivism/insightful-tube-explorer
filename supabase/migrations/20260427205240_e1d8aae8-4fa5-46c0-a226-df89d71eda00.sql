-- Profiles
CREATE TABLE public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  display_name TEXT,
  avatar_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own profile select" ON public.profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY "own profile insert" ON public.profiles FOR INSERT WITH CHECK (auth.uid() = id);
CREATE POLICY "own profile update" ON public.profiles FOR UPDATE USING (auth.uid() = id);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  INSERT INTO public.profiles (id, display_name, avatar_url)
  VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data->>'display_name', NEW.raw_user_meta_data->>'full_name', split_part(NEW.email, '@', 1)),
    NEW.raw_user_meta_data->>'avatar_url'
  );
  RETURN NEW;
END;
$$;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- updated_at helper
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

CREATE TRIGGER profiles_updated_at BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Analyses
CREATE TYPE public.analysis_status AS ENUM ('pending', 'extracting', 'transcribing', 'processing', 'complete', 'failed');

CREATE TABLE public.analyses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  youtube_url TEXT NOT NULL,
  youtube_id TEXT,
  title TEXT,
  channel TEXT,
  thumbnail_url TEXT,
  description TEXT,
  published_at TIMESTAMPTZ,
  likely_production_date TEXT,
  production_date_reasoning TEXT,
  raw_transcript JSONB,
  polished_transcript TEXT,
  summary TEXT,
  sentiment JSONB,
  expanded_notes TEXT,
  status public.analysis_status NOT NULL DEFAULT 'pending',
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX analyses_user_idx ON public.analyses(user_id, created_at DESC);
ALTER TABLE public.analyses ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own analyses select" ON public.analyses FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own analyses insert" ON public.analyses FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own analyses update" ON public.analyses FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "own analyses delete" ON public.analyses FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER analyses_updated_at BEFORE UPDATE ON public.analyses
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Speakers (per user, learned over time)
CREATE TABLE public.speakers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  channel TEXT,
  azure_profile_id TEXT,
  voice_embedding JSONB,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX speakers_user_idx ON public.speakers(user_id);
ALTER TABLE public.speakers ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own speakers select" ON public.speakers FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own speakers insert" ON public.speakers FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own speakers update" ON public.speakers FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "own speakers delete" ON public.speakers FOR DELETE USING (auth.uid() = user_id);
CREATE TRIGGER speakers_updated_at BEFORE UPDATE ON public.speakers
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- Speaker utterances
CREATE TABLE public.speaker_utterances (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  speaker_id UUID REFERENCES public.speakers(id) ON DELETE SET NULL,
  analysis_id UUID NOT NULL REFERENCES public.analyses(id) ON DELETE CASCADE,
  diarization_label TEXT,
  start_seconds NUMERIC,
  end_seconds NUMERIC,
  text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX utterances_analysis_idx ON public.speaker_utterances(analysis_id);
CREATE INDEX utterances_speaker_idx ON public.speaker_utterances(speaker_id);
ALTER TABLE public.speaker_utterances ENABLE ROW LEVEL SECURITY;
CREATE POLICY "own utterances select" ON public.speaker_utterances FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "own utterances insert" ON public.speaker_utterances FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "own utterances update" ON public.speaker_utterances FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "own utterances delete" ON public.speaker_utterances FOR DELETE USING (auth.uid() = user_id);