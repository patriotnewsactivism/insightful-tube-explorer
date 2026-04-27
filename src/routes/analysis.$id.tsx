import { createFileRoute, useNavigate, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Loader2, ArrowLeft, AlertCircle, Clock, Users, FileText, Sparkles, Calendar } from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/analysis/$id")({
  component: AnalysisPage,
});

type Analysis = {
  id: string;
  youtube_url: string;
  youtube_id: string | null;
  title: string | null;
  channel: string | null;
  thumbnail_url: string | null;
  status: string;
  summary: string | null;
  polished_transcript: string | null;
  expanded_notes: string | null;
  likely_production_date: string | null;
  production_date_reasoning: string | null;
  sentiment: any;
  error_message: string | null;
};

type Utterance = {
  id: string;
  diarization_label: string | null;
  start_seconds: number | null;
  end_seconds: number | null;
  text: string;
};

const STATUS_STEPS = ["pending", "extracting", "transcribing", "processing", "complete"];

function formatSeconds(s: number | null): string {
  if (s == null) return "";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

function StatusBadge({ status }: { status: string }) {
  const variants: Record<string, string> = {
    pending: "secondary",
    extracting: "secondary",
    transcribing: "secondary",
    processing: "secondary",
    complete: "default",
    failed: "destructive",
  };
  return <Badge variant={(variants[status] ?? "secondary") as any} className="capitalize">{status}</Badge>;
}

function ProgressBar({ status }: { status: string }) {
  if (status === "complete" || status === "failed") return null;
  const idx = STATUS_STEPS.indexOf(status);
  const pct = idx < 0 ? 5 : Math.round(((idx + 1) / STATUS_STEPS.length) * 100);
  const labels: Record<string, string> = {
    pending: "Queued...",
    extracting: "Downloading audio...",
    transcribing: "Transcribing with Azure Speech...",
    processing: "Generating insights with Azure OpenAI...",
  };
  return (
    <div className="rounded-xl border border-border bg-surface/40 p-6">
      <div className="flex items-center gap-2 mb-3">
        <Loader2 className="h-4 w-4 animate-spin text-primary" />
        <span className="text-sm font-medium">{labels[status] ?? "Processing..."}</span>
      </div>
      <div className="w-full bg-muted rounded-full h-2">
        <div className="bg-primary h-2 rounded-full transition-all duration-700" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function AnalysisPage() {
  const { id } = Route.useParams();
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [a, setA] = useState<Analysis | null>(null);
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [activeTab, setActiveTab] = useState<"summary" | "transcript" | "notes" | "sentiment">("summary");

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase.from("analyses").select("*").eq("id", id).single();
      if (error) toast.error(error.message);
      else setA(data as Analysis);
    })();

    // Realtime subscription for status updates
    const channel = supabase
      .channel(`analysis:${id}`)
      .on("postgres_changes", { event: "UPDATE", schema: "public", table: "analyses", filter: `id=eq.${id}` }, (payload) => {
        setA(payload.new as Analysis);
        if (payload.new.status === "complete") fetchUtterances();
      })
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, [id, user]);

  async function fetchUtterances() {
    const { data } = await supabase
      .from("speaker_utterances")
      .select("id, diarization_label, start_seconds, end_seconds, text")
      .eq("analysis_id", id)
      .order("start_seconds", { ascending: true });
    if (data) setUtterances(data as Utterance[]);
  }

  useEffect(() => {
    if (a?.status === "complete") fetchUtterances();
  }, [a?.status]);

  if (!a) {
    return (
      <div className="min-h-screen">
        <SiteHeader />
        <div className="grid place-items-center py-32">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      </div>
    );
  }

  const tabs = [
    { key: "summary", label: "Summary", icon: Sparkles },
    { key: "transcript", label: "Transcript", icon: Users },
    { key: "notes", label: "Notes", icon: FileText },
    { key: "sentiment", label: "Sentiment", icon: Clock },
  ] as const;

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="mb-6 -ml-2">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
        </Button>

        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="font-display text-3xl md:text-4xl font-semibold tracking-tight leading-tight">
              {a.title ?? "Untitled"}
            </h1>
            {a.channel && <p className="mt-2 text-muted-foreground">{a.channel}</p>}
          </div>
          <StatusBadge status={a.status} />
        </div>

        {a.youtube_id && (
          <div className="aspect-video rounded-xl overflow-hidden border border-border bg-black mb-6 shadow-[var(--shadow-card)]">
            <iframe
              src={`https://www.youtube.com/embed/${a.youtube_id}`}
              title={a.title ?? "video"}
              className="h-full w-full"
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              allowFullScreen
            />
          </div>
        )}

        {/* Progress bar while processing */}
        {a.status !== "complete" && a.status !== "failed" && (
          <div className="mb-6">
            <ProgressBar status={a.status} />
          </div>
        )}

        {/* Error state */}
        {a.status === "failed" && (
          <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-6 mb-6 flex gap-3">
            <AlertCircle className="h-5 w-5 text-destructive shrink-0 mt-0.5" />
            <div>
              <p className="font-medium text-destructive">Processing failed</p>
              <p className="text-sm text-muted-foreground mt-1">{a.error_message ?? "An unknown error occurred."}</p>
            </div>
          </div>
        )}

        {/* Production date estimate */}
        {a.likely_production_date && (
          <div className="rounded-xl border border-border bg-surface/40 p-4 mb-6 flex gap-3 items-start">
            <Calendar className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
            <div>
              <span className="text-sm font-medium">Likely recorded: </span>
              <span className="text-sm">{a.likely_production_date}</span>
              {a.production_date_reasoning && (
                <p className="text-xs text-muted-foreground mt-1">{a.production_date_reasoning}</p>
              )}
            </div>
          </div>
        )}

        {/* Tabs — only show when complete */}
        {a.status === "complete" && (
          <>
            <div className="flex gap-1 border-b border-border mb-6">
              {tabs.map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  onClick={() => setActiveTab(key)}
                  className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${
                    activeTab === key
                      ? "border-primary text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </button>
              ))}
            </div>

            {/* Summary */}
            {activeTab === "summary" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-3">Summary</h2>
                <p className="text-sm leading-relaxed whitespace-pre-wrap">{a.summary ?? "No summary available."}</p>
              </div>
            )}

            {/* Transcript with diarization */}
            {activeTab === "transcript" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-4">Speaker Transcript</h2>
                {utterances.length > 0 ? (
                  <div className="space-y-3 max-h-[600px] overflow-y-auto pr-2">
                    {utterances.map((u) => (
                      <div key={u.id} className="flex gap-3">
                        <div className="shrink-0 pt-0.5">
                          <span className="inline-block bg-primary/10 text-primary text-xs font-medium px-2 py-0.5 rounded-full whitespace-nowrap">
                            {u.diarization_label ?? "Unknown"}
                          </span>
                          {u.start_seconds != null && (
                            <p className="text-xs text-muted-foreground text-center mt-0.5">
                              {formatSeconds(u.start_seconds)}
                            </p>
                          )}
                        </div>
                        <p className="text-sm leading-relaxed">{u.text}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground whitespace-pre-wrap">
                    {a.polished_transcript ?? "No transcript available."}
                  </p>
                )}
              </div>
            )}

            {/* Expanded notes */}
            {activeTab === "notes" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-3">Expanded Notes</h2>
                <div className="text-sm leading-relaxed whitespace-pre-wrap prose prose-sm max-w-none">
                  {a.expanded_notes ?? "No notes available."}
                </div>
              </div>
            )}

            {/* Sentiment */}
            {activeTab === "sentiment" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-4">Sentiment Analysis</h2>
                {a.sentiment ? (
                  <div className="space-y-4">
                    <div className="flex items-center gap-3">
                      <span className="text-sm text-muted-foreground w-24">Overall</span>
                      <Badge className="capitalize">{a.sentiment.overall ?? "Unknown"}</Badge>
                    </div>
                    {a.sentiment.score != null && (
                      <div className="flex items-center gap-3">
                        <span className="text-sm text-muted-foreground w-24">Score</span>
                        <div className="flex items-center gap-2">
                          <div className="w-40 bg-muted rounded-full h-2">
                            <div
                              className="h-2 rounded-full bg-primary transition-all"
                              style={{ width: `${((a.sentiment.score + 1) / 2) * 100}%` }}
                            />
                          </div>
                          <span className="text-sm font-mono">{Number(a.sentiment.score).toFixed(2)}</span>
                        </div>
                      </div>
                    )}
                    {a.sentiment.tone && (
                      <div className="flex items-start gap-3">
                        <span className="text-sm text-muted-foreground w-24">Tone</span>
                        <span className="text-sm">{a.sentiment.tone}</span>
                      </div>
                    )}
                    {a.sentiment.key_emotions?.length > 0 && (
                      <div className="flex items-start gap-3">
                        <span className="text-sm text-muted-foreground w-24">Emotions</span>
                        <div className="flex flex-wrap gap-1">
                          {a.sentiment.key_emotions.map((e: string) => (
                            <Badge key={e} variant="secondary">{e}</Badge>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No sentiment data available.</p>
                )}
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
