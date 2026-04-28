import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState, useCallback } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Search, BookOpen, Users, FileText, MapPin, Building2,
  Scale, Calendar, Loader2, ArrowLeft, ChevronDown, ChevronUp,
  Quote, AlertTriangle, ExternalLink, Landmark, User
} from "lucide-react";
import { toast } from "sonner";

const WORKER_URL = import.meta.env.VITE_WORKER_URL || "https://insightful-tube-explorer-production.up.railway.app";

export const Route = createFileRoute("/knowledge")({
  component: KnowledgeBase,
});

type Fact = {
  id: string;
  analysis_id: string;
  claim: string;
  category: string;
  source_timestamp: number | null;
  citation: string | null;
  confidence: string;
  verified: boolean;
  created_at: string;
  analysis?: { title: string | null; youtube_id: string | null; channel: string | null };
};

type Entity = {
  id: string;
  name: string;
  entity_type: string;
  aliases: string[];
  description: string | null;
  mention_count?: number;
  video_count?: number;
  mentions?: EntityMention[];
};

type EntityMention = {
  id: string;
  analysis_id: string;
  context: string | null;
  role: string | null;
  mention_count: number;
  analysis?: { title: string | null; youtube_id: string | null };
};

type SearchResult = {
  analysis_id: string;
  title: string | null;
  youtube_id: string | null;
  channel: string | null;
  snippet: string;
  field: string;
  likely_production_date: string | null;
};

const ENTITY_ICONS: Record<string, typeof User> = {
  person: User,
  organization: Building2,
  place: MapPin,
  court: Scale,
  agency: Landmark,
  event: Calendar,
};

const CATEGORY_COLORS: Record<string, string> = {
  legal: "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300",
  date: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  person: "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300",
  location: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  event: "bg-pink-100 text-pink-700 dark:bg-pink-950 dark:text-pink-300",
  claim: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
  quote: "bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300",
  general: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
};

function KnowledgeBase() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<"search" | "facts" | "entities">("search");
  const [query, setQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [factsLoading, setFactsLoading] = useState(false);
  const [entitiesLoading, setEntitiesLoading] = useState(false);
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [entityTypeFilter, setEntityTypeFilter] = useState<string>("all");
  const [expandedEntity, setExpandedEntity] = useState<string | null>(null);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading, navigate]);

  // ── Cross-Video Search ──
  const doSearch = useCallback(async () => {
    if (!query.trim() || !user) return;
    setSearching(true);
    try {
      const res = await fetch(`${WORKER_URL}/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim(), user_id: user.id }),
      });
      const data = await res.json();
      if (data.error) {
        toast.error(data.error);
      } else {
        setSearchResults(data.results || []);
        if ((data.results || []).length === 0) {
          toast("No results found", { description: "Try different keywords" });
        }
      }
    } catch (err: any) {
      toast.error(err.message);
    } finally {
      setSearching(false);
    }
  }, [query, user]);

  // ── Load Facts ──
  useEffect(() => {
    if (activeTab !== "facts" || !user) return;
    setFactsLoading(true);
    (async () => {
      // We need to load facts with analysis titles
      const { data, error } = await supabase
        .from("facts")
        .select("*, analysis:analyses(title, youtube_id, channel)")
        .order("created_at", { ascending: false })
        .limit(500);
      if (error) toast.error(error.message);
      else setFacts(data as Fact[]);
      setFactsLoading(false);
    })();
  }, [activeTab, user]);

  // ── Load Entities ──
  useEffect(() => {
    if (activeTab !== "entities" || !user) return;
    setEntitiesLoading(true);
    (async () => {
      const { data: entData, error } = await supabase
        .from("entities")
        .select("*, mentions:entity_mentions(id, analysis_id, context, role, mention_count, analysis:analyses(title, youtube_id))")
        .order("name");
      if (error) toast.error(error.message);
      else {
        const mapped = (entData || []).map((e: any) => ({
          ...e,
          mention_count: (e.mentions || []).reduce((s: number, m: any) => s + (m.mention_count || 1), 0),
          video_count: new Set((e.mentions || []).map((m: any) => m.analysis_id)).size,
        }));
        setEntities(mapped);
      }
      setEntitiesLoading(false);
    })();
  }, [activeTab, user]);

  const filteredFacts = facts.filter((f) => categoryFilter === "all" || f.category === categoryFilter);
  const filteredEntities = entities.filter((e) => entityTypeFilter === "all" || e.entity_type === entityTypeFilter);

  const factCategories = Array.from(new Set(facts.map((f) => f.category)));
  const entityTypes = Array.from(new Set(entities.map((e) => e.entity_type)));

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-5xl px-6 py-8">
        <div className="flex items-center gap-3 mb-8">
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
          </Button>
        </div>

        <div className="mb-8">
          <h1 className="font-display text-4xl font-semibold tracking-tight flex items-center gap-3">
            <BookOpen className="h-8 w-8 text-red-500" /> Knowledge Base
          </h1>
          <p className="mt-2 text-muted-foreground">
            Search across all your videos. Extracted facts, people, organizations, and events — all in one place.
          </p>
        </div>

        {/* ── Tabs ── */}
        <div className="flex gap-1 border-b border-border mb-6">
          {[
            { key: "search", label: "Search", icon: Search },
            { key: "facts", label: "Facts & Citations", icon: FileText },
            { key: "entities", label: "People & Entities", icon: Users },
          ].map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setActiveTab(key as any)}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${
                activeTab === key
                  ? "border-red-500 text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {key === "facts" && facts.length > 0 && (
                <span className="text-xs text-muted-foreground/60 ml-0.5">({facts.length})</span>
              )}
              {key === "entities" && entities.length > 0 && (
                <span className="text-xs text-muted-foreground/60 ml-0.5">({entities.length})</span>
              )}
            </button>
          ))}
        </div>

        {/* ── Search Tab ── */}
        {activeTab === "search" && (
          <div>
            <form
              onSubmit={(e) => { e.preventDefault(); doSearch(); }}
              className="flex gap-2 mb-6"
            >
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search across all transcripts, summaries, notes... (e.g. 'Lafayette County', 'Judge')"
                  className="pl-9 h-12 text-base"
                />
              </div>
              <Button type="submit" size="lg" disabled={searching || !query.trim()}>
                {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                Search
              </Button>
            </form>

            {searchResults.length > 0 && (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">{searchResults.length} result{searchResults.length !== 1 ? "s" : ""} found</p>
                {searchResults.map((r, i) => (
                  <Link
                    key={i}
                    to="/analysis/$id"
                    params={{ id: r.analysis_id }}
                    className="block rounded-xl border border-border bg-surface/40 p-4 hover:bg-surface/70 hover:border-primary/40 transition-all"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <h3 className="font-medium truncate">{r.title ?? "Untitled"}</h3>
                          <Badge variant="secondary" className="text-xs shrink-0">{r.field}</Badge>
                        </div>
                        <p className="text-sm text-muted-foreground line-clamp-2">{r.snippet}</p>
                        <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground/60">
                          {r.channel && <span>{r.channel}</span>}
                          {r.likely_production_date && <span>Recorded: {r.likely_production_date}</span>}
                        </div>
                      </div>
                      {r.youtube_id && (
                        <img
                          src={`https://i.ytimg.com/vi/${r.youtube_id}/default.jpg`}
                          className="h-14 w-20 rounded-md object-cover shrink-0"
                          alt=""
                        />
                      )}
                    </div>
                  </Link>
                ))}
              </div>
            )}

            {searchResults.length === 0 && !searching && query && (
              <div className="text-center py-12">
                <Search className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
                <p className="text-muted-foreground">Press Search to find mentions across all your videos</p>
              </div>
            )}

            {!query && (
              <div className="text-center py-12">
                <BookOpen className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
                <p className="text-muted-foreground">
                  Search for any person, event, date, or topic across all your analyzed videos
                </p>
                <div className="flex flex-wrap justify-center gap-2 mt-4">
                  {["Lafayette County", "court filing", "first amendment", "reardon"].map((s) => (
                    <button
                      key={s}
                      onClick={() => { setQuery(s); }}
                      className="text-xs px-3 py-1.5 rounded-full border border-border hover:bg-muted transition-colors"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── Facts Tab ── */}
        {activeTab === "facts" && (
          <div>
            {factsLoading ? (
              <div className="grid place-items-center py-12"><Loader2 className="h-6 w-6 animate-spin" /></div>
            ) : facts.length === 0 ? (
              <div className="text-center py-12">
                <FileText className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
                <p className="text-muted-foreground">
                  No facts extracted yet. Facts are automatically pulled when videos finish processing.
                </p>
              </div>
            ) : (
              <>
                {/* Category filter */}
                <div className="flex flex-wrap gap-1 mb-4">
                  <button
                    onClick={() => setCategoryFilter("all")}
                    className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                      categoryFilter === "all" ? "bg-foreground text-background" : "bg-muted text-muted-foreground"
                    }`}
                  >
                    All ({facts.length})
                  </button>
                  {factCategories.map((cat) => (
                    <button
                      key={cat}
                      onClick={() => setCategoryFilter(cat)}
                      className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                        categoryFilter === cat
                          ? CATEGORY_COLORS[cat] || "bg-foreground text-background"
                          : "bg-muted text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      {cat} ({facts.filter((f) => f.category === cat).length})
                    </button>
                  ))}
                </div>

                <div className="space-y-2">
                  {filteredFacts.map((f) => (
                    <div key={f.id} className="rounded-lg border border-border bg-surface/40 p-4">
                      <div className="flex items-start gap-3">
                        <div className="shrink-0 pt-0.5">
                          {f.category === "quote" ? (
                            <Quote className="h-4 w-4 text-indigo-500" />
                          ) : f.category === "legal" ? (
                            <Scale className="h-4 w-4 text-purple-500" />
                          ) : f.category === "person" ? (
                            <User className="h-4 w-4 text-green-500" />
                          ) : f.category === "date" ? (
                            <Calendar className="h-4 w-4 text-blue-500" />
                          ) : (
                            <FileText className="h-4 w-4 text-muted-foreground" />
                          )}
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="text-sm leading-relaxed">{f.claim}</p>
                          {f.citation && (
                            <p className="text-xs text-muted-foreground mt-1 font-mono bg-muted rounded px-2 py-1">
                              📋 {f.citation}
                            </p>
                          )}
                          <div className="flex items-center gap-3 mt-2 text-xs text-muted-foreground/60">
                            <Badge variant="secondary" className={`text-xs ${CATEGORY_COLORS[f.category] || ""}`}>
                              {f.category}
                            </Badge>
                            <Badge variant="outline" className="text-xs">
                              {f.confidence} confidence
                            </Badge>
                            {f.analysis?.title && (
                              <Link
                                to="/analysis/$id"
                                params={{ id: f.analysis_id }}
                                className="flex items-center gap-1 hover:text-foreground transition-colors"
                              >
                                <ExternalLink className="h-3 w-3" />
                                <span className="truncate max-w-[200px]">{f.analysis.title}</span>
                              </Link>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* ── Entities Tab ── */}
        {activeTab === "entities" && (
          <div>
            {entitiesLoading ? (
              <div className="grid place-items-center py-12"><Loader2 className="h-6 w-6 animate-spin" /></div>
            ) : entities.length === 0 ? (
              <div className="text-center py-12">
                <Users className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
                <p className="text-muted-foreground">
                  No entities extracted yet. People, organizations, and places are auto-detected when videos process.
                </p>
              </div>
            ) : (
              <>
                {/* Type filter */}
                <div className="flex flex-wrap gap-1 mb-4">
                  <button
                    onClick={() => setEntityTypeFilter("all")}
                    className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                      entityTypeFilter === "all" ? "bg-foreground text-background" : "bg-muted text-muted-foreground"
                    }`}
                  >
                    All ({entities.length})
                  </button>
                  {entityTypes.map((t) => {
                    const Icon = ENTITY_ICONS[t] || User;
                    return (
                      <button
                        key={t}
                        onClick={() => setEntityTypeFilter(t)}
                        className={`flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                          entityTypeFilter === t ? "bg-foreground text-background" : "bg-muted text-muted-foreground"
                        }`}
                      >
                        <Icon className="h-3 w-3" />
                        {t} ({entities.filter((e) => e.entity_type === t).length})
                      </button>
                    );
                  })}
                </div>

                <div className="space-y-2">
                  {filteredEntities.map((ent) => {
                    const Icon = ENTITY_ICONS[ent.entity_type] || User;
                    const isExpanded = expandedEntity === ent.id;
                    return (
                      <div key={ent.id} className="rounded-xl border border-border bg-surface/40">
                        <button
                          onClick={() => setExpandedEntity(isExpanded ? null : ent.id)}
                          className="w-full p-4 flex items-center gap-3 text-left"
                        >
                          <div className="h-10 w-10 rounded-full bg-primary/10 grid place-items-center shrink-0">
                            <Icon className="h-5 w-5 text-primary" />
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="font-medium">{ent.name}</span>
                              <Badge variant="secondary" className="text-xs capitalize">{ent.entity_type}</Badge>
                            </div>
                            {ent.description && (
                              <p className="text-sm text-muted-foreground truncate">{ent.description}</p>
                            )}
                          </div>
                          <div className="text-right shrink-0">
                            <p className="text-sm font-medium">{ent.mention_count} mention{(ent.mention_count ?? 0) !== 1 ? "s" : ""}</p>
                            <p className="text-xs text-muted-foreground">{ent.video_count} video{(ent.video_count ?? 0) !== 1 ? "s" : ""}</p>
                          </div>
                          {isExpanded ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
                        </button>

                        {isExpanded && ent.mentions && ent.mentions.length > 0 && (
                          <div className="border-t border-border px-4 py-3 space-y-2">
                            {ent.aliases && ent.aliases.length > 0 && (
                              <p className="text-xs text-muted-foreground">
                                Also known as: {ent.aliases.join(", ")}
                              </p>
                            )}
                            {ent.mentions.map((m) => (
                              <Link
                                key={m.id}
                                to="/analysis/$id"
                                params={{ id: m.analysis_id }}
                                className="block rounded-lg bg-muted/50 p-3 hover:bg-muted transition-colors"
                              >
                                <div className="flex items-center justify-between mb-1">
                                  <span className="text-sm font-medium">{m.analysis?.title ?? "Untitled"}</span>
                                  <ExternalLink className="h-3 w-3 text-muted-foreground" />
                                </div>
                                {m.role && <p className="text-xs text-muted-foreground">Role: {m.role}</p>}
                                {m.context && <p className="text-xs text-muted-foreground mt-1 line-clamp-2">"{m.context}"</p>}
                              </Link>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
