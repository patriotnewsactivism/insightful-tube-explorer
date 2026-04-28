import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft, Calendar, Loader2, ChevronRight, Filter,
  Scale, AlertTriangle, MapPin, Clock, Gavel, ExternalLink
} from "lucide-react";

export const Route = createFileRoute("/timeline")({ component: TimelinePage });

type TimelineEvent = {
  id: string;
  analysis_id: string;
  event_date: string;
  event_date_precision: string;
  event_description: string;
  source_context: string | null;
  category: string;
  confidence: string;
  analysis?: { title: string | null; youtube_id: string | null };
};

const CAT_COLORS: Record<string, string> = {
  filing: "border-l-purple-500",
  hearing: "border-l-blue-500",
  incident: "border-l-red-500",
  deadline: "border-l-amber-500",
  arrest: "border-l-rose-600",
  ruling: "border-l-emerald-500",
  other: "border-l-gray-400",
};

const CAT_ICONS: Record<string, typeof Calendar> = {
  filing: Scale,
  hearing: Gavel,
  incident: AlertTriangle,
  deadline: Clock,
  arrest: AlertTriangle,
  ruling: Gavel,
};

function TimelinePage() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [catFilter, setCatFilter] = useState("all");

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("timeline_events")
        .select("*, analysis:analyses(title, youtube_id)")
        .order("event_date", { ascending: true });
      if (!error && data) setEvents(data as TimelineEvent[]);
      setEventsLoading(false);
    })();
  }, [user]);

  const categories = Array.from(new Set(events.map((e) => e.category)));
  const filtered = events.filter((e) => catFilter === "all" || e.category === catFilter);

  // Group by year/month
  const groups: Record<string, TimelineEvent[]> = {};
  for (const ev of filtered) {
    const key = ev.event_date.slice(0, 7) || "Unknown";
    if (!groups[key]) groups[key] = [];
    groups[key].push(ev);
  }

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-6">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
        </Button>

        <h1 className="font-display text-4xl font-semibold tracking-tight flex items-center gap-3 mb-2">
          <Calendar className="h-8 w-8 text-red-500" /> Timeline
        </h1>
        <p className="text-muted-foreground mb-8">
          Chronological events extracted from all your videos · {events.length} event{events.length !== 1 ? "s" : ""}
        </p>

        {/* Category filter */}
        <div className="flex flex-wrap gap-1 mb-6">
          <button onClick={() => setCatFilter("all")} className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${catFilter === "all" ? "bg-foreground text-background" : "bg-muted text-muted-foreground"}`}>
            All ({events.length})
          </button>
          {categories.map((cat) => (
            <button key={cat} onClick={() => setCatFilter(cat)} className={`px-2.5 py-1 rounded-full text-xs font-medium capitalize transition-colors ${catFilter === cat ? "bg-foreground text-background" : "bg-muted text-muted-foreground"}`}>
              {cat} ({events.filter((e) => e.category === cat).length})
            </button>
          ))}
        </div>

        {eventsLoading ? (
          <div className="grid place-items-center py-20"><Loader2 className="h-6 w-6 animate-spin" /></div>
        ) : filtered.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-12 text-center">
            <Calendar className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
            <p className="text-muted-foreground">No timeline events yet. Process videos to auto-extract dates and events.</p>
          </div>
        ) : (
          <div className="relative">
            {/* Vertical timeline line */}
            <div className="absolute left-[19px] top-0 bottom-0 w-px bg-border" />

            {Object.entries(groups).map(([period, evs]) => (
              <div key={period} className="mb-8">
                <div className="relative flex items-center gap-3 mb-4">
                  <div className="h-10 w-10 rounded-full bg-red-100 dark:bg-red-950/50 grid place-items-center z-10">
                    <Calendar className="h-5 w-5 text-red-600" />
                  </div>
                  <h2 className="font-display text-lg font-semibold">{period}</h2>
                </div>

                <div className="ml-[19px] pl-8 space-y-3">
                  {evs.map((ev) => {
                    const Icon = CAT_ICONS[ev.category] || Calendar;
                    return (
                      <div key={ev.id} className={`rounded-lg border border-border bg-surface/40 p-4 border-l-4 ${CAT_COLORS[ev.category] || CAT_COLORS.other}`}>
                        <div className="flex items-start gap-3">
                          <Icon className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-sm font-mono font-medium">{ev.event_date}</span>
                              <Badge variant="secondary" className="text-xs capitalize">{ev.category}</Badge>
                              {ev.event_date_precision !== "exact" && (
                                <Badge variant="outline" className="text-xs">{ev.event_date_precision}</Badge>
                              )}
                            </div>
                            <p className="text-sm leading-relaxed">{ev.event_description}</p>
                            {ev.source_context && (
                              <p className="text-xs text-muted-foreground mt-1 italic">"{ev.source_context}"</p>
                            )}
                            {ev.analysis?.title && (
                              <Link to="/analysis/$id" params={{ id: ev.analysis_id }} className="flex items-center gap-1 text-xs text-muted-foreground/60 mt-2 hover:text-foreground transition-colors">
                                <ExternalLink className="h-3 w-3" />{ev.analysis.title}
                              </Link>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
