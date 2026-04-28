import { createFileRoute, useNavigate, Link } from "@tanstack/react-router";
import { useEffect, useState, useRef, useCallback } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Loader2, ArrowLeft, AlertCircle, Clock, Users, FileText,
  Sparkles, Calendar, Download, MessageSquare, Send, User,
  ChevronDown, ChevronUp, Copy, Check
} from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/analysis/$id")({
  component: AnalysisPage,
});

const WORKER_URL = import.meta.env.VITE_WORKER_URL || "https://insightful-tube-explorer-production.up.railway.app";

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
  raw_transcript: any;
};

type Utterance = {
  id: string;
  diarization_label: string | null;
  start_seconds: number | null;
  end_seconds: number | null;
  text: string;
};

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  updates?: Record<string, string>;
};

const STATUS_STEPS = ["pending", "extracting", "transcribing", "processing", "complete"];

const STATUS_RANGES: Record<string, [number, number]> = {
  pending:      [0, 8],
  extracting:   [8, 32],
  transcribing: [32, 68],
  processing:   [68, 94],
  complete:     [100, 100],
  failed:       [0, 0],
};

const STAGE_DURATION: Record<string, number> = {
  pending: 5,
  extracting: 20,
  transcribing: 40,
  processing: 15,
};

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

// ── Progress Bar ─────────────────────────────────────────────────────────────
function NumericalProgressBar({ status }: { status: string }) {
  const [progress, setProgress] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const statusRef = useRef(status);

  useEffect(() => {
    statusRef.current = status;
    if (status === "complete") {
      setProgress(100);
      if (intervalRef.current) clearInterval(intervalRef.current);
      return;
    }
    if (status === "failed") {
      if (intervalRef.current) clearInterval(intervalRef.current);
      return;
    }
    const range = STATUS_RANGES[status] ?? [0, 10];
    const duration = STAGE_DURATION[status] ?? 10;
    const [min, max] = range;
    setProgress((prev) => Math.max(prev, min));
    if (intervalRef.current) clearInterval(intervalRef.current);
    const tickMs = 500;
    const totalTicks = (duration * 1000) / tickMs;
    const increment = (max - min) / totalTicks;
    intervalRef.current = setInterval(() => {
      setProgress((prev) => {
        const currentRange = STATUS_RANGES[statusRef.current] ?? [0, 10];
        const ceiling = currentRange[1];
        const next = prev + increment * (0.5 + Math.random() * 0.8);
        return Math.min(next, ceiling);
      });
    }, tickMs);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [status]);

  if (status === "complete" || status === "failed") return null;

  const displayPct = Math.round(progress);
  const labels: Record<string, string> = {
    pending: "Queued — waiting for worker...",
    extracting: "Extracting transcript from YouTube...",
    transcribing: "Processing transcript...",
    processing: "Generating insights with AI...",
  };

  return (
    <div className="rounded-xl border border-border bg-surface/40 p-6">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          <span className="text-sm font-medium">{labels[status] ?? "Processing..."}</span>
        </div>
        <span className="text-2xl font-bold tabular-nums text-primary">{displayPct}%</span>
      </div>
      <div className="w-full bg-muted rounded-full h-3 overflow-hidden">
        <div
          className="bg-gradient-to-r from-primary to-primary/80 h-3 rounded-full transition-all duration-500 ease-out relative"
          style={{ width: `${displayPct}%` }}
        >
          <div className="absolute inset-0 bg-white/20 animate-pulse rounded-full" />
        </div>
      </div>
      <div className="flex justify-between mt-2">
        {STATUS_STEPS.slice(0, -1).map((step, i) => {
          const isActive = status === step;
          const isDone = STATUS_STEPS.indexOf(status) > i;
          return (
            <span key={step} className={`text-xs capitalize transition-colors ${
              isActive ? "text-primary font-medium" : isDone ? "text-muted-foreground" : "text-muted-foreground/40"
            }`}>
              {isDone ? "✓ " : ""}{step}
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ── Speaker Info Card ────────────────────────────────────────────────────────
function SpeakerInfoCard({ speakersInfo }: { speakersInfo: any[] }) {
  const [expanded, setExpanded] = useState(false);
  if (!speakersInfo?.length) return null;

  return (
    <div className="rounded-xl border border-border bg-surface/40 p-4 mb-6">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center justify-between w-full"
      >
        <div className="flex items-center gap-2">
          <Users className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-medium">
            {speakersInfo.length} speaker{speakersInfo.length > 1 ? "s" : ""} identified
          </span>
        </div>
        {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
      </button>
      {expanded && (
        <div className="mt-3 space-y-2">
          {speakersInfo.map((sp: any, i: number) => (
            <div key={i} className="flex items-start gap-3 p-2 rounded-lg bg-muted/50">
              <div className="h-8 w-8 rounded-full bg-primary/10 grid place-items-center shrink-0">
                <User className="h-4 w-4 text-primary" />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{sp.likely_name || sp.label}</span>
                  {sp.speaking_percentage != null && (
                    <span className="text-xs text-muted-foreground">{sp.speaking_percentage}%</span>
                  )}
                </div>
                {sp.role && <p className="text-xs text-muted-foreground">{sp.role}</p>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── AI Chat Panel ────────────────────────────────────────────────────────────
function AIChatPanel({ analysisId, onUpdate }: { analysisId: string; onUpdate: () => void }) {
  const { user } = useAuth();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  async function sendMessage(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() || sending) return;

    const userMsg = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    setSending(true);

    try {
      const res = await fetch(`${WORKER_URL}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          analysis_id: analysisId,
          message: userMsg,
          user_id: user?.id,
        }),
      });
      const data = await res.json();
      if (data.error) {
        setMessages((prev) => [...prev, { role: "assistant", content: `Error: ${data.error}` }]);
      } else {
        setMessages((prev) => [...prev, {
          role: "assistant",
          content: data.response,
          updates: data.updates_applied,
        }]);
        if (data.updates_applied && Object.keys(data.updates_applied).length > 0) {
          onUpdate();
          toast.success("Analysis updated!");
        }
      }
    } catch (err: any) {
      setMessages((prev) => [...prev, { role: "assistant", content: `Error: ${err.message}` }]);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="rounded-xl border border-border bg-surface/40 flex flex-col" style={{ height: "500px" }}>
      <div className="px-4 py-3 border-b border-border flex items-center gap-2">
        <MessageSquare className="h-4 w-4 text-primary" />
        <span className="text-sm font-medium">AI Chat</span>
        <span className="text-xs text-muted-foreground ml-auto">Ask questions or request changes</span>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <div className="text-center py-8">
            <MessageSquare className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">
              Ask anything about this video. You can also tell me to fix dates, names, or details.
            </p>
            <div className="flex flex-wrap justify-center gap-2 mt-4">
              {["Fix the recording date", "Who are the speakers?", "Summarize the key claims", "Update the notes"].map((s) => (
                <button
                  key={s}
                  onClick={() => setInput(s)}
                  className="text-xs px-3 py-1.5 rounded-full border border-border hover:bg-muted transition-colors"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[85%] rounded-xl px-4 py-2.5 text-sm ${
              msg.role === "user"
                ? "bg-primary text-primary-foreground"
                : "bg-muted"
            }`}>
              <p className="whitespace-pre-wrap">{msg.content}</p>
              {msg.updates && Object.keys(msg.updates).length > 0 && (
                <div className="mt-2 pt-2 border-t border-white/20 text-xs opacity-80">
                  ✓ Updated: {Object.keys(msg.updates).join(", ")}
                </div>
              )}
            </div>
          </div>
        ))}
        {sending && (
          <div className="flex justify-start">
            <div className="bg-muted rounded-xl px-4 py-2.5">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          </div>
        )}
      </div>

      <form onSubmit={sendMessage} className="p-3 border-t border-border flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about this video or request changes..."
          className="flex-1 bg-muted rounded-lg px-4 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/40"
          disabled={sending}
        />
        <Button type="submit" size="sm" disabled={!input.trim() || sending}>
          <Send className="h-4 w-4" />
        </Button>
      </form>
    </div>
  );
}

// ── Polished Transcript View ─────────────────────────────────────────────────
function PolishedTranscriptView({ transcript }: { transcript: string | null }) {
  const [copied, setCopied] = useState(false);
  if (!transcript) return <p className="text-sm text-muted-foreground">No polished transcript available.</p>;

  const lines = transcript.split("\n").filter((l) => l.trim());

  function handleCopy() {
    navigator.clipboard.writeText(transcript);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
    toast.success("Transcript copied!");
  }

  return (
    <div>
      <div className="flex justify-end mb-3">
        <Button variant="ghost" size="sm" onClick={handleCopy}>
          {copied ? <Check className="h-3.5 w-3.5 mr-1" /> : <Copy className="h-3.5 w-3.5 mr-1" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <div className="space-y-3 max-h-[600px] overflow-y-auto pr-2">
        {lines.map((line, i) => {
          const speakerMatch = line.match(/^\*\*(.+?)\*\*:\s*(.*)/);
          if (speakerMatch) {
            return (
              <div key={i} className="flex gap-3">
                <div className="shrink-0 pt-0.5">
                  <span className="inline-block bg-primary/10 text-primary text-xs font-semibold px-2.5 py-1 rounded-full whitespace-nowrap">
                    {speakerMatch[1]}
                  </span>
                </div>
                <p className="text-sm leading-relaxed">{speakerMatch[2]}</p>
              </div>
            );
          }
          const tsMatch = line.match(/^\[(\d+:\d+)\]\s*(.*)/);
          if (tsMatch) {
            return (
              <div key={i} className="flex gap-3">
                <span className="text-xs text-muted-foreground tabular-nums pt-0.5 shrink-0">{tsMatch[1]}</span>
                <p className="text-sm leading-relaxed">{tsMatch[2]}</p>
              </div>
            );
          }
          return <p key={i} className="text-sm leading-relaxed">{line}</p>;
        })}
      </div>
    </div>
  );
}

// ── Export ────────────────────────────────────────────────────────────────────
function ExportButton({ analysisId, title }: { analysisId: string; title: string }) {
  const [exporting, setExporting] = useState(false);

  async function handleExport() {
    setExporting(true);
    try {
      const res = await fetch(`${WORKER_URL}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ analysis_id: analysisId }),
      });
      const data = await res.json();
      if (data.error) {
        toast.error(data.error);
        return;
      }
      // Download as text file
      const blob = new Blob([data.text], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const safeName = (data.title || title || "export").replace(/[^a-z0-9]/gi, "_").slice(0, 60);
      a.download = `TubeScribe_${safeName}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast.success("Export downloaded!");
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setExporting(false);
    }
  }

  return (
    <Button variant="outline" size="sm" onClick={handleExport} disabled={exporting}>
      {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : <Download className="h-3.5 w-3.5 mr-1" />}
      Export All
    </Button>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────
function AnalysisPage() {
  const { id } = Route.useParams();
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [a, setA] = useState<Analysis | null>(null);
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [activeTab, setActiveTab] = useState<string>("summary");

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  const fetchAnalysis = useCallback(async () => {
    if (!user) return;
    const { data, error } = await supabase.from("analyses").select("*").eq("id", id).single();
    if (error) toast.error(error.message);
    else setA(data as Analysis);
  }, [id, user]);

  useEffect(() => {
    fetchAnalysis();
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

  // Extract speakers info from raw_transcript
  const speakersInfo = a.raw_transcript?.speakers_info || [];

  const tabs = [
    { key: "summary", label: "Summary", icon: Sparkles },
    { key: "polished", label: "Polished", icon: FileText },
    { key: "transcript", label: "Raw", icon: Users },
    { key: "notes", label: "Notes", icon: FileText },
    { key: "sentiment", label: "Sentiment", icon: Clock },
    { key: "chat", label: "AI Chat", icon: MessageSquare },
  ];

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <div className="flex items-center justify-between mb-6">
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
          </Button>
          {a.status === "complete" && (
            <ExportButton analysisId={a.id} title={a.title ?? "export"} />
          )}
        </div>

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

        {a.status !== "complete" && a.status !== "failed" && (
          <div className="mb-6">
            <NumericalProgressBar status={a.status} />
          </div>
        )}

        {a.status === "failed" && (
          <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-6 mb-6 flex gap-3">
            <AlertCircle className="h-5 w-5 text-destructive shrink-0 mt-0.5" />
            <div>
              <p className="font-medium text-destructive">Processing failed</p>
              <p className="text-sm text-muted-foreground mt-1">{a.error_message ?? "An unknown error occurred."}</p>
            </div>
          </div>
        )}

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

        {a.status === "complete" && (
          <>
            <SpeakerInfoCard speakersInfo={speakersInfo} />

            <div className="flex gap-1 border-b border-border mb-6 overflow-x-auto">
              {tabs.map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  onClick={() => setActiveTab(key)}
                  className={`flex items-center gap-1.5 px-3 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px whitespace-nowrap ${
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

            {activeTab === "summary" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-3">Summary</h2>
                <p className="text-sm leading-relaxed whitespace-pre-wrap">{a.summary ?? "No summary available."}</p>
              </div>
            )}

            {activeTab === "polished" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-4">Polished Transcript</h2>
                <PolishedTranscriptView transcript={a.polished_transcript} />
              </div>
            )}

            {activeTab === "transcript" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-4">Raw Speaker Transcript</h2>
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
                  <p className="text-sm text-muted-foreground">
                    No raw utterances — check the Polished tab for the formatted transcript.
                  </p>
                )}
              </div>
            )}

            {activeTab === "notes" && (
              <div className="rounded-xl border border-border bg-surface/40 p-6">
                <h2 className="font-display text-lg font-semibold mb-3">Expanded Notes</h2>
                <div className="text-sm leading-relaxed whitespace-pre-wrap prose prose-sm max-w-none">
                  {a.expanded_notes ?? "No notes available."}
                </div>
              </div>
            )}

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

            {activeTab === "chat" && (
              <AIChatPanel analysisId={a.id} onUpdate={fetchAnalysis} />
            )}
          </>
        )}
      </main>
    </div>
  );
}
