import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft, Users, Loader2, Search, User, Building2, MapPin,
  Scale, Landmark, Calendar, ExternalLink, ChevronDown, ChevronUp,
  Hash, BookOpen, Quote, FileText, SortAsc, SortDesc
} from "lucide-react";

export const Route = createFileRoute("/people")({ component: PeoplePage });

type Entity = {
  id: string;
  name: string;
  entity_type: string;
  aliases: string[];
  description: string | null;
  first_seen_analysis: string | null;
  created_at: string;
  mentions: Mention[];
  facts: FactRef[];
  quotes: QuoteRef[];
};

type Mention = {
  id: string;
  analysis_id: string;
  context: string | null;
  role: string | null;
  mention_count: number;
  analysis?: { title: string | null; youtube_id: string | null; likely_production_date: string | null };
};

type FactRef = { id: string; claim: string; category: string; analysis_id: string };
type QuoteRef = { id: string; speaker: string; quote_text: string; analysis_id: string };

const TYPE_ICONS: Record<string, typeof User> = {
  person: User, organization: Building2, place: MapPin, court: Scale, agency: Landmark, event: Calendar,
};

const TYPE_COLORS: Record<string, string> = {
  person: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  organization: "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300",
  place: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  court: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  agency: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  event: "bg-pink-100 text-pink-700 dark:bg-pink-950 dark:text-pink-300",
};

function PeoplePage() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [entities, setEntities] = useState<Entity[]>([]);
  const [entitiesLoading, setEntitiesLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [sortBy, setSortBy] = useState<"name" | "mentions" | "videos">("mentions");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [entityFacts, setEntityFacts] = useState<Record<string, FactRef[]>>({});
  const [entityQuotes, setEntityQuotes] = useState<Record<string, QuoteRef[]>>({});

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading]);

  useEffect(() => {
    if (!user) return;
    (async () => {
      const { data, error } = await supabase
        .from("entities")
        .select("*, mentions:entity_mentions(id, analysis_id, context, role, mention_count, analysis:analyses(title, youtube_id, likely_production_date))")
        .order("name");
      if (!error && data) setEntities(data as Entity[]);
      setEntitiesLoading(false);
    })();
  }, [user]);

  // Load facts & quotes for expanded entity
  useEffect(() => {
    if (!expandedId) return;
    const ent = entities.find((e) => e.id === expandedId);
    if (!ent) return;
    const analysisIds = [...new Set(ent.mentions.map((m) => m.analysis_id))];

    // Load facts mentioning this entity's name
    (async () => {
      const { data } = await supabase
        .from("facts")
        .select("id, claim, category, analysis_id")
        .in("analysis_id", analysisIds)
        .ilike("claim", `%${ent.name}%`)
        .limit(50);
      if (data) setEntityFacts((prev) => ({ ...prev, [expandedId]: data }));
    })();

    // Load quotes by this entity
    (async () => {
      const { data } = await supabase
        .from("quotes")
        .select("id, speaker, quote_text, analysis_id")
        .in("analysis_id", analysisIds)
        .ilike("speaker", `%${ent.name}%`)
        .limit(50);
      if (data) setEntityQuotes((prev) => ({ ...prev, [expandedId]: data }));
    })();
  }, [expandedId, entities]);

  const types = Array.from(new Set(entities.map((e) => e.entity_type)));

  const processed = entities
    .map((e) => ({
      ...e,
      totalMentions: e.mentions.reduce((s, m) => s + (m.mention_count || 1), 0),
      videoCount: new Set(e.mentions.map((m) => m.analysis_id)).size,
    }))
    .filter((e) => {
      const matchSearch = !searchTerm ||
        e.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
        (e.description || "").toLowerCase().includes(searchTerm.toLowerCase()) ||
        e.aliases.some((a) => a.toLowerCase().includes(searchTerm.toLowerCase()));
      const matchType = typeFilter === "all" || e.entity_type === typeFilter;
      return matchSearch && matchType;
    })
    .sort((a, b) => {
      let cmp = 0;
      if (sortBy === "name") cmp = a.name.localeCompare(b.name);
      else if (sortBy === "mentions") cmp = a.totalMentions - b.totalMentions;
      else cmp = a.videoCount - b.videoCount;
      return sortDir === "desc" ? -cmp : cmp;
    });

  const totalPeople = entities.filter((e) => e.entity_type === "person").length;
  const totalOrgs = entities.filter((e) => ["organization", "agency", "court"].includes(e.entity_type)).length;
  const totalPlaces = entities.filter((e) => e.entity_type === "place").length;

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-5xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-6">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
        </Button>

        <h1 className="font-display text-4xl font-semibold tracking-tight flex items-center gap-3 mb-2">
          <Users className="h-8 w-8 text-red-500" /> People & Entity Index
        </h1>
        <p className="text-muted-foreground mb-4">
          Complete index of every person, organization, court, and place across your videos — like a book index.
        </p>

        {/* Stats bar */}
        <div className="flex gap-4 mb-6 text-sm">
          <div className="flex items-center gap-1.5"><User className="h-4 w-4 text-blue-500" /> <span className="font-medium">{totalPeople}</span> people</div>
          <div className="flex items-center gap-1.5"><Building2 className="h-4 w-4 text-purple-500" /> <span className="font-medium">{totalOrgs}</span> organizations</div>
          <div className="flex items-center gap-1.5"><MapPin className="h-4 w-4 text-amber-500" /> <span className="font-medium">{totalPlaces}</span> places</div>
        </div>

        {/* Filters */}
        <div className="flex flex-col sm:flex-row gap-3 mb-6">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} placeholder="Search names, aliases, descriptions..." className="pl-9" />
          </div>
          <div className="flex gap-1 p-1 bg-muted rounded-lg">
            <button onClick={() => setTypeFilter("all")} className={`px-2.5 py-1 rounded-md text-xs font-medium ${typeFilter === "all" ? "bg-background shadow-sm" : "text-muted-foreground"}`}>
              All
            </button>
            {types.map((t) => {
              const Icon = TYPE_ICONS[t] || User;
              return (
                <button key={t} onClick={() => setTypeFilter(t)} className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium capitalize ${typeFilter === t ? "bg-background shadow-sm" : "text-muted-foreground"}`}>
                  <Icon className="h-3 w-3" />{t}
                </button>
              );
            })}
          </div>
          <div className="flex gap-1 p-1 bg-muted rounded-lg">
            {(["mentions", "videos", "name"] as const).map((s) => (
              <button
                key={s}
                onClick={() => { if (sortBy === s) setSortDir(sortDir === "asc" ? "desc" : "asc"); else { setSortBy(s); setSortDir("desc"); } }}
                className={`flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium capitalize ${sortBy === s ? "bg-background shadow-sm" : "text-muted-foreground"}`}
              >
                {s} {sortBy === s && (sortDir === "desc" ? <SortDesc className="h-3 w-3" /> : <SortAsc className="h-3 w-3" />)}
              </button>
            ))}
          </div>
        </div>

        {entitiesLoading ? (
          <div className="grid place-items-center py-20"><Loader2 className="h-6 w-6 animate-spin" /></div>
        ) : processed.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-12 text-center">
            <Users className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
            <p className="text-muted-foreground">No entities indexed yet. Process videos to auto-detect people, organizations, and places.</p>
          </div>
        ) : (
          <div className="space-y-2">
            {processed.map((ent) => {
              const Icon = TYPE_ICONS[ent.entity_type] || User;
              const isExpanded = expandedId === ent.id;
              const facts = entityFacts[ent.id] || [];
              const quotes = entityQuotes[ent.id] || [];

              return (
                <div key={ent.id} className="rounded-xl border border-border bg-surface/40">
                  <button onClick={() => setExpandedId(isExpanded ? null : ent.id)} className="w-full p-4 flex items-center gap-3 text-left">
                    <div className={`h-12 w-12 rounded-xl grid place-items-center shrink-0 ${TYPE_COLORS[ent.entity_type] || "bg-gray-100"}`}>
                      <Icon className="h-6 w-6" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium text-base">{ent.name}</span>
                        <Badge variant="secondary" className="text-xs capitalize">{ent.entity_type}</Badge>
                        {ent.aliases.length > 0 && (
                          <span className="text-xs text-muted-foreground">aka {ent.aliases.join(", ")}</span>
                        )}
                      </div>
                      {ent.description && <p className="text-sm text-muted-foreground mt-0.5 line-clamp-1">{ent.description}</p>}
                    </div>
                    <div className="text-right shrink-0 space-y-0.5">
                      <div className="flex items-center gap-1 text-sm font-medium justify-end">
                        <Hash className="h-3 w-3" /> {ent.totalMentions}
                      </div>
                      <p className="text-xs text-muted-foreground">{ent.videoCount} video{ent.videoCount !== 1 ? "s" : ""}</p>
                    </div>
                    {isExpanded ? <ChevronUp className="h-4 w-4 text-muted-foreground ml-2" /> : <ChevronDown className="h-4 w-4 text-muted-foreground ml-2" />}
                  </button>

                  {isExpanded && (
                    <div className="border-t border-border px-4 py-4 space-y-4">
                      {/* Video appearances */}
                      <div>
                        <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2 flex items-center gap-1">
                          <FileText className="h-3 w-3" /> Appears in ({ent.mentions.length} videos)
                        </h4>
                        <div className="space-y-1">
                          {ent.mentions.map((m) => (
                            <Link key={m.id} to="/analysis/$id" params={{ id: m.analysis_id }}
                              className="flex items-center gap-3 rounded-lg bg-muted/40 p-3 hover:bg-muted transition-colors"
                            >
                              <div className="min-w-0 flex-1">
                                <p className="text-sm font-medium truncate">{m.analysis?.title ?? "Untitled"}</p>
                                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                                  {m.role && <span>{m.role}</span>}
                                  {m.analysis?.likely_production_date && <span>· Recorded {m.analysis.likely_production_date}</span>}
                                  <span>· {m.mention_count} mention{m.mention_count !== 1 ? "s" : ""}</span>
                                </div>
                                {m.context && <p className="text-xs text-muted-foreground/60 mt-1 italic line-clamp-1">"{m.context}"</p>}
                              </div>
                              <ExternalLink className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                            </Link>
                          ))}
                        </div>
                      </div>

                      {/* Related facts */}
                      {facts.length > 0 && (
                        <div>
                          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2 flex items-center gap-1">
                            <BookOpen className="h-3 w-3" /> Related Facts ({facts.length})
                          </h4>
                          <div className="space-y-1">
                            {facts.slice(0, 10).map((f) => (
                              <div key={f.id} className="rounded-lg bg-muted/30 p-2.5 text-sm">
                                <Badge variant="secondary" className="text-xs mr-2">{f.category}</Badge>
                                {f.claim}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* Quotes by/about this person */}
                      {quotes.length > 0 && (
                        <div>
                          <h4 className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-2 flex items-center gap-1">
                            <Quote className="h-3 w-3" /> Quotes ({quotes.length})
                          </h4>
                          <div className="space-y-1">
                            {quotes.slice(0, 5).map((q) => (
                              <div key={q.id} className="rounded-lg bg-muted/30 p-2.5 text-sm italic border-l-2 border-red-500/30 pl-3">
                                "{q.quote_text.slice(0, 150)}{q.quote_text.length > 150 ? "..." : ""}"
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
