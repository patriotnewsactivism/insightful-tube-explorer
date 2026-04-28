import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { useEffect, useState, useCallback } from "react";
import { supabase } from "@/integrations/supabase/client";
import { useAuth } from "@/hooks/useAuth";
import { SiteHeader } from "@/components/SiteHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft, BookMarked, Loader2, Plus, Trash2, Edit2, Check, X,
  FileText, Quote, Users, Calendar, ExternalLink, GripVertical, ChevronDown, ChevronUp
} from "lucide-react";
import { toast } from "sonner";

export const Route = createFileRoute("/chapters")({ component: ChaptersPage });

type Chapter = {
  id: string;
  title: string;
  chapter_number: number | null;
  description: string | null;
  color: string;
  tags?: ChapterTag[];
};

type ChapterTag = {
  id: string;
  chapter_id: string;
  analysis_id: string | null;
  fact_id: string | null;
  quote_id: string | null;
  entity_id: string | null;
  timeline_event_id: string | null;
  notes: string | null;
  // Joined data
  analysis?: { title: string | null } | null;
  fact?: { claim: string; category: string } | null;
  quote?: { speaker: string; quote_text: string } | null;
  entity?: { name: string; entity_type: string } | null;
  timeline_event?: { event_date: string; event_description: string } | null;
};

type AvailableItem = { id: string; type: string; label: string; sub: string };

const COLORS = ["#ef4444", "#f59e0b", "#10b981", "#3b82f6", "#8b5cf6", "#ec4899", "#6366f1", "#14b8a6"];

function ChaptersPage() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [chaptersLoading, setChaptersLoading] = useState(true);
  const [expandedChapter, setExpandedChapter] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editDesc, setEditDesc] = useState("");
  // Add item modal state
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const [availableItems, setAvailableItems] = useState<AvailableItem[]>([]);
  const [itemSearch, setItemSearch] = useState("");
  const [itemsLoading, setItemsLoading] = useState(false);

  useEffect(() => {
    if (!loading && !user) navigate({ to: "/auth" });
  }, [user, loading]);

  const loadChapters = useCallback(async () => {
    if (!user) return;
    const { data, error } = await supabase
      .from("chapters")
      .select(`*, tags:chapter_tags(
        id, chapter_id, analysis_id, fact_id, quote_id, entity_id, timeline_event_id, notes,
        analysis:analyses(title),
        fact:facts(claim, category),
        quote:quotes(speaker, quote_text),
        entity:entities(name, entity_type),
        timeline_event:timeline_events(event_date, event_description)
      )`)
      .order("chapter_number", { ascending: true });
    if (!error && data) setChapters(data as Chapter[]);
    setChaptersLoading(false);
  }, [user]);

  useEffect(() => { loadChapters(); }, [loadChapters]);

  async function createChapter() {
    if (!newTitle.trim() || !user) return;
    setCreating(true);
    const num = chapters.length + 1;
    const { error } = await supabase.from("chapters").insert({
      user_id: user.id,
      title: newTitle.trim(),
      chapter_number: num,
      color: COLORS[(num - 1) % COLORS.length],
    });
    if (error) toast.error(error.message);
    else {
      toast.success(`Chapter ${num} created`);
      setNewTitle("");
      loadChapters();
    }
    setCreating(false);
  }

  async function deleteChapter(id: string) {
    if (!confirm("Delete this chapter and all its tags?")) return;
    await supabase.from("chapters").delete().eq("id", id);
    setChapters((prev) => prev.filter((c) => c.id !== id));
    toast.success("Chapter deleted");
  }

  async function saveEdit(id: string) {
    await supabase.from("chapters").update({ title: editTitle, description: editDesc || null }).eq("id", id);
    setEditingId(null);
    loadChapters();
    toast.success("Updated");
  }

  async function removeTag(tagId: string) {
    await supabase.from("chapter_tags").delete().eq("id", tagId);
    loadChapters();
  }

  // Load available items to tag
  async function openAddItem(chapterId: string) {
    setAddingTo(chapterId);
    setItemsLoading(true);
    setItemSearch("");
    const items: AvailableItem[] = [];

    const [analyses, facts, quotes, entities, events] = await Promise.all([
      supabase.from("analyses").select("id, title, channel").order("created_at", { ascending: false }).limit(100),
      supabase.from("facts").select("id, claim, category").order("created_at", { ascending: false }).limit(200),
      supabase.from("quotes").select("id, speaker, quote_text").order("created_at", { ascending: false }).limit(200),
      supabase.from("entities").select("id, name, entity_type").order("name").limit(200),
      supabase.from("timeline_events").select("id, event_date, event_description").order("event_date").limit(200),
    ]);

    for (const a of analyses.data || []) items.push({ id: a.id, type: "analysis", label: a.title || "Untitled", sub: a.channel || "" });
    for (const f of facts.data || []) items.push({ id: f.id, type: "fact", label: f.claim.slice(0, 80), sub: f.category });
    for (const q of quotes.data || []) items.push({ id: q.id, type: "quote", label: `"${q.quote_text.slice(0, 60)}..."`, sub: q.speaker });
    for (const e of entities.data || []) items.push({ id: e.id, type: "entity", label: e.name, sub: e.entity_type });
    for (const ev of events.data || []) items.push({ id: ev.id, type: "timeline", label: ev.event_description.slice(0, 80), sub: ev.event_date });

    setAvailableItems(items);
    setItemsLoading(false);
  }

  async function addItemToChapter(item: AvailableItem) {
    if (!addingTo) return;
    const row: Record<string, string> = { chapter_id: addingTo };
    if (item.type === "analysis") row.analysis_id = item.id;
    else if (item.type === "fact") row.fact_id = item.id;
    else if (item.type === "quote") row.quote_id = item.id;
    else if (item.type === "entity") row.entity_id = item.id;
    else if (item.type === "timeline") row.timeline_event_id = item.id;

    const { error } = await supabase.from("chapter_tags").insert(row);
    if (error) toast.error(error.message);
    else {
      toast.success("Added to chapter");
      loadChapters();
    }
  }

  const filteredItems = availableItems.filter((i) =>
    !itemSearch || i.label.toLowerCase().includes(itemSearch.toLowerCase()) || i.sub.toLowerCase().includes(itemSearch.toLowerCase())
  );

  const TYPE_ICONS: Record<string, typeof FileText> = { analysis: FileText, fact: FileText, quote: Quote, entity: Users, timeline: Calendar };

  return (
    <div className="min-h-screen">
      <SiteHeader />
      <main className="mx-auto max-w-4xl px-6 py-8">
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-6">
          <Link to="/dashboard"><ArrowLeft className="h-4 w-4 mr-1" /> Library</Link>
        </Button>

        <h1 className="font-display text-4xl font-semibold tracking-tight flex items-center gap-3 mb-2">
          <BookMarked className="h-8 w-8 text-red-500" /> Book Chapters
        </h1>
        <p className="text-muted-foreground mb-8">
          Organize your research into book chapters. Tag videos, facts, quotes, and entities to each chapter.
        </p>

        {/* Create chapter */}
        <div className="flex gap-2 mb-8">
          <Input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="New chapter title..."
            className="max-w-sm"
            onKeyDown={(e) => e.key === "Enter" && createChapter()}
          />
          <Button onClick={createChapter} disabled={creating || !newTitle.trim()}>
            <Plus className="h-4 w-4 mr-1" /> Add Chapter
          </Button>
        </div>

        {chaptersLoading ? (
          <div className="grid place-items-center py-20"><Loader2 className="h-6 w-6 animate-spin" /></div>
        ) : chapters.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-12 text-center">
            <BookMarked className="h-8 w-8 text-muted-foreground/30 mx-auto mb-3" />
            <p className="text-muted-foreground">No chapters yet. Create your first chapter to start organizing research.</p>
          </div>
        ) : (
          <div className="space-y-4">
            {chapters.map((ch) => {
              const isExpanded = expandedChapter === ch.id;
              const isEditing = editingId === ch.id;
              const tags = ch.tags || [];
              return (
                <div key={ch.id} className="rounded-xl border border-border bg-surface/40 overflow-hidden">
                  {/* Chapter header */}
                  <div className="flex items-center gap-3 p-4">
                    <div className="h-10 w-10 rounded-lg grid place-items-center font-bold text-white text-sm" style={{ backgroundColor: ch.color }}>
                      {ch.chapter_number ?? "?"}
                    </div>
                    <div className="flex-1 min-w-0">
                      {isEditing ? (
                        <div className="space-y-2">
                          <Input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} className="font-medium" />
                          <Input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} placeholder="Description (optional)" className="text-sm" />
                          <div className="flex gap-1">
                            <Button size="sm" onClick={() => saveEdit(ch.id)}><Check className="h-3 w-3 mr-1" />Save</Button>
                            <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}><X className="h-3 w-3" /></Button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <h3 className="font-medium">{ch.title}</h3>
                          {ch.description && <p className="text-sm text-muted-foreground">{ch.description}</p>}
                        </>
                      )}
                    </div>
                    <Badge variant="secondary" className="text-xs">{tags.length} item{tags.length !== 1 ? "s" : ""}</Badge>
                    {!isEditing && (
                      <div className="flex gap-1">
                        <Button variant="ghost" size="sm" onClick={() => { setEditingId(ch.id); setEditTitle(ch.title); setEditDesc(ch.description || ""); }}>
                          <Edit2 className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => deleteChapter(ch.id)}>
                          <Trash2 className="h-3.5 w-3.5 text-red-500" />
                        </Button>
                      </div>
                    )}
                    <button onClick={() => setExpandedChapter(isExpanded ? null : ch.id)}>
                      {isExpanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                    </button>
                  </div>

                  {/* Expanded: show tagged items */}
                  {isExpanded && (
                    <div className="border-t border-border px-4 py-3 space-y-2">
                      {tags.map((t) => {
                        let icon = FileText;
                        let label = "";
                        let sub = "";
                        let linkTo: string | null = null;

                        if (t.analysis) { icon = FileText; label = t.analysis.title || "Untitled"; sub = "Video"; linkTo = t.analysis_id!; }
                        else if (t.fact) { icon = FileText; label = t.fact.claim.slice(0, 100); sub = `Fact (${t.fact.category})`; }
                        else if (t.quote) { icon = Quote; label = `"${t.quote.quote_text.slice(0, 80)}..."`; sub = t.quote.speaker; }
                        else if (t.entity) { icon = Users; label = t.entity.name; sub = t.entity.entity_type; }
                        else if (t.timeline_event) { icon = Calendar; label = t.timeline_event.event_description.slice(0, 80); sub = t.timeline_event.event_date; }

                        const Icon = icon;
                        return (
                          <div key={t.id} className="flex items-center gap-3 rounded-lg bg-muted/40 p-3 group">
                            <Icon className="h-4 w-4 text-muted-foreground shrink-0" />
                            <div className="min-w-0 flex-1">
                              <p className="text-sm truncate">{label}</p>
                              <p className="text-xs text-muted-foreground">{sub}</p>
                            </div>
                            {linkTo && (
                              <Link to="/analysis/$id" params={{ id: linkTo }} className="shrink-0">
                                <ExternalLink className="h-3.5 w-3.5 text-muted-foreground hover:text-foreground" />
                              </Link>
                            )}
                            <button onClick={() => removeTag(t.id)} className="shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                              <X className="h-3.5 w-3.5 text-red-500" />
                            </button>
                          </div>
                        );
                      })}

                      <Button variant="outline" size="sm" className="w-full mt-2" onClick={() => openAddItem(ch.id)}>
                        <Plus className="h-3.5 w-3.5 mr-1" /> Add item to chapter
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Add item modal */}
        {addingTo && (
          <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick={() => setAddingTo(null)}>
            <div className="bg-background rounded-2xl border border-border max-w-lg w-full max-h-[70vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
              <div className="p-4 border-b border-border flex items-center justify-between">
                <h3 className="font-display text-lg font-semibold">Add to Chapter</h3>
                <button onClick={() => setAddingTo(null)}><X className="h-5 w-5" /></button>
              </div>
              <div className="p-3 border-b border-border">
                <Input value={itemSearch} onChange={(e) => setItemSearch(e.target.value)} placeholder="Search videos, facts, quotes, people..." autoFocus />
              </div>
              <div className="flex-1 overflow-y-auto p-2">
                {itemsLoading ? (
                  <div className="grid place-items-center py-8"><Loader2 className="h-5 w-5 animate-spin" /></div>
                ) : filteredItems.length === 0 ? (
                  <p className="text-center text-muted-foreground py-8 text-sm">No items found</p>
                ) : (
                  filteredItems.slice(0, 50).map((item) => {
                    const Icon = TYPE_ICONS[item.type] || FileText;
                    return (
                      <button
                        key={`${item.type}-${item.id}`}
                        onClick={() => addItemToChapter(item)}
                        className="w-full flex items-center gap-3 p-3 rounded-lg hover:bg-muted transition-colors text-left"
                      >
                        <Icon className="h-4 w-4 text-muted-foreground shrink-0" />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm truncate">{item.label}</p>
                          <p className="text-xs text-muted-foreground capitalize">{item.type} · {item.sub}</p>
                        </div>
                        <Plus className="h-4 w-4 text-muted-foreground shrink-0" />
                      </button>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
