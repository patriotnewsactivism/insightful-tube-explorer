export function extractYouTubeId(url: string): string | null {
  try {
    const u = new URL(url.trim());
    if (u.hostname === "youtu.be") return u.pathname.slice(1) || null;
    if (u.hostname.includes("youtube.com")) {
      if (u.pathname === "/watch") return u.searchParams.get("v");
      if (u.pathname.startsWith("/embed/")) return u.pathname.split("/")[2] || null;
      if (u.pathname.startsWith("/shorts/")) return u.pathname.split("/")[2] || null;
    }
    return null;
  } catch {
    return null;
  }
}

export async function fetchYouTubeOEmbed(url: string) {
  const res = await fetch(
    `https://www.youtube.com/oembed?url=${encodeURIComponent(url)}&format=json`
  );
  if (!res.ok) throw new Error("Could not fetch video info");
  return res.json() as Promise<{
    title: string;
    author_name: string;
    thumbnail_url: string;
    html: string;
  }>;
}

/* ─── Client-side transcript fetching ─────────────────────────────────────── */
// video.google.com/timedtext has CORS headers (Access-Control-Allow-Origin)
// that mirror the requesting origin, so the user's browser can fetch captions
// directly using their residential IP and YouTube cookies.

interface TimedTextTrack {
  lang: string;
  name: string;
  kind?: string;
}

async function listCaptionTracks(videoId: string): Promise<TimedTextTrack[]> {
  const res = await fetch(
    `https://video.google.com/timedtext?type=list&v=${videoId}`,
    { credentials: "omit" }
  );
  if (!res.ok) return [];
  const text = await res.text();
  if (!text.trim()) return [];

  const parser = new DOMParser();
  const doc = parser.parseFromString(text, "text/xml");
  const tracks: TimedTextTrack[] = [];
  doc.querySelectorAll("track").forEach((el) => {
    tracks.push({
      lang: el.getAttribute("lang_code") || el.getAttribute("lang") || "en",
      name: el.getAttribute("name") || "",
      kind: el.getAttribute("kind") || undefined,
    });
  });
  return tracks;
}

async function fetchTrackContent(
  videoId: string,
  lang: string,
  kind?: string
): Promise<string | null> {
  let url = `https://video.google.com/timedtext?type=track&v=${videoId}&lang=${lang}`;
  if (kind) url += `&kind=${kind}`;

  const res = await fetch(url, { credentials: "omit" });
  if (!res.ok) return null;
  const text = await res.text();
  if (!text.trim()) return null;

  const parser = new DOMParser();
  const doc = parser.parseFromString(text, "text/xml");
  const segments: string[] = [];

  doc.querySelectorAll("text").forEach((el) => {
    const start = parseFloat(el.getAttribute("start") || "0");
    const content = (el.textContent || "")
      .replace(/&amp;/g, "&")
      .replace(/&#39;/g, "'")
      .replace(/&quot;/g, '"')
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .trim();
    if (!content) return;

    const mins = Math.floor(start / 60);
    const secs = Math.floor(start % 60);
    segments.push(`${mins}:${secs.toString().padStart(2, "0")} ${content}`);
  });

  return segments.length > 0 ? segments.join("\n") : null;
}

/**
 * Fetch transcript client-side via video.google.com/timedtext.
 * Uses the user's browser context (residential IP + cookies) which
 * bypasses YouTube's datacenter IP blocking.
 * Returns timestamped transcript text or null if unavailable.
 */
export async function fetchClientTranscript(
  videoId: string
): Promise<string | null> {
  try {
    const tracks = await listCaptionTracks(videoId);

    if (tracks.length === 0) {
      // Try direct fetch with common English variants
      for (const lang of ["en", "en-US"]) {
        for (const kind of ["asr", undefined] as const) {
          const content = await fetchTrackContent(videoId, lang, kind);
          if (content) return content;
        }
      }
      return null;
    }

    // Prefer: manual English > any English > auto English > first track
    const preferred =
      tracks.find((t) => t.lang === "en" && !t.kind) ||
      tracks.find((t) => t.lang.startsWith("en") && !t.kind) ||
      tracks.find((t) => t.lang === "en" && t.kind === "asr") ||
      tracks.find((t) => t.lang.startsWith("en")) ||
      tracks[0];

    if (!preferred) return null;
    return await fetchTrackContent(videoId, preferred.lang, preferred.kind);
  } catch (err) {
    console.warn("[TubeScribe] Client-side transcript fetch failed:", err);
    return null;
  }
}
