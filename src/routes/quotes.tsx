import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft, Quote, Loader2, Search, ExternalLink, Copy, Check, User, Filter
} from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/quotes")({ component: QuotesPage });

type QuoteItem = {
  id: string;
  analysis_id: string;
  speaker: string;
  quote_text: string;
  context: string | null;
  timestamp_seconds: number | null;
  significance: string;
  tags: string[];
  analysis?: { title: string | null; youtube_id: string | null };
};

const SIG_COLORS: Record<string, string> = {
  high: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  medium: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
};

const TAG_COLORS: Record<string, string> = {
  evidence: "bg-purple-100 text-purple-700",
  testimony: "bg-blue-100 text-blue-700",
  threat: "bg-red-100 text-red-700",
  admission: "bg-green-100 text-green-700",
  denial: "bg-orange-100 text-orange-700",
  emotional: "bg-pink-100 text-pink-700",
  legal: "bg-indigo-100 text-indigo-700",
};

function QuotesPage() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [quotes, setQuotes] = useState<QuoteItem[]>([]);
  const [quotesLoading, setQuotesLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [speakerFilter, setSpeakerFilter] = useState("all");
  const [sigFilter, setSigFilter] = useState("all");
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("quotes")
        .select("*, analysis:analyses(title, youtube_id)")
        .order("created_at", { ascending: false })
        .limit(500);
      if (!error && data) setQuotes(data as QuoteItem[]);
      setQuotesLoading(false);
    })();
  }, [user]);

  const speakers = Array.from(new Set(quotes.map((q) => q.speaker))).sort();

  const filtered = quotes.filter((q) => {
    const matchSearch = !searchTerm || q.quote_text.toLowerCase().includes(searchTerm.toLowerCase()) || q.speaker.toLowerCase().includes(searchTerm.toLowerCase());
    const matchSpeaker = speakerFilter === "all" || q.speaker === speakerFilter;
    const matchSig = sigFilter === "all" || q.significance === sigFilter;
    return matchSearch && matchSpeaker && matchSig;
  });

  function copyQuote(q: QuoteItem) {
    const text = `"${q.quote_text}" — ${q.speaker}${q.analysis?.title ? `, "${q.analysis.title}"` : ""}`;
    navigator.clipboard.writeText(text);
    setCopiedId(q.id);
    toast.success("Quote copied!");
    setTimeout(() => setCopiedId(null), 2000);
  }

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-6">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
        </Button>

        <h1 className="font-display text-4xl font-semibold tracking-tight flex items-center gap-3 mb-2">
          <Quote className="h-8 w-8 text-red-500" /> Quotes
        </h1>
        <p className="text-muted-foreground mb-8">
          Notable quotes extracted from all your videos · {quotes.length} quote{quotes.length !== 1 ? "s" : ""}
        </p>

        {/* Filters */}
        <div className="flex flex-col sm:flex-row gap-3 mb-6">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} placeholder="Search quotes..." className="pl-9" />
          </div>
          {speakers.length > 1 && (
            <select
              value={speakerFilter}
              onChange={(e) => setSpeakerFilter(e.target.value)}
              className="rounded-lg border border-border bg-background px-3 py-2 text-sm"
            >
              <option value="all">All speakers ({quotes.length})</option>
              {speakers.map((s) => (
                <option key={s} value={s}>{s} ({quotes.filter((q) => q.speaker === s).length})</option>
              ))}
            </select>
          )}
          <div className="flex gap-1 p-1 bg-muted rounded-lg">
            {["all", "high", "medium", "low"].map((s) => (
              <button key={s} onClick={() => setSigFilter(s)} className={`px-2.5 py-1 rounded-md text-xs font-medium capitalize transition-colors ${sigFilter === s ? "bg-background shadow-sm" : "text-muted-foreground"}`}>
                {s}
              </button>
            ))}
          </div>
        </div>

        {quotesLoading ? (
          <div className="grid place-items-center py-20"><Loader2 className="h-6 w-6 animate-spin" /></div>
        ) : filtered.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-12 text-center">
            <Quote className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
            <p className="text-muted-foreground">No quotes found. Process videos to auto-extract notable quotes.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {filtered.map((q) => (
              <div key={q.id} className="rounded-xl border border-border bg-surface/40 p-5 hover:bg-surface/60 transition-colors group">
                <div className="flex items-start gap-4">
                  <div className="shrink-0 mt-1 h-10 w-10 rounded-full bg-primary/10 grid place-items-center">
                    <User className="h-5 w-5 text-primary" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <blockquote className="text-base leading-relaxed border-l-2 border-red-500/50 pl-4 italic">
                      "{q.quote_text}"
                    </blockquote>
                    <div className="flex items-center gap-2 mt-3">
                      <span className="text-sm font-medium">— {q.speaker}</span>
                      <Badge variant="secondary" className={`text-xs ${SIG_COLORS[q.significance] || ""}`}>
                        {q.significance}
                      </Badge>
                      {q.tags?.map((tag) => (
                        <Badge key={tag} variant="outline" className={`text-xs ${TAG_COLORS[tag] || ""}`}>{tag}</Badge>
                      ))}
                    </div>
                    {q.context && <p className="text-xs text-muted-foreground mt-2">{q.context}</p>}
                    <div className="flex items-center gap-3 mt-2">
                      {q.analysis?.title && (
                        <Link to="/analysis/$id" params={{ id: q.analysis_id }} className="flex items-center gap-1 text-xs text-muted-foreground/60 hover:text-foreground transition-colors">
                          <ExternalLink className="h-3 w-3" /><span className="truncate max-w-[250px]">{q.analysis.title}</span>
                        </Link>
                      )}
                      {q.timestamp_seconds != null && (
                        <span className="text-xs text-muted-foreground/60 font-mono">
                          {Math.floor(q.timestamp_seconds / 60)}:{String(Math.floor(q.timestamp_seconds % 60)).padStart(2, "0")}
                        </span>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => copyQuote(q)}
                    className="shrink-0 opacity-0 group-hover:opacity-100 p-2 rounded-lg hover:bg-muted transition-all"
                    title="Copy quote"
                  >
                    {copiedId === q.id ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4 text-muted-foreground" />}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
