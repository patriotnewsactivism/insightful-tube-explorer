import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { extractYouTubeId, fetchYouTubeOEmbed, fetchClientTranscript } from "@/lib/youtube";
import { toast } from "sonner";
import {
  Plus, Loader2, Play, ClipboardPaste, ChevronDown, ChevronUp,
  Zap, List, Search, BookOpen, Users, Filter, CheckCircle2, XCircle, Clock
} from "lucide-react";
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
  likely_production_date: string | null;
};

function Dashboard() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"single" | "bulk">("single");
  const [url, setUrl] = useState("");
  const [bulkUrls, setBulkUrls] = useState("");
  const [pastedTranscript, setPastedTranscript] = useState("");
  const [showPaste, setShowPaste] = useState(false);
  const [creating, setCreating] = useState(false);
  const [bulkProgress, setBulkProgress] = useState<{ total: number; done: number; current: string } | null>(null);
  const [fetchStatus, setFetchStatus] = useState<"idle" | "fetching" | "success" | "failed">("idle");
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("analyses")
        .select("id, youtube_url, title, channel, thumbnail_url, status, created_at, likely_production_date")
        .order("created_at", { ascending: false });
      if (error) toast.error(error.message);
      else setAnalyses(data as Analysis[]);
      setListLoading(false);
    })();

    // Subscribe to realtime updates for the analyses list
    const channel = supabase
      .channel("analyses-list")
      .on("postgres_changes", { event: "*", schema: "public", table: "analyses" }, (payload) => {
        if (payload.eventType === "INSERT") {
          setAnalyses((prev) => [payload.new as Analysis, ...prev]);
        } else if (payload.eventType === "UPDATE") {
          setAnalyses((prev) =>
            prev.map((a) => (a.id === (payload.new as Analysis).id ? (payload.new as Analysis) : a))
          );
        }
      })
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, [user]);

  // ── Single URL create ──
  async function createSingle(targetUrl: string, transcript?: string) {
    if (!user) return null;
    const ytId = extractYouTubeId(targetUrl);
    if (!ytId) {
      toast.error(`Invalid YouTube URL: ${targetUrl.slice(0, 60)}`);
      return null;
    }

    const [oembed, clientTranscript] = await Promise.all([
      fetchYouTubeOEmbed(targetUrl).catch(() => null),
      transcript ? Promise.resolve(null) : fetchClientTranscript(ytId),
    ]);

    const finalTranscript = transcript || clientTranscript;

    const insertData: Record<string, unknown> = {
      user_id: user.id,
      youtube_url: targetUrl,
      youtube_id: ytId,
      title: oembed?.title ?? null,
      channel: oembed?.author_name ?? null,
      thumbnail_url: oembed?.thumbnail_url ?? `https://i.ytimg.com/vi/${ytId}/hqdefault.jpg`,
      status: "pending",
    };
    if (finalTranscript) insertData.pasted_transcript = finalTranscript;

    const { data, error } = await supabase.from("analyses").insert(insertData).select("id").single();
    if (error) throw error;
    return data.id;
  }

  async function onCreateSingle(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setFetchStatus("fetching");
    try {
      const transcript = showPaste && pastedTranscript.trim().length > 50 ? pastedTranscript.trim() : undefined;
      const id = await createSingle(url, transcript);
      if (id) navigate({ to: "/analysis/$id", params: { id } });
    } catch (err: any) {
      toast.error(err.message ?? "Could not create analysis");
    } finally {
      setCreating(false);
      setFetchStatus("idle");
    }
  }

  // ── Bulk create ──
  async function onCreateBulk(e: React.FormEvent) {
    e.preventDefault();
    const urls = bulkUrls
      .split(/[\n,]+/)
      .map((u) => u.trim())
      .filter((u) => u.length > 5);

    if (urls.length === 0) return toast.error("No valid URLs found");
    if (urls.length > 50) return toast.error("Max 50 URLs at a time");

    setCreating(true);
    setBulkProgress({ total: urls.length, done: 0, current: "" });
    let successCount = 0;

    for (let i = 0; i < urls.length; i++) {
      setBulkProgress({ total: urls.length, done: i, current: urls[i].slice(0, 50) });
      try {
        await createSingle(urls[i]);
        successCount++;
      } catch (err: any) {
        console.error(`Bulk: failed ${urls[i]}: ${err.message}`);
      }
      // Small delay to avoid rate limiting
      if (i < urls.length - 1) await new Promise((r) => setTimeout(r, 300));
    }

    setBulkProgress(null);
    setCreating(false);
    setBulkUrls("");
    toast.success(`${successCount} of ${urls.length} videos queued for analysis!`);
  }

  // ── Filtering ──
  const filtered = analyses.filter((a) => {
    const matchesSearch =
      !searchTerm ||
      (a.title ?? "").toLowerCase().includes(searchTerm.toLowerCase()) ||
      (a.channel ?? "").toLowerCase().includes(searchTerm.toLowerCase()) ||
      (a.likely_production_date ?? "").toLowerCase().includes(searchTerm.toLowerCase());
    const matchesStatus = statusFilter === "all" || a.status === statusFilter;
    return matchesSearch && matchesStatus;
  });

  const counts = {
    all: analyses.length,
    complete: analyses.filter((a) => a.status === "complete").length,
    pending: analyses.filter((a) => ["pending", "extracting", "transcribing", "processing"].includes(a.status)).length,
    failed: analyses.filter((a) => a.status === "failed").length,
  };

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-5xl px-6 py-12">
        <div className="flex items-start justify-between mb-10">
          <div>
            <h1 className="font-display text-4xl font-semibold tracking-tight">Your library</h1>
            <p className="mt-2 text-muted-foreground">
              {analyses.length} video{analyses.length !== 1 ? "s" : ""} analyzed
              {counts.pending > 0 && ` · ${counts.pending} processing`}
            </p>
          </div>
          <div className="flex gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to="/knowledge">
                <BookOpen className="h-3.5 w-3.5 mr-1" /> Knowledge Base
              </Link>
            </Button>
          </div>
        </div>

        {/* ── Mode toggle ── */}
        <div className="flex gap-1 mb-4 p-1 bg-muted rounded-lg w-fit">
          <button
            onClick={() => setMode("single")}
            className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
              mode === "single" ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Zap className="h-3.5 w-3.5 inline mr-1" /> Single
          </button>
          <button
            onClick={() => setMode("bulk")}
            className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
              mode === "bulk" ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <List className="h-3.5 w-3.5 inline mr-1" /> Bulk
          </button>
        </div>

        {/* ── Single mode ── */}
        {mode === "single" && (
          <form onSubmit={onCreateSingle} className="mb-12 space-y-3">
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
                    <Zap className="h-4 w-4" /> Analyze
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
                {showPaste ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              </button>
              <span className="text-xs text-muted-foreground/60">(TubeScribe tries auto-grab first)</span>
            </div>

            {showPaste && (
              <div className="rounded-xl border border-border bg-surface/40 p-4 space-y-3 animate-in slide-in-from-top-2 duration-200">
                <div className="flex items-start gap-3">
                  <div className="shrink-0 h-6 w-6 rounded-full bg-red-100 dark:bg-red-950/50 grid place-items-center">
                    <span className="text-xs font-bold text-red-600">?</span>
                  </div>
                  <p className="text-xs text-muted-foreground leading-relaxed">
                    If auto-grab doesn't work, paste the transcript manually. On YouTube, tap <strong>⋯</strong> → <strong>Show transcript</strong> → select all text.
                  </p>
                </div>
                <textarea
                  value={pastedTranscript}
                  onChange={(e) => setPastedTranscript(e.target.value)}
                  placeholder="Paste the transcript text here..."
                  className="w-full min-h-[160px] rounded-lg border border-border bg-background px-4 py-3 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-red-500/40 resize-y"
                />
                {pastedTranscript.trim().length > 0 && pastedTranscript.trim().length < 50 && (
                  <p className="text-xs text-amber-600">Transcript looks too short.</p>
                )}
                {pastedTranscript.trim().length >= 50 && (
                  <p className="text-xs text-green-600">
                    ✓ {pastedTranscript.trim().split(/\s+/).length.toLocaleString()} words ready
                  </p>
                )}
              </div>
            )}
          </form>
        )}

        {/* ── Bulk mode ── */}
        {mode === "bulk" && (
          <form onSubmit={onCreateBulk} className="mb-12 space-y-3">
            <div className="rounded-xl border border-border bg-surface/40 p-4 space-y-3">
              <div className="flex items-center gap-2 mb-1">
                <List className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm font-medium">Bulk Processing</span>
                <span className="text-xs text-muted-foreground">— Paste up to 50 YouTube URLs (one per line)</span>
              </div>
              <textarea
                value={bulkUrls}
                onChange={(e) => setBulkUrls(e.target.value)}
                placeholder={`https://www.youtube.com/watch?v=abc123\nhttps://youtu.be/def456\nhttps://www.youtube.com/watch?v=ghi789`}
                className="w-full min-h-[180px] rounded-lg border border-border bg-background px-4 py-3 text-sm font-mono placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-red-500/40 resize-y"
                disabled={creating}
              />
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  {bulkUrls.split(/[\n,]+/).filter((u) => u.trim().length > 5).length} URLs detected
                </span>
                <Button type="submit" disabled={creating || !bulkUrls.trim()}>
                  {creating ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin mr-1" />
                      Processing {bulkProgress?.done ?? 0}/{bulkProgress?.total ?? 0}...
                    </>
                  ) : (
                    <>
                      <Zap className="h-4 w-4 mr-1" /> Analyze All
                    </>
                  )}
                </Button>
              </div>
              {bulkProgress && (
                <div className="space-y-1">
                  <div className="w-full bg-muted rounded-full h-2">
                    <div
                      className="bg-red-600 h-2 rounded-full transition-all"
                      style={{ width: `${(bulkProgress.done / bulkProgress.total) * 100}%` }}
                    />
                  </div>
                  <p className="text-xs text-muted-foreground truncate">{bulkProgress.current}</p>
                </div>
              )}
            </div>
          </form>
        )}

        {/* ── Search & Filter Bar ── */}
        {analyses.length > 0 && (
          <div className="flex flex-col sm:flex-row gap-3 mb-6">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder="Search videos, channels, dates..."
                className="pl-9"
              />
            </div>
            <div className="flex gap-1 p-1 bg-muted rounded-lg">
              {(["all", "complete", "pending", "failed"] as const).map((s) => {
                const icons: Record<string, typeof CheckCircle2> = {
                  all: Filter,
                  complete: CheckCircle2,
                  pending: Clock,
                  failed: XCircle,
                };
                const Icon = icons[s];
                return (
                  <button
                    key={s}
                    onClick={() => setStatusFilter(s)}
                    className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                      statusFilter === s ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    <Icon className="h-3 w-3" />
                    <span className="capitalize">{s}</span>
                    <span className="text-muted-foreground/60">({counts[s]})</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* ── Video Grid ── */}
        {listLoading ? (
          <div className="grid place-items-center py-20">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface/30 p-12 text-center">
            <p className="text-muted-foreground">
              {analyses.length === 0
                ? "No analyses yet. Paste a YouTube URL above to begin."
                : "No videos match your search."}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {filtered.map((a) => (
              <Link
                key={a.id}
                to="/analysis/$id"
                params={{ id: a.id }}
                className="group rounded-xl overflow-hidden border border-border bg-surface/40 hover:bg-surface/70 hover:border-primary/40 transition-all"
              >
                <div className="aspect-video relative bg-muted overflow-hidden">
                  {a.thumbnail_url && (
                    <img src={a.thumbnail_url} alt={a.title ?? ""} className="h-full w-full object-cover" />
                  )}
                  <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent" />
                  <div className={`absolute top-2 right-2 rounded-full backdrop-blur px-2 py-0.5 text-xs capitalize ${
                    a.status === "complete" ? "bg-green-500/20 text-green-300" :
                    a.status === "failed" ? "bg-red-500/20 text-red-300" :
                    "bg-background/80"
                  }`}>
                    {["pending", "extracting", "transcribing", "processing"].includes(a.status) ? (
                      <span className="flex items-center gap-1">
                        <Loader2 className="h-3 w-3 animate-spin" /> {a.status}
                      </span>
                    ) : a.status}
                  </div>
                  <Play className="absolute inset-0 m-auto h-10 w-10 text-white/0 group-hover:text-white/90 transition-colors" />
                </div>
                <div className="p-4">
                  <h3 className="font-medium line-clamp-2 leading-snug">{a.title ?? a.youtube_url}</h3>
                  <div className="flex items-center justify-between mt-1">
                    <p className="text-xs text-muted-foreground">
                      {a.channel} · {formatDistanceToNow(new Date(a.created_at), { addSuffix: true })}
                    </p>
                    {a.likely_production_date && (
                      <span className="text-xs text-muted-foreground/60">{a.likely_production_date}</span>
                    )}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
