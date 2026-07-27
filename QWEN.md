# QWEN.md

Guidance for AI assistants working in the TubeScribe codebase.

## Project Overview

**TubeScribe** (package name `lumen`; repo alias `insightful-tube-explorer`) is a YouTube video analysis and forensic transcription tool built for journalists, authors, and researchers documenting public records, court proceedings, and civic matters. A user pastes a YouTube URL; the system transcribes the audio, runs parallel AI insights (summary, sentiment, notes, speaker identification, production-date estimation), and stores everything in Supabase. A cryptographic evidence chain preserves the byte-exact raw transcript for court-admissible export.

### Architecture (two deployables)

1. **Frontend** — React 19 SPA on Vercel. Vite + TanStack Router (file-based, auto code-split) + Tailwind CSS v4 + shadcn/ui (new-york style). Talks directly to Supabase (auth + DB + realtime). Inserts an `analyses` row; subscribes to realtime `UPDATE`s for live progress.
2. **Worker** — a single Python file (`worker/main.py`, ~2,600 lines) running on Railway via nixpacks (python3.11 + ffmpeg + yt-dlp + nodejs_22). Triggered by a Supabase database webhook on `analyses` INSERT. Downloads/transcribes audio, runs AI calls in parallel, writes results back to Supabase via the REST API using the service-role key.

**Data flow:** Frontend inserts row → Supabase webhook POSTs to worker → worker runs the pipeline (4 transcription tracks, then 6 parallel AI calls) → worker patches the row → Supabase realtime pushes the update to the frontend.

### Transcription pipeline (fallback chain, in `run_pipeline`)

1. **Deepgram Nova-2** (audio download via yt-dlp + ffmpeg) — most accurate, primary when `DEEPGRAM_API_KEY` is set.
2. **Supadata API** — caption-based, bypasses YouTube bot detection via curl.
3. **YouTube captions** — direct `video.google.com/timedtext` scrape (server-side).
4. **Pasted transcript** — user-supplied text; short-circuits all other tracks.

### AI providers (OpenAI-compatible, all free-tier)

Preference order in code: **Cerebras** (`llama-3.3-70b`, 1M tokens/day free) → **OpenRouter** (`openai/gpt-oss-120b:free`) → **Groq** (`llama-3.3-70b-versatile`). All called through `call_openai()`. Six prompts run in parallel (max 2 concurrent): summary, sentiment (JSON), expanded notes, production-date estimate (JSON), polished speaker transcript, speaker identification (JSON).

### Forensic & legal-precision systems

- **Forensic evidence (v14):** `preserve_raw_transcript()` freezes the byte-exact transcript and computes two SHA-256 hashes (evidence hash = `video_id|captured_at|raw_text`; raw hash = transcript text only). `custody_log` table is append-only (no UPDATE/DELETE RLS policies). `verify_evidence_hash()` recomputes and compares. `/export-forensic` generates a court-ready certificate.
- **Legal precision (Batch 1):** `sanitize_for_legal()` strips all markdown formatting from every text output for legal-document compatibility. `compute_confidence_score()` rates claims 0.0–1.0 based on source reliability, hedging language, and corroboration. `extract_legal_entities_regex()` finds case numbers, statutes, reporter citations, constitutional amendments, and courts via regex; results go in `legal_entities`.

## Repository Layout

```
C:\Tubescribe\
├── src/                       # Frontend (React/TS)
│   ├── components/            # SiteHeader, EvidenceBadge, CustodyTimeline, ui/ (shadcn)
│   ├── hooks/                 # useAuth.tsx, use-mobile.tsx
│   ├── integrations/supabase/ # client.ts (lazy singleton), types.ts (generated), auth-middleware.ts
│   ├── lib/                   # utils.ts (cn), youtube.ts (ID extraction + client transcript fetch)
│   ├── routes/                # TanStack file-based routes (index, auth, dashboard, analysis.$id,
│   │                          #   chapters, contradictions, knowledge, people, quotes, timeline)
│   ├── main.tsx, router.tsx   # Entry + router (auto-generated routeTree.gen.ts)
│   └── styles.css             # Tailwind v4 + theme tokens
├── worker/                    # Python backend (single main.py)
│   ├── main.py                # The entire worker: HTTP server, pipeline, AI, forensic, legal
│   ├── railway.json, nixpacks.toml, Procfile  # Railway deploy config
├── supabase/
│   ├── migrations/            # 5 SQL migrations (base schema, pasted transcript, forensic, legal)
│   ├── functions/process-analysis/  # Edge function (legacy)
│   └── config.toml
├── package.json, vite.config.ts, tsconfig.json, eslint.config.js
├── components.json            # shadcn/ui config (new-york style, slate base, lucide icons)
├── SETUP_AZURE.md             # ⚠ OUTDATED — describes old Azure pipeline; worker now uses free providers
├── vercel.json                # Frontend SPA rewrites
└── wrangler.jsonc             # Cloudflare config (vestigial)
```

## Building & Running

### Frontend
```bash
bun install          # or npm install — bun.lockb present, but package-lock.json also committed
bun run dev          # vite dev (hot reload)
bun run build        # vite build → dist/
bun run preview      # preview production build
bun run lint         # eslint .
bun run format       # prettier --write .
```

Requires env (`.env`, Vite-prefixed):
- `VITE_SUPABASE_URL`
- `VITE_SUPABASE_KEY`
- `VITE_WORKER_URL` (optional; defaults to the Railway production URL)

### Worker (local)
```bash
cd worker
python main.py       # listens on PORT (default 8080)
```
Requires env: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, plus any of `DEEPGRAM_API_KEY`, `SUPADATA_API_KEY`, `CEREBRAS_API_KEY`/`OPENROUTER_API_KEY`/`GROQ_API_KEY`. Needs `ffmpeg` and `yt-dlp` on PATH.

### Database
Migrations live in `supabase/migrations/`. Apply via `supabase db push` or the Supabase dashboard. A Supabase database webhook on `analyses` INSERT must point to the Railway worker URL (see `SETUP_AZURE.md` Step 3 — the webhook part is still accurate even though the Azure steps are stale).

## Development Conventions

- **TypeScript:** `strict: true`, bundler module resolution, path alias `@/*` → `src/*`. `noUnusedLocals`/`noUnusedParameters` are OFF. `routeTree.gen.ts` is auto-generated by the TanStack router plugin — do not edit by hand.
- **Formatting:** Prettier — 100 col, semicolons, double quotes, trailing commas all. ESLint extends `typescript-eslint` recommended + `react-hooks` + `react-refresh` + prettier integration. `@typescript-eslint/no-unused-vars` is OFF.
- **shadcn/ui:** new-york variant, slate base color, lucide icons, CSS variables (not Tailwind config). Components live in `src/components/ui/`. When adding components, follow `components.json` aliases (`@/components`, `@/lib/utils`, etc.).
- **Supabase client:** `src/integrations/supabase/client.ts` is marked auto-generated and uses a lazy Proxy singleton — import `{ supabase }` from `@/integrations/supabase/client`. Types in `types.ts` are generated from the DB schema; regenerate after schema changes.
- **Auth:** `AuthProvider` wraps the root route; `useAuth()` hook gives `{ user, session, loading, signOut }`. Routes that require auth redirect to `/auth` when `!user`.
- **Python worker:** single-file, stdlib only (no requirements.txt / pip dependencies beyond system packages from nixpacks). Uses `urllib` for HTTP, `http.server` for the server, `subprocess` + curl for Supadata/yt-dlp. New features are functions added to `main.py`; the `Handler.do_POST` switch dispatches by URL path.
- **Worker endpoints** (all POST JSON): `/` (webhook trigger), `/search`, `/reprocess-insights`, `/export`, `/export-forensic`, `/verify-hash`, `/custody-log`.
- **Analysis status enum:** `pending → extracting → transcribing → processing → complete | failed`.
- **Forensic invariant:** `custody_log` is append-only by design (no UPDATE/DELETE RLS). `preserved_raw_transcript` is written once and never modified. Any code that would mutate these is a bug.
- **Legal-text rule:** all AI text outputs pass through `sanitize_for_legal()` before storage — never store markdown-formatted text in `summary`/`expanded_notes`/`polished_transcript`.

## Database Tables (public schema)

| Table | Purpose |
|---|---|
| `profiles` | Auto-created on signup via trigger; display name + avatar. |
| `analyses` | Core record: URL, transcript (raw JSONB + polished TEXT + preserved raw TEXT), summary, sentiment, notes, status, forensic hash + capture metadata, `ai_provenance` JSONB. RLS: owner only. |
| `speakers` | Per-user learned speaker profiles (name, channel, azure_profile_id, voice_embedding). |
| `speaker_utterances` | Timestamped, diarized transcript segments linked to analysis + speaker. |
| `custody_log` | Append-only audit trail (action, details, actor, timestamps). No UPDATE/DELETE. |
| `legal_entities` | Extracted case numbers, statutes, citations, courts with confidence scores + verification status. |
| `facts`, `quotes`, `timeline_events`, `entities`, `chapters` | Insight tables referenced by routes (knowledge, people, quotes, timeline, chapters); confidence + verification columns added in legal-precision migration. |
