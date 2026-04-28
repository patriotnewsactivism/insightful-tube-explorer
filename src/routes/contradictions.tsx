import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft, AlertTriangle, Loader2, ExternalLink, CheckCircle2, ArrowLeftRight
} from "lucide-react";

export const Route = createFileRoute("/contradictions")({ component: ContradictionsPage });

type Contradiction = {
  id: string;
  claim_a_analysis_id: string;
  claim_b_analysis_id: string | null;
  claim_a: string;
  claim_b: string;
  explanation: string | null;
  severity: string;
  resolved: boolean;
  resolution_notes: string | null;
  created_at: string;
  analysis_a?: { title: string | null };
  analysis_b?: { title: string | null };
};

const SEV_COLORS: Record<string, string> = {
  high: "bg-red-100 text-red-700 border-red-200 dark:bg-red-950/50 dark:text-red-300 dark:border-red-900",
  medium: "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950/50 dark:text-amber-300 dark:border-amber-900",
  low: "bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-950/50 dark:text-blue-300 dark:border-blue-900",
};

function ContradictionsPage() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [items, setItems] = useState<Contradiction[]>([]);
  const [itemsLoading, setItemsLoading] = useState(true);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("contradictions")
        .select("*, analysis_a:analyses!claim_a_analysis_id(title), analysis_b:analyses!claim_b_analysis_id(title)")
        .order("created_at", { ascending: false });
      if (!error && data) setItems(data as Contradiction[]);
      setItemsLoading(false);
    })();
  }, [user]);

  const unresolved = items.filter((i) => !i.resolved);
  const resolved = items.filter((i) => i.resolved);

  async function toggleResolved(id: string, current: boolean) {
    await supabase.from("contradictions").update({ resolved: !current }).eq("id", id);
    setItems((prev) => prev.map((i) => i.id === id ? { ...i, resolved: !current } : i));
  }

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-6">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
        </Button>

        <h1 className="font-display text-4xl font-semibold tracking-tight flex items-center gap-3 mb-2">
          <AlertTriangle className="h-8 w-8 text-red-500" /> Contradictions
        </h1>
        <p className="text-muted-foreground mb-8">
          Conflicting claims detected across your videos · {unresolved.length} unresolved
        </p>

        {itemsLoading ? (
          <div className="grid place-items-center py-20"><Loader2 className="h-6 w-6 animate-spin" /></div>
        ) : items.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-12 text-center">
            <CheckCircle2 className="h-8 w-8 text-green-500/50 mx-auto mb-3" />
            <p className="text-muted-foreground">No contradictions detected. Process multiple videos to enable cross-video fact checking.</p>
          </div>
        ) : (
          <div className="space-y-4">
            {[...unresolved, ...resolved].map((c) => (
              <div key={c.id} className={`rounded-xl border p-5 transition-all ${c.resolved ? "opacity-60 bg-muted/30 border-border" : SEV_COLORS[c.severity] || SEV_COLORS.medium}`}>
                <div className="flex items-start justify-between gap-3 mb-4">
                  <div className="flex items-center gap-2">
                    <AlertTriangle className={`h-5 w-5 ${c.resolved ? "text-muted-foreground" : c.severity === "high" ? "text-red-600" : c.severity === "medium" ? "text-amber-600" : "text-blue-600"}`} />
                    <Badge variant="secondary" className="text-xs capitalize">{c.severity} severity</Badge>
                    {c.resolved && <Badge variant="outline" className="text-xs text-green-600">Resolved</Badge>}
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => toggleResolved(c.id, c.resolved)} className="text-xs">
                    {c.resolved ? "Reopen" : "Mark Resolved"}
                  </Button>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-3">
                  <div className="rounded-lg bg-background/60 p-4 border border-border/50">
                    <p className="text-xs text-muted-foreground mb-1 uppercase tracking-wide font-medium">Claim A</p>
                    <p className="text-sm leading-relaxed">{c.claim_a}</p>
                    {c.analysis_a?.title && (
                      <Link to="/analysis/$id" params={{ id: c.claim_a_analysis_id }} className="flex items-center gap-1 text-xs text-muted-foreground/60 mt-2 hover:text-foreground transition-colors">
                        <ExternalLink className="h-3 w-3" /><span className="truncate">{c.analysis_a.title}</span>
                      </Link>
                    )}
                  </div>
                  <div className="rounded-lg bg-background/60 p-4 border border-border/50">
                    <p className="text-xs text-muted-foreground mb-1 uppercase tracking-wide font-medium">Claim B</p>
                    <p className="text-sm leading-relaxed">{c.claim_b}</p>
                    {c.analysis_b?.title && c.claim_b_analysis_id && (
                      <Link to="/analysis/$id" params={{ id: c.claim_b_analysis_id }} className="flex items-center gap-1 text-xs text-muted-foreground/60 mt-2 hover:text-foreground transition-colors">
                        <ExternalLink className="h-3 w-3" /><span className="truncate">{c.analysis_b.title}</span>
                      </Link>
                    )}
                  </div>
                </div>

                {c.explanation && (
                  <div className="flex items-start gap-2 rounded-lg bg-background/40 p-3">
                    <ArrowLeftRight className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
                    <p className="text-sm text-muted-foreground leading-relaxed">{c.explanation}</p>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
