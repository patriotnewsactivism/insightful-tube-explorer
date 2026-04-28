import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { extractYouTubeId, fetchYouTubeOEmbed, fetchClientTranscript } from "@/lib/youtube";
import { toast } from "sonner";
import { Plus, Loader2, Play, ClipboardPaste, ChevronDown, ChevronUp, Zap } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

export const Route = createFileRoute("/dashboard")({
  component: Dashboard,
});

type Analysis = {
  id: string;
  youtube_url: string;
  title: string | null;
  channel: string | null;
  thumbnail_url: string | null;
  status: string;
  created_at: string;
};

function Dashboard() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [pastedTranscript, setPastedTranscript] = useState("");
  const [showPaste, setShowPaste] = useState(false);
  const [creating, setCreating] = useState(false);
  const [fetchStatus, setFetchStatus] = useState<"idle" | "fetching" | "success" | "failed">("idle");
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [listLoading, setListLoading] = useState(true);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("analyses")
        .select("id, youtube_url, title, channel, thumbnail_url, status, created_at")
        .order("created_at", { ascending: false });
      if (error) toast.error(error.message);
      else setAnalyses(data as Analysis[]);
      setListLoading(false);
    })();
  }, [user]);

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!user) return;
    const ytId = extractYouTubeId(url);
    if (!ytId) return toast.error("That doesn't look like a YouTube URL");

    setCreating(true);
    setFetchStatus("fetching");

    try {
      // Fetch metadata + transcript in parallel
      const [oembed, clientTranscript] = await Promise.all([
        fetchYouTubeOEmbed(url).catch(() => null),
        // If user already pasted a transcript, skip auto-fetch
        showPaste && pastedTranscript.trim().length > 50
          ? Promise.resolve(null)
          : fetchClientTranscript(ytId),
      ]);

      // Use pasted transcript first, then auto-fetched, then null
      const transcript =
        showPaste && pastedTranscript.trim().length > 50
          ? pastedTranscript.trim()
          : clientTranscript;

      if (clientTranscript) {
        setFetchStatus("success");
        toast.success("Transcript grabbed automatically!");
      } else if (!(showPaste && pastedTranscript.trim().length > 50)) {
        setFetchStatus("failed");
      }

      const insertData: Record<string, unknown> = {
        user_id: user.id,
        youtube_url: url,
        youtube_id: ytId,
        title: oembed?.title ?? null,
        channel: oembed?.author_name ?? null,
        thumbnail_url:
          oembed?.thumbnail_url ??
          `https://i.ytimg.com/vi/${ytId}/hqdefault.jpg`,
        status: "pending",
      };
      if (transcript) {
        insertData.pasted_transcript = transcript;
      }

      const { data, error } = await supabase
        .from("analyses")
        .insert(insertData)
        .select("id")
        .single();
      if (error) throw error;
      navigate({ to: "/analysis/$id", params: { id: data.id } });
    } catch (err: any) {
      toast.error(err.message ?? "Could not create analysis");
    } finally {
      setCreating(false);
      setFetchStatus("idle");
    }
  }

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-5xl px-6 py-12">
        <div className="mb-10">
          <h1 className="font-display text-4xl font-semibold tracking-tight">
            Your library
          </h1>
          <p className="mt-2 text-muted-foreground">
            Paste a YouTube URL to analyze it. TubeScribe auto-grabs the transcript — one click.
          </p>
        </div>

        <form onSubmit={onCreate} className="mb-12 space-y-3">
          <div className="flex gap-2">
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.youtube.com/watch?v=..."
              className="h-12 text-base"
            />
            <Button type="submit" size="lg" disabled={creating || !url}>
              {creating ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  {fetchStatus === "fetching" ? "Grabbing transcript…" : "Creating…"}
                </>
              ) : (
                <>
                  <Zap className="h-4 w-4" />
                  Analyze
                </>
              )}
            </Button>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setShowPaste(!showPaste)}
              className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              <ClipboardPaste className="h-3.5 w-3.5" />
              Paste transcript manually
              {showPaste ? (
                <ChevronUp className="h-3 w-3" />
              ) : (
                <ChevronDown className="h-3 w-3" />
              )}
            </button>
            <span className="text-xs text-muted-foreground/60">
              (TubeScribe tries to grab it automatically first)
            </span>
          </div>

          {showPaste && (
            <div className="rounded-xl border border-border bg-surface/40 p-4 space-y-3 animate-in slide-in-from-top-2 duration-200">
              <div className="flex items-start gap-3">
                <div className="shrink-0 h-6 w-6 rounded-full bg-red-100 dark:bg-red-950/50 grid place-items-center">
                  <span className="text-xs font-bold text-red-600">?</span>
                </div>
                <p className="text-xs text-muted-foreground leading-relaxed">
                  If auto-grab doesn't work for a video, you can paste the transcript
                  manually. On YouTube, tap <strong>⋯</strong> →{" "}
                  <strong>Show transcript</strong> → select all text and paste below.
                </p>
              </div>
              <textarea
                value={pastedTranscript}
                onChange={(e) => setPastedTranscript(e.target.value)}
                placeholder="Paste the transcript text here..."
                className="w-full min-h-[160px] rounded-lg border border-border bg-background px-4 py-3 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-red-500/40 resize-y"
              />
              {pastedTranscript.trim().length > 0 &&
                pastedTranscript.trim().length < 50 && (
                  <p className="text-xs text-amber-600">
                    Transcript looks too short. Make sure you copied the full
                    transcript.
                  </p>
                )}
              {pastedTranscript.trim().length >= 50 && (
                <p className="text-xs text-green-600">
                  ✓{" "}
                  {pastedTranscript
                    .trim()
                    .split(/\s+/)
                    .length.toLocaleString()}{" "}
                  words ready for analysis
                </p>
              )}
            </div>
          )}
        </form>

        {listLoading ? (
          <div className="grid place-items-center py-20">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : analyses.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/30 p-12 text-center">
            <p className="text-muted-foreground">
              No analyses yet. Paste a YouTube URL above to begin.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {analyses.map((a) => (
              <Link
                key={a.id}
                to="/analysis/$id"
                params={{ id: a.id }}
                className="group rounded-xl overflow-hidden border border-border bg-surface/40 hover:bg-surface/70 hover:border-primary/40 transition-all"
              >
                <div className="aspect-video relative bg-muted overflow-hidden">
                  {a.thumbnail_url && (
                    <img
                      src={a.thumbnail_url}
                      alt={a.title ?? ""}
                      className="h-full w-full object-cover"
                    />
                  )}
                  <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent" />
                  <div className="absolute top-2 right-2 rounded-full bg-background/80 backdrop-blur px-2 py-0.5 text-xs capitalize">
                    {a.status}
                  </div>
                  <Play className="absolute inset-0 m-auto h-10 w-10 text-white/0 group-hover:text-white/90 transition-colors" />
                </div>
                <div className="p-4">
                  <h3 className="font-medium line-clamp-2 leading-snug">
                    {a.title ?? a.youtube_url}
                  </h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {a.channel} ·{" "}
                    {formatDistanceToNow(new Date(a.created_at), {
                      addSuffix: true,
                    })}
                  </p>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
