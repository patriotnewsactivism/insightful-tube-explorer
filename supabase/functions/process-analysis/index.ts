// Supabase Edge Function: process-analysis
// Triggered via Supabase Database Webhook on analyses INSERT
// Pipeline: YouTube audio → Azure Blob → Azure Speech (diarization) → Azure OpenAI → Supabase

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const AZURE_SPEECH_ENDPOINT = Deno.env.get("AZURE_SPEECH_ENDPOINT") ?? "https://eastus.api.cognitive.microsoft.com/";
const AZURE_SPEECH_API_KEY = Deno.env.get("AZURE_SPEECH_API_KEY")!;
const AZURE_STORAGE_CONNECTION_STRING = Deno.env.get("AZURE_STORAGE_CONNECTION_STRING")!;
const AZURE_STORAGE_ACCOUNT_NAME = "wtptranscriptionstorage";
const AZURE_STORAGE_CONTAINER = "transcriptions";
const AZURE_OPENAI_ENDPOINT = Deno.env.get("AZURE_OPENAI_ENDPOINT") ?? "https://openaiyoutube.openai.azure.com/openai/responses";
const AZURE_OPENAI_API_KEY = Deno.env.get("AZURE_OPENAI_API_KEY")!;
const AZURE_OPENAI_DEPLOYMENT = Deno.env.get("AZURE_OPENAI_DEPLOYMENT") ?? "gpt-5-mini";

// ---------------------------------------------------------------------------
// YouTube URL → Video ID extraction (mirrors src/lib/youtube.ts)
// ---------------------------------------------------------------------------
function extractYouTubeId(url: string): string | null {
  const trimmed = url.trim();
  if (/^[A-Za-z0-9_-]{11}$/.test(trimmed)) return trimmed;
  try {
    const u = new URL(trimmed);
    if (u.hostname === "youtu.be") {
      const id = u.pathname.slice(1).split("/")[0];
      return id || null;
    }
    if (u.hostname.includes("youtube.com") || u.hostname.includes("youtube-nocookie.com")) {
      if (u.pathname === "/watch" || u.pathname === "/watch/") return u.searchParams.get("v");
      for (const prefix of ["/embed/", "/shorts/", "/live/", "/v/", "/e/"]) {
        if (u.pathname.startsWith(prefix)) {
          const id = u.pathname.slice(prefix.length).split(/[/?]/)[0];
          return id || null;
        }
      }
      const vParam = u.searchParams.get("v");
      if (vParam) return vParam;
    }
    return null;
  } catch {
    const match = trimmed.match(
      /(?:youtu\.be\/|youtube\.com\/(?:watch\?.*v=|embed\/|shorts\/|live\/|v\/|e\/))([A-Za-z0-9_-]{11})/
    );
    return match?.[1] ?? null;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const sb = () => createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

async function setStatus(id: string, status: string, extra: Record<string, unknown> = {}) {
  await sb().from("analyses").update({ status, ...extra }).eq("id", id);
}

async function fail(id: string, message: string) {
  console.error(`[process-analysis] FAILED ${id}: ${message}`);
  await sb().from("analyses").update({ status: "failed", error_message: message }).eq("id", id);
}

// ---------------------------------------------------------------------------
// Step 1: Download YouTube audio via yt-dlp
// ---------------------------------------------------------------------------
async function downloadAudio(youtubeUrl: string, outputPath: string): Promise<void> {
  const cmd = new Deno.Command("yt-dlp", {
    args: ["--no-playlist", "-x", "--audio-format", "mp3", "--audio-quality", "96K", "-o", outputPath, youtubeUrl],
    stdout: "piped",
    stderr: "piped",
  });
  const { code, stderr } = await cmd.output();
  if (code !== 0) throw new Error(`yt-dlp failed (code ${code}): ${new TextDecoder().decode(stderr)}`);
}

// ---------------------------------------------------------------------------
// Step 2: Azure Blob upload with Shared Key auth
// ---------------------------------------------------------------------------
function parseConnStr(cs: string): Record<string, string> {
  return Object.fromEntries(
    cs.split(";").filter(Boolean).map((p) => {
      const idx = p.indexOf("=");
      return [p.slice(0, idx), p.slice(idx + 1)];
    })
  );
}

async function hmacSha256(key: Uint8Array, data: string): Promise<Uint8Array> {
  const k = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(data)));
}

const b64 = (buf: Uint8Array) => btoa(String.fromCharCode(...buf));

async function uploadToAzureBlob(filePath: string, blobName: string): Promise<void> {
  const { AccountKey } = parseConnStr(AZURE_STORAGE_CONNECTION_STRING);
  const fileData = await Deno.readFile(filePath);
  const contentType = "audio/mpeg";
  const date = new Date().toUTCString();

  const stringToSign = [
    "PUT", "", "", String(fileData.byteLength), "", contentType, "",
    "", "", "", "", "",
    `x-ms-blob-type:BlockBlob\nx-ms-date:${date}\nx-ms-version:2020-04-08`,
    `/${AZURE_STORAGE_ACCOUNT_NAME}/${AZURE_STORAGE_CONTAINER}/${blobName}`,
  ].join("\n");

  const keyBytes = Uint8Array.from(atob(AccountKey), (c) => c.charCodeAt(0));
  const sig = await hmacSha256(keyBytes, stringToSign);
  const auth = `SharedKey ${AZURE_STORAGE_ACCOUNT_NAME}:${b64(sig)}`;

  const url = `https://${AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net/${AZURE_STORAGE_CONTAINER}/${blobName}`;
  const res = await fetch(url, {
    method: "PUT",
    headers: {
      Authorization: auth,
      "x-ms-blob-type": "BlockBlob",
      "x-ms-date": date,
      "x-ms-version": "2020-04-08",
      "Content-Type": contentType,
      "Content-Length": String(fileData.byteLength),
    },
    body: fileData,
  });

  if (!res.ok) throw new Error(`Blob upload failed (${res.status}): ${await res.text()}`);
}

async function generateSasUrl(blobName: string): Promise<string> {
  const { AccountKey } = parseConnStr(AZURE_STORAGE_CONNECTION_STRING);
  const start = new Date(); start.setMinutes(start.getMinutes() - 5);
  const expiry = new Date(); expiry.setHours(expiry.getHours() + 6);
  const startStr = start.toISOString().replace(/\.\d+Z$/, "Z");
  const expiryStr = expiry.toISOString().replace(/\.\d+Z$/, "Z");
  const version = "2020-04-08";

  const stringToSign = [
    "r", startStr, expiryStr,
    `/${AZURE_STORAGE_ACCOUNT_NAME}/${AZURE_STORAGE_CONTAINER}/${blobName}`,
    "", "", "https", version, "b", "", "", "", "", "", "",
  ].join("\n");

  const keyBytes = Uint8Array.from(atob(AccountKey), (c) => c.charCodeAt(0));
  const sig = await hmacSha256(keyBytes, stringToSign);

  return `https://${AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net/${AZURE_STORAGE_CONTAINER}/${blobName}` +
    `?sv=${version}&st=${encodeURIComponent(startStr)}&se=${encodeURIComponent(expiryStr)}&sr=b&sp=r&spr=https&sig=${encodeURIComponent(b64(sig))}`;
}

// ---------------------------------------------------------------------------
// Step 3–5: Azure Speech batch transcription
// ---------------------------------------------------------------------------
async function submitTranscriptionJob(sasUrl: string, analysisId: string): Promise<string> {
  const endpoint = AZURE_SPEECH_ENDPOINT.replace(/\/$/, "");
  const res = await fetch(`${endpoint}/speechtotext/v3.1/transcriptions`, {
    method: "POST",
    headers: { "Ocp-Apim-Subscription-Key": AZURE_SPEECH_API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({
      contentUrls: [sasUrl],
      locale: "en-US",
      displayName: `analysis-${analysisId}`,
      properties: {
        diarizationEnabled: true,
        wordLevelTimestampsEnabled: false,
        punctuationMode: "DictatedAndAutomatic",
        profanityFilterMode: "None",
      },
    }),
  });
  if (!res.ok) throw new Error(`Speech submit failed (${res.status}): ${await res.text()}`);
  const data = await res.json();
  return data.self as string;
}

async function pollTranscriptionJob(jobUrl: string): Promise<any> {
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    const res = await fetch(jobUrl, { headers: { "Ocp-Apim-Subscription-Key": AZURE_SPEECH_API_KEY } });
    if (!res.ok) throw new Error(`Poll failed (${res.status})`);
    const data = await res.json();
    if (data.status === "Succeeded") return data;
    if (data.status === "Failed") throw new Error(`Speech job failed: ${JSON.stringify(data)}`);
  }
  throw new Error("Speech job timed out after 10 minutes");
}

async function fetchTranscriptionResults(jobData: any): Promise<any> {
  const filesRes = await fetch(jobData.links.files, { headers: { "Ocp-Apim-Subscription-Key": AZURE_SPEECH_API_KEY } });
  if (!filesRes.ok) throw new Error(`Files fetch failed (${filesRes.status})`);
  const { values } = await filesRes.json();
  const file = values?.find((f: any) => f.kind === "Transcription");
  if (!file) throw new Error("No transcription file found");
  const res = await fetch(file.links.contentUrl);
  if (!res.ok) throw new Error(`Result fetch failed (${res.status})`);
  return res.json();
}

type Utterance = { speaker: string; text: string; start: number; end: number };

function parseUtterances(result: any): Utterance[] {
  return (result?.recognizedPhrases ?? [])
    .map((phrase: any) => ({
      speaker: phrase.speaker != null ? `Speaker ${phrase.speaker}` : "Unknown",
      text: phrase.nBest?.[0]?.display ?? "",
      start: (phrase.offsetInTicks ?? 0) / 1e7,
      end: ((phrase.offsetInTicks ?? 0) + (phrase.durationInTicks ?? 0)) / 1e7,
    }))
    .filter((u: Utterance) => u.text);
}

// ---------------------------------------------------------------------------
// Step 6: Azure OpenAI via Responses API
// ---------------------------------------------------------------------------
async function callOpenAI(instructions: string, input: string): Promise<string> {
  // Use the Responses API endpoint as provided
  const url = "https://openaiyoutube.openai.azure.com/openai/responses?api-version=2025-04-01-preview";
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "api-key": AZURE_OPENAI_API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: AZURE_OPENAI_DEPLOYMENT,
      instructions,
      input,
      max_output_tokens: 2000,
      temperature: 0.3,
    }),
  });
  if (!res.ok) throw new Error(`Azure OpenAI failed (${res.status}): ${await res.text()}`);
  const data = await res.json();
  // Responses API returns output array of content blocks
  const textBlock = data.output?.find((o: any) => o.type === "message")
    ?.content?.find((c: any) => c.type === "output_text");
  return textBlock?.text ?? data.output_text ?? "";
}

async function generateInsights(transcript: string, title: string | null) {
  const ctx = title ? `Video title: "${title}"\n\n` : "";
  const t = transcript.slice(0, 12000);

  const [summaryRaw, sentimentRaw, notesRaw, dateRaw] = await Promise.all([
    callOpenAI(
      "You are an expert media analyst. Produce a concise 3–5 sentence summary of the key points discussed.",
      `${ctx}Transcript:\n${t}`
    ),
    callOpenAI(
      `Analyze the transcript and return ONLY valid JSON with no markdown fences: {"overall":"positive"|"negative"|"neutral"|"mixed","score":<-1.0 to 1.0>,"tone":"<brief>","key_emotions":["..."]}`,
      `${ctx}Transcript:\n${t}`
    ),
    callOpenAI(
      `Produce detailed expanded notes with these markdown sections: ## Main Topics, ## Key Claims, ## Notable Quotes, ## Action Items, ## Unanswered Questions`,
      `${ctx}Transcript:\n${t}`
    ),
    callOpenAI(
      `Analyze for clues about when this content was produced or recorded (not published). Return ONLY valid JSON with no markdown fences: {"likely_production_date":"<date range>","reasoning":"<brief explanation>"}`,
      `${ctx}Transcript:\n${transcript.slice(0, 8000)}`
    ),
  ]);

  let sentiment: Record<string, unknown> = {};
  try {
    const m = sentimentRaw.match(/\{[\s\S]*\}/);
    if (m) sentiment = JSON.parse(m[0]);
  } catch { sentiment = { raw: sentimentRaw }; }

  let likelyProductionDate = "", productionDateReasoning = "";
  try {
    const m = dateRaw.match(/\{[\s\S]*\}/);
    if (m) {
      const p = JSON.parse(m[0]);
      likelyProductionDate = p.likely_production_date ?? "";
      productionDateReasoning = p.reasoning ?? "";
    }
  } catch { likelyProductionDate = "Unknown"; productionDateReasoning = dateRaw; }

  return { summary: summaryRaw, sentiment, expandedNotes: notesRaw, likelyProductionDate, productionDateReasoning };
}

// ---------------------------------------------------------------------------
// Main handler
// ---------------------------------------------------------------------------
Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405 });

  let payload: any;
  try { payload = await req.json(); } catch { return new Response("Invalid JSON", { status: 400 }); }

  const record = payload?.record ?? payload;
  const { id: analysisId, youtube_url: youtubeUrl, youtube_id: existingYtId, title, user_id, status, pasted_transcript: pastedTranscript } = record ?? {};

  if (!analysisId || !youtubeUrl) return new Response("Missing id or youtube_url", { status: 400 });
  if (status && status !== "pending") return new Response("Not pending, skipping", { status: 200 });

  // Resolve the video ID — use existing if present, otherwise extract from URL
  const videoId = existingYtId || extractYouTubeId(youtubeUrl);
  if (!videoId) {
    await fail(analysisId, `Could not extract video ID from URL: ${youtubeUrl.slice(0, 80)}. Supported formats: youtube.com/watch?v=, youtu.be/, /shorts/, /live/, /embed/.`);
    return new Response(JSON.stringify({ ok: false, error: "Invalid YouTube URL" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  console.log(`[process-analysis] Starting pipeline for ${analysisId} (video: ${videoId})`);

  (async () => {
    const tmpDir = await Deno.makeTempDir();
    const audioPath = `${tmpDir}/audio.mp3`;
    const blobName = `${analysisId}.mp3`;

    try {
      // If a pasted transcript was provided (or client-side transcript was grabbed),
      // skip audio extraction and go straight to AI analysis.
      if (pastedTranscript && typeof pastedTranscript === "string" && pastedTranscript.trim().length > 50) {
        console.log(`[${analysisId}] Using pasted/client transcript (${pastedTranscript.length} chars), skipping audio pipeline`);
        await setStatus(analysisId, "processing");

        const insights = await generateInsights(pastedTranscript.trim(), title ?? null);

        await sb().from("analyses").update({
          status: "complete",
          polished_transcript: pastedTranscript.trim(),
          summary: insights.summary,
          sentiment: insights.sentiment,
          expanded_notes: insights.expandedNotes,
          likely_production_date: insights.likelyProductionDate,
          production_date_reasoning: insights.productionDateReasoning,
        }).eq("id", analysisId);

        console.log(`[${analysisId}] Pipeline complete (pasted transcript) ✓`);
        return;
      }

      // Full audio pipeline: download → upload → transcribe → analyze
      await setStatus(analysisId, "extracting");
      await downloadAudio(youtubeUrl, audioPath);

      await setStatus(analysisId, "transcribing");
      await uploadToAzureBlob(audioPath, blobName);
      const sasUrl = await generateSasUrl(blobName);
      const jobUrl = await submitTranscriptionJob(sasUrl, analysisId);
      const jobData = await pollTranscriptionJob(jobUrl);
      const result = await fetchTranscriptionResults(jobData);

      const utterances = parseUtterances(result);
      const polishedTranscript = utterances.map((u) => `[${u.speaker}]: ${u.text}`).join("\n");

      await setStatus(analysisId, "processing");
      const insights = await generateInsights(polishedTranscript, title ?? null);

      await sb().from("analyses").update({
        status: "complete",
        raw_transcript: result,
        polished_transcript: polishedTranscript,
        summary: insights.summary,
        sentiment: insights.sentiment,
        expanded_notes: insights.expandedNotes,
        likely_production_date: insights.likelyProductionDate,
        production_date_reasoning: insights.productionDateReasoning,
      }).eq("id", analysisId);

      if (utterances.length > 0) {
        const rows = utterances.map((u) => ({
          user_id, analysis_id: analysisId,
          diarization_label: u.speaker,
          start_seconds: u.start, end_seconds: u.end, text: u.text,
        }));
        for (let i = 0; i < rows.length; i += 500) {
          await sb().from("speaker_utterances").insert(rows.slice(i, i + 500));
        }
      }

      console.log(`[${analysisId}] Pipeline complete ✓`);
    } catch (err: any) {
      await fail(analysisId, err?.message ?? String(err));
    } finally {
      await Deno.remove(tmpDir, { recursive: true }).catch(() => {});
    }
  })();

  return new Response(JSON.stringify({ ok: true, id: analysisId }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});
